"""An engine that keeps everything in memory.

For tests, and for clicking through the UI with no node anywhere. It is
deliberately *faithful* rather than permissive, for the reason the old
controller mock taught the hard way: a harness that accepts anything proves
nothing, and a gap in it reads exactly like a bug in the thing under test.
So paths are guarded the way the agent guards them, a command to a stopped
server is refused, a zip with a member outside its folder is refused whole,
and reading a binary file as text fails.

It does not run anything. `power` flips a flag and writes a few plausible
console lines; `install` lays down the files a loader installer would have
left (a `run.sh` and its args file for Forge/NeoForge) so the code that
reads them back has something real to read. Two hooks exist for tests:
`fail_fetch` / `fail_run` make the next install step fail, and
`inject(server_id, line)` appends a console line.

With `seed=True` it starts with two servers -- "Sakura SMP" (NeoForge
1.21.1, running, three players) and "Paper Survival" (Paper 1.21.4, stopped)
-- under the ids `aaaa1111` and `bbbb2222` that the HTTP tests use.
"""
from __future__ import annotations

import collections
import io
import json
import posixpath
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator

from app.engine.base import (EngineError, power_action, required_java_major)


def _clean(path: str | None) -> list[str]:
    """Server-relative path -> parts, refusing anything that leaves the folder."""
    raw = (path or ".").replace("\\", "/").strip()
    if "\x00" in raw:
        raise EngineError("that path contains a null byte", 400)
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise EngineError("only paths inside the server folder are allowed", 400)
    parts = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise EngineError("that path points outside the server folder", 400)
        parts.append(part)
    return parts


class _Dir(dict):
    """A directory: name -> _Dir | _File. Its own mtime rides along."""

    def __init__(self):
        super().__init__()
        self.mtime = time.time()


class _File:
    __slots__ = ("data", "size", "mtime")

    def __init__(self, data: bytes = b"", size: int | None = None):
        self.data = data
        # A fixture can claim a size without holding the bytes: a 1.8 MB jar
        # does not need 1.8 MB of RAM to be listed as one.
        self.size = len(data) if size is None else size
        self.mtime = time.time()


