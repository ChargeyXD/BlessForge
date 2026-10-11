#!/usr/bin/env python3
"""Move every server off the old Crafty deployment onto BlessForge's own nodes.

Run once, on the panel host, with the panel's own environment (.env) -- the
servers are created through the same engine the panel uses, on its default
node:

    CRAFTY_URL=https://127.0.0.1:8443 CRAFTY_TOKEN=... \\
        python dev/tools/import_from_crafty.py --dry-run
    ... --from-dir /DATA/AppData/big-bear-crafty/servers    # copy from disk
    ... (no --from-dir)                                      # copy through Crafty's API

The token comes from the environment, never the command line (which lands
in shell history). `--from-dir` is Crafty's servers folder as this machine
sees it -- faster and exact, worlds included; without it every file is
downloaded through Crafty's API, one last time.

For each Crafty server: read its name, port, heap, loader and Minecraft
version (the .blessforge.json BlessForge wrote there, else the jar name);
create the matching server; copy its folder across; point its startup
command at the node's own Java (Crafty's names an absolute JVM path that
does not exist on a node); boot it once with the smoke test; and report.

Read-only towards Crafty: nothing there is stopped, edited or deleted. A
server already imported (same name and port, stamped in its manifest) is
skipped, so a run that stopped half-way can simply be run again.
Uninstalling Crafty is a separate, manual step: the report says when every
server has booted here and it is safe.

    --only ID      one Crafty server
    --no-boot      copy, but do not run the boot test
    --dry-run      say what would happen, change nothing
    --report FILE  also write the report as JSON
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import posixpath
import re
import stat
import sys
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

# Never copied: a world lock held by the old process, and Crafty's own temp.
SKIP_NAMES = {"session.lock"}
SKIP_SUFFIXES = (".tmp", ".lck")


class ImportError_(Exception):
    pass


# --- Crafty, read-only ---------------------------------------------------------


class Crafty:
    def __init__(self, url: str, token: str, verify: bool = False, transport=None):
        if not url or not token:
            raise ImportError_("set CRAFTY_URL and CRAFTY_TOKEN (in the environment)")
        self.c = httpx.AsyncClient(base_url=url.rstrip("/"), verify=verify, timeout=600,
                                   transport=transport,
                                   headers={"Authorization": f"Bearer {token}",
                                            "Accept": "application/json"})

    async def close(self) -> None:
        await self.c.aclose()

    async def _data(self, resp: httpx.Response, what: str):
        if resp.status_code >= 400:
            raise ImportError_(f"Crafty: {what} failed ({resp.status_code})")
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("status") == "error":
            raise ImportError_(f"Crafty: {what} failed ({payload.get('error')})")
        return payload.get("data") if isinstance(payload, dict) else payload

    async def servers(self) -> list[dict]:
        return await self._data(await self.c.get("/api/v2/servers"), "list servers") or []

    async def stats(self, sid: str) -> dict:
        try:
            return await self._data(await self.c.get(f"/api/v2/servers/{sid}/stats"),
                                    "stats") or {}
        except ImportError_:
            return {}

    async def listing(self, sid: str, path: str) -> dict:
        # POST, not GET: Crafty's handler only implements post().
        data = await self._data(await self.c.post(
            f"/api/v2/servers/{sid}/files", json={"page": path, "path": path}),
            f"list {path}")
        return {k: v for k, v in (data or {}).items() if k != "root_path"}

    async def text(self, sid: str, path: str) -> str | None:
        try:
            data = await self._data(await self.c.post(
                f"/api/v2/servers/{sid}/files", json={"page": path, "path": path}),
                f"read {path}")
        except ImportError_:
            return None
        return data.get("content") if isinstance(data, dict) else None

    async def download(self, sid: str, path: str) -> bytes:
        quoted = urllib.parse.quote(path, safe="")
        r = await self.c.get(f"/api/v2/servers/{sid}/files/{quoted}/download")
        if r.status_code >= 400:
            raise ImportError_(f"Crafty: download {path} failed ({r.status_code})")
        return r.content


# --- what a Crafty server is -------------------------------------------------------

_XMX = re.compile(r"-Xmx(\d+(?:\.\d+)?)([gGmM])")
_JAR_HINTS = [
    (re.compile(r"neoforge-(\d+\.\d+\.\d+)"), "neoforge"),
    (re.compile(r"forge-(\d+\.\d+(?:\.\d+)?)-"), "forge"),
    (re.compile(r"fabric-server-mc\.(\d+\.\d+(?:\.\d+)?)"), "fabric"),
    (re.compile(r"folia-(\d+\.\d+(?:\.\d+)?)"), "folia"),
    (re.compile(r"purpur-(\d+\.\d+(?:\.\d+)?)"), "purpur"),
    (re.compile(r"paper-(\d+\.\d+(?:\.\d+)?)"), "paper"),
    (re.compile(r"(?:minecraft_)?server-(\d+\.\d+(?:\.\d+)?)\.jar"), "vanilla"),
]


def heap_gb(command: str, jvm_args: str | None) -> float:
    for text in (jvm_args or "", command or ""):
        m = _XMX.search(text)
        if m:
            n = float(m.group(1))
            return n if m.group(2).lower() == "g" else round(n / 1024, 2)
    return 2.0


def loader_and_version(server: dict, manifest: dict) -> tuple[str, str]:
    """From BlessForge's own manifest when there is one, else from the jar name."""
    from app import loaders
    loader = loaders.family_of(manifest.get("loader") or "") if manifest else ""
    mc = (manifest or {}).get("minecraft") or ""
    if loader and mc:
        return loader, mc
    text = f"{server.get('executable') or ''} {server.get('execution_command') or ''}"
    for pattern, family in _JAR_HINTS:
        m = pattern.search(text)
        if m:
            version = m.group(1)
            if family == "neoforge":
                version = loaders._neoforge_mc(version) or version
            return loader or family, mc or version
    return loader or "vanilla", mc


