"""The servers on this node: their state, their consoles, their lifecycles.

The panel says what each server should be (`put`), and this keeps it so:
the folder exists and belongs to the container user, the spec is on disk so
an agent restart remembers it, and start/stop/kill do what they say.

Lifecycle rules worth knowing:

  * **Stop is graceful.** "stop" is typed into the console, the server gets
    `stop_timeout` seconds to save and exit, and only then is it killed.
  * **A crash is seen, not hidden.** An exit nobody asked for is a crash: it
    is reported, and an auto-start server is started again after 5, then 15,
    then 45 seconds. Three crashes inside ten minutes is a crash loop: it is
    reported as one and the server is left stopped, because restarting a
    server that dies on boot forever only fills a log.
  * **The disk limit is enforced.** With project quotas (xfs/ext4) the
    kernel enforces it; with "watchdog" the folder is measured every minute
    and a server over its limit is warned in its own console, then stopped.
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import logging
import os
import re
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any

import httpx

from agent import files
from agent.config import AgentConfig
from agent.containers import RuntimeError_, argv_of, limits_of

log = logging.getLogger("blessforge.agent")

CONSOLE_LINES = 2000
CRASH_BACKOFF = (5, 15, 45)
CRASH_LOOP_WINDOW = 600
CRASH_LOOP_COUNT = 3
MAX_DOWNLOAD = 1024 * 1024 * 1024

_UUID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
_DONE = re.compile(r'Done \([0-9.]+s\)! For help')
_JOINED = re.compile(r"\]: ([A-Za-z0-9_]{2,16}) joined the game")
_LEFT = re.compile(r"\]: ([A-Za-z0-9_]{2,16}) left the game")
# The reply to `list`, both shapes:
#   There are 2 of a max of 20 players online: Alice, Bob     (1.13+)
#   There are 2/20 players online:                            (1.12 and older;
#   Alice, Bob                                                 names on the next line)
_LIST = re.compile(r"There are (\d+) ?(?:of a max(?: of)? (\d+)|/ ?(\d+)) players online:?(.*)$",
                   re.I)
_LIST_NAMES = re.compile(r"\]:\s*(.*)$")
_PLAYER = re.compile(r"^[A-Za-z0-9_]{1,16}$")
PING_EVERY = 30        # seconds between server list pings
LIST_EVERY = 120       # the most often `list` is sent to settle a disagreement


def check_uuid(value: str) -> str:
    if not _UUID.match(value or ""):
        raise RuntimeError_(f"{value!r} is not a server id", 400)
    return value


class Console:
    """A ring buffer of output lines and the websockets following it."""

    def __init__(self):
        self.lines: collections.deque[str] = collections.deque(maxlen=CONSOLE_LINES)
        self.subscribers: set[asyncio.Queue] = set()

    def feed(self, line: str) -> None:
        self.lines.append(line)
        for q in list(self.subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


class Server:
    def __init__(self, uuid: str, spec: dict):
        self.uuid = uuid
        self.spec = spec
        self.state = "offline"      # offline starting running stopping crashed installing
        self.running = None
        self.console = Console()
        self.started_at: float | None = None
        self.stopping = False
        self.crashes: collections.deque[float] = collections.deque(maxlen=10)
        self.players: set[str] = set()
        # When each online player joined, as seen by this agent. A player
        # found by a ping or a `list` rather than a join line gets the time
        # they were found, flagged as such.
        self.sessions: dict[str, dict] = {}
        self.player_source = "log"
        self.ping: dict | None = None
        self.pinged_at = 0.0
        self.listed_at = 0.0
        self.list_names_next = False
        self.restart_task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    def set_players(self, names: set[str], source: str) -> None:
        now = time.time()
        for gone in self.players - names:
            self.sessions.pop(gone, None)
        for name in names - self.players:
            self.sessions[name] = {"since": now, "exact": False}
        self.players = set(names)
        self.player_source = source

    def clear_players(self) -> None:
        self.players.clear()
        self.sessions.clear()
        self.ping = None


class Supervisor:
    def __init__(self, cfg: AgentConfig, runtime, events):
        self.cfg = cfg
        self.runtime = runtime
        self.events = events
        self.servers: dict[str, Server] = {}
        self.installs: dict[str, dict] = {}
        self.specs_dir = cfg.state_dir / "servers"

    # --- desired state -----------------------------------------------------------

    def load(self) -> None:
        self.specs_dir.mkdir(parents=True, exist_ok=True)
        for path in self.specs_dir.glob("*.json"):
            try:
                spec = json.loads(path.read_text(encoding="utf-8"))
                self.servers[spec["uuid"]] = Server(spec["uuid"], spec)
            except (ValueError, KeyError) as e:
                log.warning("ignoring %s: %s", path, e)

    def folder(self, uuid: str) -> Path:
        return self.cfg.server_dir(check_uuid(uuid))

    def get(self, uuid: str) -> Server:
        srv = self.servers.get(check_uuid(uuid))
        if srv is None:
            raise RuntimeError_(f"no server {uuid} on this node", 404)
        return srv

    def put(self, uuid: str, spec: dict) -> dict:
        """Create or update a server's spec. Idempotent."""
        uuid = check_uuid(uuid)
        clean = {
            "uuid": uuid,
            "name": str(spec.get("name") or uuid)[:120],
            "port": int(spec["port"]),
            "java_major": int(spec["java_major"]) if spec.get("java_major") else None,
            "execution_command": str(spec.get("execution_command") or ""),
            "memory_mb": int(spec.get("memory_mb") or 2048),
            "limits": dict(spec.get("limits") or {}),
            "auto_start": bool(spec.get("auto_start")),
        }
        if not 1024 <= clean["port"] <= 65535:
            raise RuntimeError_("the port must be between 1024 and 65535", 400)
        for other in self.servers.values():
            if other.uuid != uuid and other.spec.get("port") == clean["port"]:
                raise RuntimeError_(f"port {clean['port']} is already used on this node", 409)
        folder = self.folder(uuid)
        folder.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "chown"):
            try:
                os.chown(folder, self.cfg.container_uid, self.cfg.container_gid)
            except PermissionError:
                pass            # a non-root dev agent; the container user is us
        self.specs_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.specs_dir / f"{uuid}.json.tmp"
        tmp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
        tmp.replace(self.specs_dir / f"{uuid}.json")
        srv = self.servers.get(uuid)
        if srv is None:
            srv = self.servers[uuid] = Server(uuid, clean)
        else:
            srv.spec = clean
        return self.describe(srv)

    async def delete(self, uuid: str, delete_files: bool) -> None:
        srv = self.get(uuid)
        if srv.running is not None:
            srv.stopping = True
            await srv.running.kill()
        await self.runtime.remove(uuid)
        self.servers.pop(uuid, None)
        try:
            (self.specs_dir / f"{uuid}.json").unlink()
        except FileNotFoundError:
            pass
        if delete_files:
            folder = self.folder(uuid)
            if folder.exists():
                import shutil
                shutil.rmtree(folder)

    def describe(self, srv: Server) -> dict:
        return {**srv.spec, "state": srv.state,
                "running": srv.running is not None,
                "started_at": srv.started_at,
                "players": sorted(srv.players),
                "sessions": {n: srv.sessions.get(n) for n in sorted(srv.players)}}

    # --- power ------------------------------------------------------------------

    async def power(self, uuid: str, action: str) -> dict:
        srv = self.get(uuid)
        async with srv.lock:
            if action == "start":
                await self._start(srv)
            elif action == "stop":
                await self._stop(srv)
            elif action == "restart":
                await self._stop(srv)
                await self._start(srv)
            elif action == "kill":
                if srv.running is not None:
                    srv.stopping = True
                    await srv.running.kill()
            else:
                raise RuntimeError_(f"unknown power action {action!r}", 400)
        return self.describe(srv)

    async def _start(self, srv: Server) -> None:
        if srv.running is not None:
            return
        if srv.state == "installing":
            raise RuntimeError_("an install is running on this server", 409)
        argv_of(srv.spec.get("execution_command") or "")      # 409 if none
        srv.stopping = False
        srv.state = "starting"
        srv.clear_players()
        srv.console.feed(f"[BlessForge] Starting {srv.spec.get('name')}")

        def on_output(line: str) -> None:
            srv.console.feed(line)
            if srv.state == "starting" and _DONE.search(line):
                srv.state = "running"
                self.events.emit("status", srv.uuid, state="running")
            self._track_players(srv, line)

        async def on_exit(code: int | None) -> None:
            await self._exited(srv, code)

        try:
            srv.running = await self.runtime.start(srv.spec, self.folder(srv.uuid),
                                                   on_output, on_exit)
        except Exception:
            srv.state = "offline"
            raise
        srv.started_at = time.time()
        self.events.emit("status", srv.uuid, state="starting")

    async def _stop(self, srv: Server) -> None:
        if srv.running is None:
            return
        srv.stopping = True
        srv.state = "stopping"
        srv.console.feed("[BlessForge] Stopping")
        running = srv.running
        await running.stop(self.cfg.stop_timeout)
        for _ in range(100):
            if srv.running is None:
                return
            await asyncio.sleep(0.1)
        await running.kill()

    async def _exited(self, srv: Server, code: int | None) -> None:
        srv.running = None
        srv.started_at = None
        srv.clear_players()
        if srv.stopping:
            srv.state = "offline"
            srv.console.feed(f"[BlessForge] Stopped (exit code {code})")
            self.events.emit("status", srv.uuid, state="offline", exit_code=code)
            return
        now = time.time()
        srv.crashes.append(now)
        recent = [t for t in srv.crashes if now - t < CRASH_LOOP_WINDOW]
        srv.state = "crashed"
        srv.console.feed(f"[BlessForge] The server exited on its own (code {code})")
        if len(recent) >= CRASH_LOOP_COUNT:
            srv.console.feed("[BlessForge] It has crashed "
                             f"{len(recent)} times in {CRASH_LOOP_WINDOW // 60} minutes; "
                             "it will not be restarted until someone starts it.")
            self.events.emit("crash_loop", srv.uuid, exit_code=code, crashes=len(recent))
            return
        self.events.emit("crash", srv.uuid, exit_code=code, crashes=len(recent))
        if srv.spec.get("auto_start"):
            delay = CRASH_BACKOFF[min(len(recent) - 1, len(CRASH_BACKOFF) - 1)]
            srv.console.feed(f"[BlessForge] Restarting in {delay}s")

            async def later():
                await asyncio.sleep(delay)
                async with srv.lock:
                    if srv.running is None and srv.state == "crashed":
                        await self._start(srv)
            srv.restart_task = asyncio.create_task(later())

    async def command(self, uuid: str, line: str) -> None:
        srv = self.get(uuid)
        line = (line or "").strip()
        if not line:
            raise RuntimeError_("no command given", 400)
        if "\n" in line or "\r" in line:
            raise RuntimeError_("one command at a time", 400)
        if srv.running is None:
            raise RuntimeError_("the server is not running, so it cannot accept "
                                "commands", 409)
        srv.console.feed(f"> {line}")
        await srv.running.send(line)

    # --- who is online -------------------------------------------------------------

    def _track_players(self, srv: Server, line: str) -> None:
        """Join and leave lines: the instant signal. `list` replies: the fix."""
        if srv.list_names_next:
            srv.list_names_next = False
            m = _LIST_NAMES.search(line)
            names = {n.strip() for n in (m.group(1) if m else "").split(",")}
            srv.set_players({n for n in names if _PLAYER.match(n)}, "list")
            return
        m = _JOINED.search(line)
        if m:
            srv.players.add(m.group(1))
            srv.sessions[m.group(1)] = {"since": time.time(), "exact": True}
            self._players_moved(srv)
            return
        m = _LEFT.search(line)
        if m:
            srv.players.discard(m.group(1))
            srv.sessions.pop(m.group(1), None)
            self._players_moved(srv)
            return
        m = _LIST.search(line)
        if m:
            count = int(m.group(1))
            tail = (m.group(4) or "").strip()
            if count and not tail:
                srv.list_names_next = True      # 1.12: the names follow
                return
            names = {n.strip() for n in tail.split(",") if n.strip()}
            srv.set_players({n for n in names if _PLAYER.match(n)}, "list")

    @staticmethod
    def _players_moved(srv: Server) -> None:
        """Someone joined or left: the last ping's count is now wrong. It
        is marked stale, and the next look asks the server again. Kept, it read
        "0 online" for up to 30 seconds beside the names of two players."""
        if srv.ping:
            # Its slots and version still hold; only its count is stale.
            srv.ping = {**srv.ping, "stale": True}
        srv.pinged_at = 0.0
        srv.player_source = "log"

    async def _reconcile_players(self, srv: Server) -> None:
        """Ask the server itself, and settle any drift from missed join lines."""
        now = time.time()
        if srv.state != "running" or now - srv.pinged_at < PING_EVERY:
            return
        srv.pinged_at = now
        host = self.cfg.game_bind_ip
        if not host or host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        from agent.ping import ping
        result = await ping(host, int(srv.spec.get("port") or 25565))
        if result is None:
            return
        srv.ping = result
        if result["online"] == 0:
            if srv.players:
                srv.set_players(set(), "ping")
        elif len(result["sample"]) == result["online"]:
            if set(result["sample"]) != srv.players:
                srv.set_players(set(result["sample"]), "ping")
        elif result["online"] != len(srv.players) and now - srv.listed_at > LIST_EVERY:
            # The ping names only some (a dozen at most, or none where the
            # owner hides them); `list` names everyone. Sent straight to the
            # server, not echoed as a command, and at most every two minutes.
            srv.listed_at = now
            try:
                await srv.running.send("list")
            except Exception:       # noqa: BLE001
                pass

    async def stats(self, uuid: str) -> dict:
        srv = self.get(uuid)
        if srv.running is not None:
            try:
                await self._reconcile_players(srv)
            except Exception:       # noqa: BLE001 -- stats must still answer
                pass
        ping = srv.ping or {}
        online = (len(srv.players) if not ping or ping.get("stale")
                  else ping.get("online", len(srv.players)))
        if srv.ping and srv.ping["online"] != len(srv.players) and srv.player_source == "list":
            online = len(srv.players)       # a fresh `list` beats an older ping
        out: dict[str, Any] = {"running": srv.running is not None,
                               "state": srv.state, "crashed": srv.state == "crashed",
                               "started": srv.started_at, "online": online,
                               "max": (srv.ping or {}).get("max") or None,
                               "players": sorted(srv.players),
                               "sessions": {n: srv.sessions.get(n) for n in sorted(srv.players)},
                               "player_source": srv.player_source,
                               "version": (srv.ping or {}).get("version")}
        if srv.running is not None:
            out.update(await srv.running.stats())
        if out.get("cpu") is not None:
            # The runtimes count 100 per busy core; the panel shows a share
            # with "busy" at 60 and "straining" at 85. Raw, a 2-core server
            # flat out read 200% "straining" and a boot on 16 cores 1223%.
            # As a share of the cores this server may use, with the counts
            # kept for the detail.
            allowance = limits_of(srv.spec)["cpu_pct"]
            out["cpu_cores"] = round(out["cpu"] / 100, 2)
            out["cpu_allowance"] = round(allowance / 100, 2)
            out["cpu"] = round(min(100.0, out["cpu"] / allowance * 100), 1)
        if out.get("mem") is not None:
            out["mem_limit"] = limits_of(srv.spec)["memory_mb"] * 1024 * 1024
        if out.get("mem_percent") is None and out.get("mem"):
            out["mem_percent"] = round(out["mem"] / out["mem_limit"] * 100, 1)
        return out

    # --- installs -----------------------------------------------------------------

    def start_install(self, uuid: str, spec: dict) -> str:
        srv = self.get(uuid)
        if srv.running is not None:
            raise RuntimeError_("stop the server before installing into it", 409)
        job_id = uuidlib.uuid4().hex[:12]
        record = {"id": job_id, "status": "running", "output": [], "error": None,
                  "started": time.time()}
        self.installs[job_id] = record
        asyncio.create_task(self._install(srv, spec, record))
        return job_id

    async def _install(self, srv: Server, spec: dict, record: dict) -> None:
        def out(line: str) -> None:
            record["output"].append(line)
            del record["output"][:-400]
            srv.console.feed(f"[install] {line}")

        srv.state = "installing"
        self.events.emit("install", srv.uuid, phase="start")
        folder = self.folder(srv.uuid)
        try:
            fetch = spec.get("fetch")
            if fetch:
                await self._download(folder, fetch, out)
            run = spec.get("run")
            if run:
                if not isinstance(run, list) or not all(isinstance(a, str) for a in run):
                    raise RuntimeError_("run must be a list of strings", 400)
                server = {**srv.spec, "java_major": spec.get("java_major")
                          or srv.spec.get("java_major")}
                out(f"running {' '.join(run)}")
                code = await self.runtime.run_installer(
                    server, folder, run, int(spec.get("timeout") or 900), out)
                if code != 0:
                    raise RuntimeError_(f"the installer exited with code {code}", 500)
            for name in spec.get("remove_after") or []:
                try:
                    files.delete(folder, [name])
                except files.JailError:
                    pass
            record["files"] = sorted(files.list_dir(folder, "."))
            record["status"] = "done"
            self.events.emit("install", srv.uuid, phase="done")
        except Exception as e:          # noqa: BLE001 -- reported on the record
            record["status"] = "error"
            record["error"] = str(e)
            out(f"failed: {e}")
            self.events.emit("install", srv.uuid, phase="error", error=str(e))
        finally:
            record["finished"] = time.time()
            if srv.state == "installing":
                srv.state = "offline"

    async def _download(self, folder: Path, fetch: dict, out) -> None:
        url = str(fetch.get("url") or "")
        if not url.startswith(("https://", "http://")):
            raise RuntimeError_("only http(s) downloads are allowed", 400)
        name = files.check_name(fetch.get("filename") or url.rsplit("/", 1)[-1])
        out(f"downloading {name} from {url}")
        sha1, sha256 = hashlib.sha1(), hashlib.sha256()
        got = 0
        # Streamed into the jail's temp file; the checksum is compared before
        # the file is renamed into place, so a corrupt download never lands.
        with files.open_file(folder, name, "wb") as fh:
            async with httpx.AsyncClient(timeout=600, follow_redirects=True,
                                         headers={"User-Agent": "BlessForge-agent"}) as c:
                async with c.stream("GET", url) as r:
                    if r.status_code != 200:
                        raise RuntimeError_(f"{url} answered {r.status_code}", 502)
                    async for chunk in r.aiter_bytes(1024 * 1024):
                        got += len(chunk)
                        if got > MAX_DOWNLOAD:
                            raise RuntimeError_("that download is over 1 GB", 413)
                        sha1.update(chunk)
                        sha256.update(chunk)
                        fh.write(chunk)
            for algo, h in (("sha1", sha1), ("sha256", sha256)):
                want = (fetch.get(algo) or "").lower()
                if want and h.hexdigest() != want:
                    raise RuntimeError_(f"{name} is corrupt: its {algo} does not match",
                                        502)
        out(f"downloaded {name} ({got / 1048576:.1f} MB)")

    # --- the disk watchdog --------------------------------------------------------

    async def watch_disks(self) -> None:
        if self.cfg.disk_quota != "watchdog":
            return
        while True:
            await asyncio.sleep(60)
            await self.check_disks()

    async def check_disks(self) -> list[str]:
        """One pass: stop every running server over its disk limit."""
        stopped = []
        for srv in list(self.servers.values()):
            limit = limits_of(srv.spec)["disk_mb"]
            if not limit or srv.running is None:
                continue
            try:
                used = files.usage(self.folder(srv.uuid))["total_bytes"]
            except Exception:       # noqa: BLE001
                continue
            if used > limit * 1048576:
                srv.console.feed(f"[BlessForge] This server uses {used // 1048576} MB "
                                 f"of its {limit} MB disk. Stopping it.")
                self.events.emit("disk_full", srv.uuid, used_mb=used // 1048576,
                                 limit_mb=limit)
                await self.power(srv.uuid, "stop")
                stopped.append(srv.uuid)
        return stopped