class MockEngine:
    name = "mock"

    def __init__(self, *, seed: bool = True, node: str = "mock"):
        self.node = node
        self.servers: dict[str, dict] = {}
        self.stats: dict[str, dict] = {}
        self.fs: dict[str, _Dir] = {}
        self.console: dict[str, collections.deque] = {}
        # Test hooks.
        self.fail_fetch: str | None = None
        self.fail_run: str | None = None
        self.fetched: list[str] = []
        self.installs: list[dict] = []
        self.commands: list[tuple[str, str]] = []
        if seed:
            _seed(self)

    # --- internals ---------------------------------------------------------

    def _record(self, server_id: str) -> dict:
        rec = self.servers.get(server_id)
        if rec is None:
            raise EngineError(f"no server {server_id}", 404)
        return rec

    def _root(self, server_id: str) -> _Dir:
        self._record(server_id)
        return self.fs.setdefault(server_id, _Dir())

    def _walk(self, server_id: str, parts: list[str], *, create: bool = False) -> _Dir:
        node: Any = self._root(server_id)
        for part in parts:
            nxt = node.get(part)
            if nxt is None:
                if not create:
                    raise EngineError(f"{'/'.join(parts)} does not exist", 404)
                nxt = node[part] = _Dir()
            if not isinstance(nxt, _Dir):
                raise EngineError(f"{part} is a file, not a folder", 409)
            node = nxt
        return node

    def _get(self, server_id: str, path: str) -> tuple[_Dir, str, Any]:
        parts = _clean(path)
        if not parts:
            raise EngineError("that is the server folder itself", 400)
        parent = self._walk(server_id, parts[:-1])
        return parent, parts[-1], parent.get(parts[-1])

    def _put(self, server_id: str, location: str, filename: str, data: bytes) -> None:
        if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
            raise EngineError(f"{filename!r} is not a file name", 400)
        folder = self._walk(server_id, _clean(location), create=True)
        if isinstance(folder.get(filename), _Dir):
            raise EngineError(f"{filename} is a folder", 409)
        folder[filename] = _File(data)
        folder.mtime = time.time()

    def _log(self, server_id: str, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.console.setdefault(server_id, collections.deque(maxlen=1000)).append(
            f"[{stamp}] [Server thread/INFO]: {line}")

    def inject(self, server_id: str, line: str) -> None:
        """Append a raw console line (a test seam, not part of the contract)."""
        self.console.setdefault(server_id, collections.deque(maxlen=1000)).append(line)

    # --- servers -------------------------------------------------------------

    async def list_servers(self) -> list[dict]:
        return [dict(r) for r in self.servers.values()]

    async def get_server(self, server_id: str) -> dict:
        return dict(self._record(server_id))

    async def get_stats(self, server_id: str) -> dict:
        self._record(server_id)
        return dict(self.stats.setdefault(server_id, _stopped()))

    async def create_server(self, *, name: str, port: int, memory_mb: int,
                            mc_version: str = "", loader: str = "",
                            java_major: int | None = None,
                            server_id: str | None = None,
                            limits: dict | None = None) -> str:
        sid = server_id or uuid.uuid4().hex
        if sid in self.servers:
            raise EngineError(f"server {sid} already exists", 409)
        if any(r.get("server_port") == port for r in self.servers.values()):
            raise EngineError(f"port {port} is already taken on this node", 409)
        self.servers[sid] = {
            "server_id": sid, "server_name": name, "server_port": int(port),
            "executable": "", "execution_command": "",
            "java_major": java_major or (required_java_major(mc_version)
                                         if mc_version else None),
            "memory_mb": int(memory_mb), "node": self.node,
            "path": f"/srv/blessforge/servers/{sid}", "type": "minecraft-java",
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "auto_start": False, "limits": dict(limits or {}),
        }
        self.fs[sid] = _Dir()
        self.stats[sid] = _stopped()
        self.console[sid] = collections.deque(maxlen=1000)
        return sid

    async def delete_server(self, server_id: str, delete_files: bool = True) -> None:
        self._record(server_id)
        # Deleting a running server stops it first, as the agent does.
        await self.power(server_id, "kill")
        self.servers.pop(server_id, None)
        self.stats.pop(server_id, None)
        self.console.pop(server_id, None)
        if delete_files:
            self.fs.pop(server_id, None)

    _UPDATABLE = {"server_name", "server_port", "executable", "execution_command",
                  "java_major", "memory_mb", "auto_start", "limits"}

    async def update_server(self, server_id: str, fields: dict) -> None:
        rec = self._record(server_id)
        unknown = set(fields) - self._UPDATABLE
        if unknown:
            raise EngineError(f"cannot change {', '.join(sorted(unknown))}", 400)
        if "server_port" in fields:
            port = int(fields["server_port"])
            if any(r.get("server_port") == port and k != server_id
                   for k, r in self.servers.items()):
                raise EngineError(f"port {port} is already taken on this node", 409)
            fields = {**fields, "server_port": port}
        rec.update(fields)

    async def power(self, server_id: str, action: str) -> None:
        self._record(server_id)
        action = power_action(action)
        st = self.stats.setdefault(server_id, _stopped())
        if action in ("stop", "kill"):
            if st.get("running"):
                self._log(server_id, "Stopping the server" if action == "stop"
                          else "(killed)")
            st.update(_stopped(max=st.get("max")))
            return
        if action == "restart" and st.get("running"):
            self._log(server_id, "Stopping the server")
        if not self.servers[server_id].get("execution_command"):
            raise EngineError("this server has no startup command yet", 409)
        self._log(server_id, "Starting minecraft server")
        self._log(server_id, 'Done (1.042s)! For help, type "help"')
        st.update(running=True, crashed=False, started=time.time(), cpu=12.0,
                  mem=1_200_000_000, mem_percent=30.0, online=0)

    async def install(self, server_id: str, spec: dict) -> dict:
        """Lay down what an installer would leave. No network, no processes."""
        self._record(server_id)
        output: list[str] = []
        fetch = spec.get("fetch") or None
        if fetch:
            if self.fail_fetch:
                why, self.fail_fetch = self.fail_fetch, None
                raise EngineError(f"download of {fetch.get('url')} failed: {why}", 502)
            self.fetched.append(fetch.get("url") or "")
            name = fetch.get("filename") or posixpath.basename(fetch.get("url") or "x.jar")
            self._put(server_id, ".", name, b"PK\x05\x06" + b"\x00" * 18)
            output.append(f"downloaded {name}")
        run = spec.get("run") or None
        if run:
            if self.fail_run:
                why, self.fail_run = self.fail_run, None
                raise EngineError(f"the installer exited with an error: {why}", 500,
                                  {"output": [why]})
            output.extend(self._simulate_installer(server_id, spec))
        for name in spec.get("remove_after") or []:
            try:
                await self.delete_paths(server_id, [name])
            except EngineError:
                pass
        self.installs.append(dict(spec))
        root = self._root(server_id)
        return {"ok": True, "files": sorted(root), "output": output}

    def _simulate_installer(self, server_id: str, spec: dict) -> list[str]:
        family = (spec.get("family") or "").lower()
        mc = spec.get("mc_version") or ""
        ver = spec.get("loader_version") or "0.0.0"
        if family not in ("forge", "neoforge"):
            return ["nothing to run for this loader"]
        legacy = family == "forge" and required_java_major(mc) == 8
        if legacy:
            # Before 1.17 the installer leaves a runnable jar, not a run.sh.
            self._put(server_id, ".", f"forge-{mc}-{ver}.jar", b"PK\x05\x06" + b"\x00" * 18)
            self._walk(server_id, ["libraries"], create=True)
            return [f"installed forge-{mc}-{ver}.jar"]
        group = ("net/neoforged/neoforge" if family == "neoforge"
                 else "net/minecraftforge/forge")
        full = ver if family == "neoforge" else f"{mc}-{ver}"
        lib = f"libraries/{group}/{full}"
        self._put(server_id, lib, "unix_args.txt", b"-cp libraries/...\n")
        self._put(server_id, lib, f"{family}-{full}-server.jar", b"PK\x05\x06" + b"\x00" * 18)
        self._put(server_id, ".", "run.sh",
                  f'#!/usr/bin/env sh\njava @user_jvm_args.txt @{lib}/unix_args.txt "$@"\n'
                  .encode())
        self._put(server_id, ".", "run.bat",
                  f'java @user_jvm_args.txt @{lib}/win_args.txt %*\n'.encode())
        root = self._root(server_id)
        if "user_jvm_args.txt" not in root:
            self._put(server_id, ".", "user_jvm_args.txt",
                      b"# Xmx and Xms set the maximum and minimum RAM usage\n")
        return [f"The server installed successfully ({family} {full})"]

    async def set_runtime(self, server_id: str, mc_version: str) -> dict:
        rec = self._record(server_id)
        want = required_java_major(mc_version)
        old = rec.get("java_major")
        rec["java_major"] = want
        return {"changed": old != want, "java_major": want,
                "java_path": f"java {want} runtime",
                **({} if old != want else {"reason": "already correct"})}

    # --- files -----------------------------------------------------------

    async def list_dir(self, server_id: str, path: str = ".") -> dict:
        parts = _clean(path)
        folder = self._walk(server_id, parts)
        base = "/".join(parts)
        out = {}
        for name, node in folder.items():
            rel = f"{base}/{name}" if base else name
            if isinstance(node, _Dir):
                out[name] = {"path": rel, "dir": True, "size": 0,
                             "modified": node.mtime}
            else:
                out[name] = {"path": rel, "dir": False, "size": node.size,
                             "modified": node.mtime}
        return out

    async def read_file(self, server_id: str, path: str) -> str:
        _, name, node = self._get(server_id, path)
        if node is None:
            raise EngineError(f"{path} does not exist", 404)
        if isinstance(node, _Dir):
            raise EngineError(f"{path} is a folder", 409)
        try:
            return node.data.decode("utf-8")
        except UnicodeDecodeError:
            raise EngineError(f"{path} is not a readable text file", 415)

    async def write_file(self, server_id: str, path: str, contents: str) -> None:
        parent, name, node = self._get(server_id, path)
        if isinstance(node, _Dir):
            raise EngineError(f"{path} is a folder", 409)
        parent[name] = _File(contents.encode("utf-8"))
        parent.mtime = time.time()

    async def delete_paths(self, server_id: str, rel_paths: list[str]) -> None:
        for rel in rel_paths:
            parent, name, node = self._get(server_id, rel)
            if node is None:
                raise EngineError(f"{rel} does not exist", 404)
            del parent[name]
            parent.mtime = time.time()

    async def rename_path(self, server_id: str, path: str, new_name: str) -> None:
        if "/" in new_name or "\\" in new_name or new_name in ("", ".", ".."):
            raise EngineError(f"{new_name!r} is not a name", 400)
        parent, name, node = self._get(server_id, path)
        if node is None:
            raise EngineError(f"{path} does not exist", 404)
        if new_name in parent:
            raise EngineError(f"{new_name} already exists", 409)
        parent[new_name] = parent.pop(name)

    async def move_path(self, server_id: str, path: str, dest_dir: str) -> None:
        parent, name, node = self._get(server_id, path)
        if node is None:
            raise EngineError(f"{path} does not exist", 404)
        dest = self._walk(server_id, _clean(dest_dir))
        if dest is node:
            raise EngineError("cannot move a folder into itself", 400)
        if name in dest:
            raise EngineError(f"{name} already exists in {dest_dir}", 409)
        dest[name] = parent.pop(name)

    async def create_entry(self, server_id: str, parent: str, name: str,
                           directory: bool = False) -> None:
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise EngineError(f"{name!r} is not a name", 400)
        folder = self._walk(server_id, _clean(parent))
        existing = folder.get(name)
        if existing is not None:
            if isinstance(existing, _Dir) != bool(directory):
                raise EngineError(f"{name} already exists as a "
                                  f"{'folder' if isinstance(existing, _Dir) else 'file'}",
                                  409)
            return
        folder[name] = _Dir() if directory else _File(b"")

    async def upload_file(self, server_id: str, location: str, filename: str,
                          payload: bytes) -> None:
        self._put(server_id, location, filename, bytes(payload))

    async def upload_path(self, server_id: str, location: str, filename: str,
                          src: Any) -> None:
        self._put(server_id, location, filename, Path(src).read_bytes())

    async def extract(self, server_id: str, rel_zip_path: str) -> None:
        parent_parts = _clean(rel_zip_path)[:-1]
        data = (await self.download_file(server_id, rel_zip_path))
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise EngineError(f"{rel_zip_path} is not a zip: {e}", 400)
        members = zf.infolist()
        # Checked before anything is written: an archive with one bad member
        # is refused whole, rather than half-extracted.
        for info in members:
            try:
                _clean(info.filename)
            except EngineError:
                raise EngineError(
                    f"{rel_zip_path} has a member outside its folder "
                    f"({info.filename!r}); nothing was extracted", 400)
        for info in members:
            parts = _clean(info.filename)
            if not parts:
                continue
            if info.is_dir():
                self._walk(server_id, parent_parts + parts, create=True)
                continue
            self._put(server_id, "/".join(parent_parts + parts[:-1]) or ".",
                      parts[-1], zf.read(info))

    async def download_file(self, server_id: str, rel_path: str) -> bytes:
        _, _, node = self._get(server_id, rel_path)
        if node is None:
            raise EngineError(f"{rel_path} does not exist", 404)
        if isinstance(node, _Dir):
            raise EngineError(f"{rel_path} is a folder", 409)
        return node.data

    async def stream_file(self, server_id: str, rel_path: str) -> AsyncIterator[bytes]:
        data = await self.download_file(server_id, rel_path)
        for i in range(0, len(data), 256 * 1024):
            yield data[i:i + 256 * 1024]
        if not data:
            yield b""

    # --- console ---------------------------------------------------------

    async def console_lines(self, server_id: str, *, from_file: bool = False
                            ) -> list[str]:
        self._record(server_id)
        if from_file:
            try:
                return (await self.read_file(server_id, "logs/latest.log")).splitlines()
            except EngineError:
                return []
        return list(self.console.get(server_id) or [])

    async def send_command(self, server_id: str, command: str) -> dict:
        self._record(server_id)
        if not (self.stats.get(server_id) or {}).get("running"):
            raise EngineError("the server is not running, so it cannot accept commands",
                              409)
        self.commands.append((server_id, command))
        self._log(server_id, f"(mock) ran: {command}")
        return {"sent": command}


def _stopped(**over) -> dict:
    out = {"running": False, "crashed": False, "cpu": 0.0, "mem": 0,
           "mem_percent": 0.0, "online": 0, "max": 20, "players": [],
           "world_name": "world", "world_size": None, "started": None}
    out.update({k: v for k, v in over.items() if v is not None})
    return out


# --- the two seeded servers ---------------------------------------------------

_MANIFEST_A = {
    "schema": 1, "complete": True, "installed_at": 0,
    "pack": {"source": "curseforge", "name": "Cozy Sakura Adventure",
             "version": "1.4.2", "install_source": "manifest"},
    "minecraft": "1.21.1", "loader": "neoforge", "loader_version": "21.1.209",
    "recommended_ram_mb": 6144,
    "mods": [
        {"file": "mods/jei-1.21.1-19.21.0.jar", "name": "Just Enough Items",
         "source": "curseforge", "project_id": 238222, "file_id": 5555,
         "version": "19.21.0"},
        {"file": "mods/sophisticatedcore-1.21.1-1.2.jar",
         "name": "Sophisticated Core", "source": "curseforge",
         "project_id": 618298, "file_id": 5556, "version": "1.2"},
        {"file": "mods/figura-0.1.4.jar", "name": "Figura", "source": "modrinth",
         "project_id": "Nk1jHUcH", "file_id": "abc", "version": "0.1.4",
         "client_only": True,
         "client_only_reasons": ["the jar declares environment=client"]},
    ],
    "problems": [],
}
_MANIFEST_B = {
    "schema": 1, "complete": True, "installed_at": 0,
    "pack": {"name": "Paper Survival", "source": "empty",
             "install_source": "blank", "version": "paper 1.21.4"},
    "minecraft": "1.21.4", "loader": "paper", "mod_directory": "plugins",
    "mods": [], "problems": [],
}

_LOG = [
    "[12:01:02] [main/INFO]: Starting minecraft server version 1.21.1",
    "[12:01:04] [main/INFO]: Loading 214 mods",
    "[12:01:31] [main/WARN]: Mod figura is client-only and was skipped",
    "[12:02:10] [Server thread/INFO]: Preparing level \"world\"",
    "[12:02:44] [Server thread/INFO]: Done (41.522s)! For help, type \"help\"",
    "[12:14:03] [Server thread/INFO]: Notch joined the game",
    "[12:14:09] [Server thread/INFO]: <Notch> anyone seen my pickaxe",
    "[12:15:41] [Server thread/INFO]: Alex joined the game",
    "[12:19:02] [Server thread/ERROR]: Encountered an unexpected exception",
    "[12:31:00] [Server thread/INFO]: There are 3 of a max of 20 players online: "
    "Notch, Alex, Steve",
]


def _seed(m: MockEngine) -> None:
    now = time.time()
    m.servers["aaaa1111"] = {
        "server_id": "aaaa1111", "server_name": "Sakura SMP", "server_port": 25565,
        "executable": "libraries/net/neoforged/neoforge/21.1.209/neoforge-21.1.209-server.jar",
        "execution_command": "java @user_jvm_args.txt "
                             "@libraries/net/neoforged/neoforge/21.1.209/unix_args.txt nogui",
        "java_major": 21, "memory_mb": 8192, "node": m.node,
        "path": "/srv/blessforge/servers/aaaa1111", "type": "minecraft-java",
        "created": "2026-08-01T00:00:00Z", "auto_start": True, "limits": {},
    }
    m.servers["bbbb2222"] = {
        "server_id": "bbbb2222", "server_name": "Paper Survival", "server_port": 25566,
        "executable": "paper-1.21.4-232.jar",
        "execution_command": "java -Xms2G -Xmx4G -jar paper-1.21.4-232.jar nogui",
        "java_major": 21, "memory_mb": 4096, "node": m.node,
        "path": "/srv/blessforge/servers/bbbb2222", "type": "minecraft-java",
        "created": "2026-08-20T00:00:00Z", "auto_start": False, "limits": {},
    }
    m.stats["aaaa1111"] = {
        "running": True, "crashed": False, "cpu": 41.5, "mem": 3_650_000_000,
        "mem_percent": 46.2, "online": 3, "max": 20,
        "players": ["Notch", "Alex", "Steve"], "world_name": "world",
        "world_size": "1.4 GB", "started": now - 7400,
    }
    m.stats["bbbb2222"] = _stopped(max=40)
    m.stats["bbbb2222"]["world_size"] = "312 MB"
    m.console["aaaa1111"] = collections.deque(_LOG, maxlen=1000)
    m.console["bbbb2222"] = collections.deque(maxlen=1000)

    a = dict(_MANIFEST_A, installed_at=now - 86400 * 3)
    b = dict(_MANIFEST_B, installed_at=now - 3600)
    text_a = {
        "server.properties": (
            "#Minecraft server properties\nmotd=A Sakura server\n"
            "server-port=25565\nquery.port=25565\ndifficulty=normal\n"
            "gamemode=survival\nmax-players=20\nonline-mode=true\npvp=true\n"
            "white-list=true\nenforce-whitelist=false\nview-distance=10\n"
            "simulation-distance=8\nspawn-protection=16\n"),
        "eula.txt": "eula=true\n",
        "user_jvm_args.txt": "# Managed\n-Xms3G\n-Xmx6G\n-XX:+UseG1GC\n",
        ".blessforge.json": json.dumps(a, indent=2),
        "whitelist.json": json.dumps([
            {"uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5", "name": "Notch"},
            {"uuid": "ec561538-f3fd-461d-aff5-086b22154bce", "name": "Alex"},
        ], indent=2),
        "ops.json": json.dumps([
            {"uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5", "name": "Notch",
             "level": 4, "bypassesPlayerLimit": False}], indent=2),
        "banned-players.json": json.dumps([
            {"uuid": "11111111-2222-3333-4444-555555555555", "name": "Griefer",
             "created": "2026-08-14 12:00:00 +0000", "source": "Server",
             "expires": "forever", "reason": "broke the spawn"}], indent=2),
        "banned-ips.json": "[]",
        "usercache.json": json.dumps([
            {"name": "Notch", "uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5",
             "expiresOn": "2026-10-01 00:00:00 +0000"},
            {"name": "Alex", "uuid": "ec561538-f3fd-461d-aff5-086b22154bce",
             "expiresOn": "2026-10-01 00:00:00 +0000"},
            {"name": "Steve", "uuid": "8667ba71-b85a-4004-af54-457a9734eed7",
             "expiresOn": "2026-10-01 00:00:00 +0000"},
            {"name": "Griefer", "uuid": "11111111-2222-3333-4444-555555555555",
             "expiresOn": "2026-09-01 00:00:00 +0000"}], indent=2),
        "config/sophisticatedcore-common.toml":
            "[general]\n\t# how many upgrades\n\tmaxUpgrades = 5\n",
        "config/jei/jei-client.ini": "[advanced]\nlowMemorySlowSearchEnabled=false\n",
        "logs/latest.log": "\n".join(_LOG) + "\n",
    }
    binary_a = {
        "mods/jei-1.21.1-19.21.0.jar": 1_887_436,
        "mods/sophisticatedcore-1.21.1-1.2.jar": 2_516_582,
        "mods/figura-0.1.4.jar.disabled": 5_347_738,
        "world/level.dat": 12_288,
        "world/region/r.0.0.mca": 8_808_038,
    }
    text_b = {
        "server.properties": (
            "motd=Paper Survival\nserver-port=25566\ndifficulty=easy\n"
            "gamemode=survival\nmax-players=40\nonline-mode=true\n"
            "white-list=false\npvp=false\n"),
        "eula.txt": "eula=true\n",
        ".blessforge.json": json.dumps(b, indent=2),
    }
    for sid, texts, blobs, dirs in (
        ("aaaa1111", text_a, binary_a, ["mods", "config", "world", "logs", "libraries"]),
        ("bbbb2222", text_b, {}, ["plugins", "logs"]),
    ):
        m.fs[sid] = _Dir()
        for d in dirs:
            m._walk(sid, [d], create=True)
        for rel, text in texts.items():
            parts = rel.split("/")
            m._put(sid, "/".join(parts[:-1]) or ".", parts[-1], text.encode())
        for rel, size in blobs.items():
            parts = rel.split("/")
            folder = m._walk(sid, parts[:-1], create=True)
            folder[parts[-1]] = _File(b"\x00binary", size=size)