def node_command(command: str) -> str:
    """Crafty's command with its absolute JVM path made plain `java`.

    On a node the runtime image (or the agent's java_paths) decides which
    Java `java` is; a path like /usr/lib/jvm/java-21-openjdk-amd64/bin/java
    exists on the Crafty box and nowhere else.
    """
    from app.engine import java_in_command
    found = java_in_command(command)
    path = found.get("path")
    if path and path != "java":
        command = command.replace(path, "java", 1)
    return re.sub(r"^\s*[\"']?java[\"']?\s", "java ", command)


# --- the files ---------------------------------------------------------------------


def _skip(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES)


def _is_link(path: str) -> bool:
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda _p: False)(path)


def local_files(folder: pathlib.Path):
    """(relative dir, name, path) for every regular file; links are not followed."""
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        rel = pathlib.Path(dirpath).relative_to(folder).as_posix()
        # A junction is not a "link" to os.path.islink, and os.walk walks
        # straight into one; both kinds are left out.
        dirnames[:] = [d for d in dirnames if not _is_link(os.path.join(dirpath, d))]
        for name in filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            if stat.S_ISREG(st.st_mode) and not _skip(name):
                yield ("." if rel == "." else rel), name, full


async def api_files(crafty: Crafty, sid: str, path: str = "."):
    for name, meta in sorted((await crafty.listing(sid, path)).items()):
        rel = name if path == "." else f"{path}/{name}"
        if meta.get("dir"):
            async for item in api_files(crafty, sid, rel):
                yield item
        elif not _skip(name):
            yield path, name, rel


# --- one server ------------------------------------------------------------------


async def import_one(crafty: Crafty, server: dict, args, existing: list[dict]) -> dict:
    from app import engine, specs, templates
    from app.jobs import Job
    from app import smoketest

    sid = str(server.get("server_id"))
    name = str(server.get("server_name") or sid)
    port = int(server.get("server_port") or 25565)
    report: dict = {"crafty_id": sid, "name": name, "port": port}

    manifest = {}
    raw = await crafty.text(sid, ".blessforge.json")
    if raw:
        try:
            manifest = json.loads(raw)
        except ValueError:
            manifest = {}
    jvm_args = await crafty.text(sid, "user_jvm_args.txt")
    loader, mc = loader_and_version(server, manifest)
    heap = heap_gb(server.get("execution_command") or "", jvm_args)
    command = node_command(server.get("execution_command") or "")
    report.update(loader=loader, minecraft=mc, heap_gb=heap, command=command)

    already = next((s for s in existing if s.get("server_name") == name
                    and int(s.get("server_port") or 0) == port), None)
    if already is not None:
        try:
            stamped = (await engine.read_manifest(already["server_id"])).get("imported_from")
        except Exception:       # noqa: BLE001
            stamped = None
        if stamped and stamped.get("crafty") == sid:
            return {**report, "outcome": "skipped", "server_id": already["server_id"],
                    "why": "already imported"}
        return {**report, "outcome": "failed",
                "why": f"a server named {name!r} already uses port {port} here"}
    if any(int(s.get("server_port") or 0) == port for s in existing):
        return {**report, "outcome": "failed",
                "why": f"port {port} is already used by another server here"}
    if not mc:
        return {**report, "outcome": "failed",
                "why": "could not tell which Minecraft version it runs"}
    if args.dry_run:
        return {**report, "outcome": "would import"}

    java = await templates.java_major(loader, mc)
    new_id = await engine.create_server(
        name=name, port=port, memory_mb=specs.container_memory_mb(heap),
        mc_version=mc, loader=loader, java_major=java)
    report["server_id"] = new_id
    copied = size = 0
    try:
        if args.from_dir:
            folder = pathlib.Path(args.from_dir) / posixpath.basename(
                str(server.get("path") or sid).rstrip("/"))
            if not folder.is_dir():
                raise ImportError_(f"{folder} is not a folder")
            for rel_dir, fname, full in local_files(folder):
                data = pathlib.Path(full).read_bytes()
                await engine.upload_file(new_id, rel_dir, fname, data)
                copied += 1
                size += len(data)
        else:
            async for rel_dir, fname, rel in api_files(crafty, sid):
                data = await crafty.download(sid, rel)
                await engine.upload_file(new_id, rel_dir, fname, data)
                copied += 1
                size += len(data)
        executable = server.get("executable") or ""
        await engine.update_server(new_id, {"execution_command": command,
                                            "executable": executable})
        await engine.set_runtime(new_id, mc, java)
        await engine.write_file(new_id, "eula.txt", engine.EULA_ACCEPTED)
        await engine.write_manifest(new_id, {
            **(manifest or {"schema": 1, "complete": True, "minecraft": mc,
                            "loader": loader, "pack": {"name": name, "source": "crafty"}}),
            "imported_from": {"crafty": sid, "at": time.time(),
                              "source": "disk" if args.from_dir else "api"},
        })
    except Exception as e:      # noqa: BLE001 -- reported, the next server continues
        return {**report, "outcome": "failed", "files": copied,
                "why": f"copying failed after {copied} files: {e}"}
    report.update(files=copied, bytes=size)

    if args.no_boot:
        return {**report, "outcome": "copied"}
    job = Job("import", f"Boot test for {name}", server_id=new_id, server_name=name)
    boot = await smoketest.run(job, new_id, timeout=templates.boot(loader)["timeout"])
    return {**report, "outcome": "booted" if boot["outcome"] == "ready" else "failed",
            "boot": {k: boot[k] for k in ("outcome", "seconds")},
            **({} if boot["outcome"] == "ready" else
               {"why": f"the boot test ended '{boot['outcome']}'",
                "log_tail": boot["log_tail"][-1500:]})}


async def run(args, *, setup: bool = True, transport=None) -> dict:
    """Import every server. `setup=False` uses an engine the caller set up;
    `transport` reaches Crafty some other way (the test's in-process mock)."""
    from app import config, db, engine
    use_engine = not args.dry_run or args.connect_engine
    if setup and use_engine:
        await db.init()
        engine.configure()
        await engine.load_nodes()
    crafty = Crafty(os.environ.get("CRAFTY_URL", ""), os.environ.get("CRAFTY_TOKEN", ""),
                    verify=os.environ.get("CRAFTY_VERIFY_SSL", "").lower() in ("1", "true"),
                    transport=transport)
    try:
        servers = await crafty.servers()
        if args.only:
            servers = [s for s in servers if str(s.get("server_id")) == args.only]
        existing = await engine.list_servers() if use_engine else []
        results = []
        for server in servers:
            print(f"-- {server.get('server_name')} ({server.get('server_id')})", flush=True)
            try:
                result = await import_one(crafty, server, args, existing)
            except Exception as e:      # noqa: BLE001
                result = {"crafty_id": server.get("server_id"),
                          "name": server.get("server_name"), "outcome": "failed",
                          "why": str(e)}
            if result.get("server_id") and result["outcome"] not in ("skipped",):
                existing.append({"server_id": result["server_id"],
                                 "server_name": result["name"],
                                 "server_port": result.get("port")})
            print("   " + result["outcome"] + (f": {result['why']}" if result.get("why")
                                               else ""), flush=True)
            results.append(result)
    finally:
        await crafty.close()
        if setup and use_engine:
            await db.dispose()
    done = [r for r in results if r["outcome"] in ("booted", "skipped")]
    summary = {
        "engine": config.ENGINE, "servers": len(results), "ok": len(done),
        "results": results,
        # Only a full run, every server booted, ever says so.
        "crafty_can_go": bool(results) and len(done) == len(results)
        and not args.no_boot and not args.only and not args.dry_run,
    }
    print(f"\n{len(done)}/{len(results)} servers imported and booted.")
    print("Every server boots here: Crafty can be uninstalled (by hand, after a backup)."
          if summary["crafty_can_go"] else
          "Keep Crafty for now: not every server has been imported and booted.")
    if args.report:
        pathlib.Path(args.report).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--from-dir", help="Crafty's servers folder, as this machine sees it")
    p.add_argument("--only", help="import one Crafty server id")
    p.add_argument("--no-boot", action="store_true", help="skip the boot test")
    p.add_argument("--dry-run", action="store_true", help="change nothing")
    p.add_argument("--connect-engine", action="store_true",
                   help="with --dry-run, also check ports against the engine")
    p.add_argument("--report", help="write the report as JSON here")
    return p.parse_args(argv)


if __name__ == "__main__":
    summary = asyncio.run(run(parse()))
    sys.exit(0 if summary["ok"] == summary["servers"] else 1)
