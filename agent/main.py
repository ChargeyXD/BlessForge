"""The BlessForge node agent: the /v1 API the panel drives.

    uvicorn agent.main:app --host <wireguard-ip> --port 8443
    (or: systemctl start blessforge-agent, see agent/systemd/)

Every route needs the node token, except the three a browser reaches with a
short-lived token the panel signed (agent/auth.py):

    GET  /v1/servers/{uuid}/ws        console and stats
    POST /v1/upload                   one file, into the path the token names
    GET  /v1/download                 one file, the one the token names

On start the agent reads its specs from disk and takes back every server
whose container kept running while it was away (an update or a restart
never stops a Docker server; see Supervisor.adopt_running), then asks the
panel for the desired state of every server on this node
(`/api/agent/servers`) and creates what is missing -- reporting, never
deleting, what the panel does not know about. Servers marked auto-start
that are not already running are started.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from agent import auth, clients, files
from agent.config import AgentConfig, load
from agent.containers import RuntimeError_, runtime_for
from agent.events import Events
from agent.servers import SAMPLE_EVERY, Supervisor, check_uuid

log = logging.getLogger("blessforge.agent")

VERSION = "0.5.0"
STATS_EVERY = 15
BROWSER_PATHS = ("/v1/upload", "/v1/download")

# Every file operation runs off the event loop -- a world delete or a backup
# compress on it froze every console, stats call and event flush on the
# node. Two small pools of their own: quick work (list, read, write, rename)
# never queues behind heavy work (copy, delete, extract, compress, a tree
# walk), and neither competes with the default executor uploads stream on.
_QUICK = ThreadPoolExecutor(4, thread_name_prefix="bf-files")
_HEAVY = ThreadPoolExecutor(2, thread_name_prefix="bf-files-heavy")
HEAVY_OPS = {"copy", "delete", "extract", "compress"}


async def quick(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_QUICK, fn, *args)


async def heavy(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(_HEAVY, fn, *args)


def create_app(cfg: AgentConfig | None = None) -> FastAPI:
    state: dict = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        c = cfg or load()
        c.servers_root.mkdir(parents=True, exist_ok=True)
        c.state_dir.mkdir(parents=True, exist_ok=True)
        events = Events(c)
        sup = Supervisor(c, runtime_for(c), events)
        sup.load()
        state.update(cfg=c, events=events, sup=sup,
                     tokens=auth.BrowserTokens(c.panel_public_key, c.node_id))
        # Before anything can auto-start them: the servers that kept running
        # while this agent was away (an update, a restart) are taken back.
        try:
            adopted = await asyncio.wait_for(sup.adopt_running(), 60)
        except asyncio.TimeoutError:
            adopted = []
            log.warning("re-attaching running servers took over 60 s; continuing")
        if adopted:
            log.info("re-attached %d running server(s)", len(adopted))
        tasks = [asyncio.create_task(events.run()),
                 asyncio.create_task(sup.watch_disks()),
                 asyncio.create_task(sup.sample_loop()),
                 asyncio.create_task(_stats_loop(sup, events)),
                 asyncio.create_task(_reconcile(c, sup, events))]
        events.emit("agent_started", version=VERSION)
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await sup.shutdown()
            await events.flush()
            await clients.close_all()

    app = FastAPI(title="BlessForge agent", version=VERSION, lifespan=lifespan)

    @app.middleware("http")
    async def node_token(request: Request, call_next):
        path = request.url.path
        if path.startswith("/v1/") and path not in BROWSER_PATHS:
            c: AgentConfig = state["cfg"]
            try:
                auth.check_node_token(request.headers.get("authorization"),
                                      c.token_id, c.token)
            except auth.Denied as e:
                return JSONResponse({"detail": str(e)}, status_code=e.status)
        return await call_next(request)

    @app.exception_handler(RuntimeError_)
    async def _runtime_error(_r, e: RuntimeError_):
        return JSONResponse({"detail": str(e), **e.extra}, status_code=e.status)

    @app.exception_handler(files.JailError)
    async def _jail_error(_r, e: files.JailError):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    def sup() -> Supervisor:
        return state["sup"]

    def folder(uuid: str) -> Path:
        sup().get(uuid)                 # 404 for a server this node has not got
        return sup().folder(uuid)

    # --- the node -------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"ok": True, "version": VERSION}

    @app.get("/v1/system")
    async def system():
        c: AgentConfig = state["cfg"]
        mem = available = load = None
        try:
            import psutil
            vm = psutil.virtual_memory()
            mem, available = vm.total, vm.available
            load = psutil.getloadavg()[0]
        except Exception:       # noqa: BLE001
            pass
        disk = shutil.disk_usage(c.servers_root)
        try:
            rt = await sup().runtime.info()
        except Exception as e:  # noqa: BLE001 -- a down Docker is worth saying
            rt = {"error": str(e)}
        return {"node": c.node_id, "agent_version": VERSION,
                "cpu_threads": os.cpu_count(), "ram_bytes": mem,
                "ram_available_bytes": available, "load_1m": load,
                "disk_total": disk.total, "disk_free": disk.free,
                "os": platform.platform(), "runtime": c.runtime, **rt,
                "servers": len(sup().servers),
                "running": sum(1 for s in sup().servers.values() if s.running)}

    # --- servers ----------------------------------------------------------------

    @app.get("/v1/servers")
    async def list_servers():
        return {"items": [sup().describe(s) for s in sup().servers.values()]}

    @app.get("/v1/stats")
    async def all_stats():
        """Every server's state, players, CPU and memory in one call, from the
        sampler's last reading -- what the fleet view asks for, once per node."""
        out = {}
        for uuid in list(sup().servers):
            try:
                out[uuid] = await sup().stats(uuid)
            except RuntimeError_:
                continue        # deleted while this was answering
        return {"servers": out, "sample_every": SAMPLE_EVERY}

    @app.get("/v1/servers/{uuid}")
    async def get_server(uuid: str):
        srv = sup().get(uuid)
        return {**sup().describe(srv), "stats": await sup().stats(uuid)}

    @app.put("/v1/servers/{uuid}")
    async def put_server(uuid: str, spec: dict = Body(...)):
        return sup().put(uuid, spec)

    @app.delete("/v1/servers/{uuid}")
    async def delete_server(uuid: str, files_too: bool = Query(True, alias="files")):
        await sup().delete(uuid, files_too)
        return {"deleted": uuid}

    @app.post("/v1/servers/{uuid}/power")
    async def power(uuid: str, body: dict = Body(...)):
        return await sup().power(uuid, str(body.get("action") or ""))

    @app.post("/v1/servers/{uuid}/command")
    async def command(uuid: str, body: dict = Body(...)):
        await sup().command(uuid, str(body.get("command") or ""))
        return {"sent": body.get("command")}

    @app.get("/v1/servers/{uuid}/console")
    async def console(uuid: str, lines: int = 500, after: int | None = None):
        """The last `lines` lines, or with `after` only the lines numbered
        after it (and `reset` when that one has left the ring). `seq` is the
        newest line's number, for the next `after`."""
        srv = sup().get(uuid)
        if after is not None:
            fresh, reset = srv.console.since(after)
            return {"lines": fresh, "seq": srv.console.seq, "reset": reset}
        return {"lines": list(srv.console.lines)[-max(1, min(lines, 2000)):],
                "seq": srv.console.seq}

    @app.post("/v1/servers/{uuid}/install")
    async def install(uuid: str, spec: dict = Body(...)):
        return {"job_id": sup().start_install(uuid, spec)}

    @app.get("/v1/servers/{uuid}/install/{job_id}")
    async def install_status(uuid: str, job_id: str):
        sup().get(uuid)
        rec = sup().installs.get(job_id)
        if rec is None:
            raise RuntimeError_("no such install", 404)
        return rec

    # --- files ------------------------------------------------------------------

    @app.get("/v1/servers/{uuid}/files")
    async def list_files(uuid: str, path: str = "."):
        return {"entries": await quick(files.list_dir, folder(uuid), path)}

    @app.get("/v1/servers/{uuid}/files/content")
    async def read_content(uuid: str, path: str, tail: int = 0):
        if tail > 0:
            return {"content": await quick(files.read_tail, folder(uuid), path, tail)}
        return {"content": await quick(files.read_text, folder(uuid), path)}

    @app.put("/v1/servers/{uuid}/files/content")
    async def write_content(uuid: str, path: str, body: dict = Body(...)):
        await quick(files.write_bytes, folder(uuid), path,
                    str(body.get("content") or "").encode())
        return {"written": path}

    @app.get("/v1/servers/{uuid}/files/raw")
    async def read_raw(uuid: str, path: str):
        root = folder(uuid)
        await quick(files.check_readable, root, path)
        return StreamingResponse(files.iter_file(root, path),
                                 media_type="application/octet-stream")

    @app.put("/v1/servers/{uuid}/files/raw")
    async def write_raw(uuid: str, path: str, request: Request):
        size = await _receive(folder(uuid), path, request)
        return {"written": path, "bytes": size}

    @app.post("/v1/servers/{uuid}/files/{op}")
    async def file_op(uuid: str, op: str, body: dict = Body(...)):
        root = folder(uuid)
        run = heavy if op in HEAVY_OPS else quick
        if op == "rename":
            return {"path": await run(files.rename, root, body["path"], body["name"])}
        if op == "move":
            return {"path": await run(files.move, root, body["path"], body["dest"])}
        if op == "copy":
            return {"path": await run(files.copy, root, body["path"],
                                      body.get("dest") or ".")}
        if op == "delete":
            return {"deleted": await run(files.delete, root,
                                         list(body.get("paths") or []))}
        if op == "mkdir":
            await run(files.mkdir, root, body.get("parent") or ".", body["name"])
            return {"created": True}
        if op == "create":
            await run(files.create_file, root, body.get("parent") or ".", body["name"])
            return {"created": True}
        if op == "extract":
            return {"written": len(await run(files.extract, root, body["path"]))}
        if op == "compress":
            return {"path": await run(files.compress, root,
                                      list(body.get("paths") or []), body["dest"])}
        if op == "chmod":
            await run(files.chmod, root, body["path"], int(str(body["mode"]), 8))
            return {"changed": True}
        raise RuntimeError_(f"unknown file operation {op!r}", 404)

    @app.get("/v1/servers/{uuid}/files/usage")
    async def usage(uuid: str, path: str = "."):
        return await heavy(files.usage, folder(uuid), path)

    @app.get("/v1/servers/{uuid}/files/search")
    async def search(uuid: str, q: str, path: str = ".", limit: int = 300):
        # A thread: a walk of a big pack's folder must not hold up every
        # other server's console on this node while it runs.
        return {"results": await heavy(
            files.search, folder(uuid), q, path, max(1, min(limit, 2000)))}

    # --- backups: WP-11 ------------------------------------------------------------

    @app.post("/v1/servers/{uuid}/backups")
    @app.post("/v1/servers/{uuid}/backups/{backup_id}/restore")
    @app.delete("/v1/servers/{uuid}/backups/{backup_id}")
    async def backups(uuid: str, backup_id: str | None = None):
        raise RuntimeError_("world backups (restic) arrive with WP-11", 501)

    # --- what a browser reaches directly ------------------------------------------------

    @app.websocket("/v1/servers/{uuid}/ws")
    async def ws(socket: WebSocket, uuid: str, token: str = "", backlog: int = 500):
        try:
            claims = state["tokens"].verify(token, server_uuid=uuid, perm="console.read")
            srv = sup().get(uuid)
        except (auth.Denied, RuntimeError_) as e:
            await socket.close(code=4401 if isinstance(e, auth.Denied) else 4404,
                               reason=str(e)[:120])
            return
        may_type = "console.command" in (claims.get("perm") or [])
        await socket.accept()
        q = srv.console.subscribe()
        await socket.send_json({"type": "backlog",
                                "lines": list(srv.console.lines)[-max(1, min(backlog, 2000)):],
                                "seq": srv.console.seq,
                                "state": srv.state, "may_type": may_type})

        async def outbound():
            last_stats, last_state = 0.0, srv.state
            while True:
                try:
                    seq, line = await asyncio.wait_for(q.get(), 1.0)
                    batch = [line]
                    while not q.empty() and len(batch) < 200:
                        seq, line = q.get_nowait()
                        batch.append(line)
                    if q in srv.console.overflowed:
                        # It fell behind and lines were dropped: the ring
                        # afresh rather than a console with a hole in it.
                        srv.console.overflowed.discard(q)
                        while not q.empty():
                            q.get_nowait()
                        await socket.send_json({"type": "backlog",
                                                "lines": list(srv.console.lines),
                                                "seq": srv.console.seq,
                                                "state": srv.state, "may_type": may_type})
                        continue
                    await socket.send_json({"type": "lines", "lines": batch, "seq": seq})
                except asyncio.TimeoutError:
                    pass
                # Every five seconds, and at once when the state changes, so
                # a console shows "running" as the server comes up.
                if time.time() - last_stats > 5 or srv.state != last_state:
                    last_stats, last_state = time.time(), srv.state
                    await socket.send_json({"type": "stats", **await sup().stats(uuid)})

        sender = asyncio.create_task(outbound())
        try:
            while True:
                msg = await socket.receive_json()
                if msg.get("type") == "command":
                    if not may_type:
                        await socket.send_json({"type": "error",
                                                "message": "this token cannot type commands"})
                        continue
                    try:
                        await sup().command(uuid, str(msg.get("command") or ""))
                    except RuntimeError_ as e:
                        await socket.send_json({"type": "error", "message": str(e)})
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        finally:
            sender.cancel()
            srv.console.unsubscribe(q)

    @app.post("/v1/upload")
    async def browser_upload(request: Request, token: str = ""):
        claims = _browser(token, "files.write", request.query_params.get("server"),
                          request.query_params.get("path"))
        size = await _receive(folder(claims["srv"]), claims["path"], request)
        return {"written": claims["path"], "bytes": size}

    @app.get("/v1/download")
    async def browser_download(request: Request, token: str = ""):
        claims = _browser(token, "files.read", request.query_params.get("server"),
                          request.query_params.get("path"))
        root = folder(claims["srv"])
        await quick(files.check_readable, root, claims["path"])
        name = claims["path"].rsplit("/", 1)[-1]
        return StreamingResponse(files.iter_file(root, claims["path"]),
                                 media_type="application/octet-stream",
                                 headers={"Content-Disposition":
                                          f'attachment; filename="{name}"'})

    def _browser(token: str, perm: str, server: str | None, path: str | None) -> dict:
        if not server or not path:
            raise RuntimeError_("server and path are required", 400)
        try:
            return state["tokens"].verify(token, server_uuid=check_uuid(server),
                                          perm=perm, path=path)
        except auth.Denied as e:
            raise RuntimeError_(str(e), e.status)

    app.state.agent = state
    return app


_ABORT = object()


async def _receive(root: Path, path: str, request: Request) -> int:
    """Stream a request body into the jail without holding it whole.

    The jail writes to a temp file and renames it into place only when the
    write finishes cleanly; a body that is cut off part-way aborts that
    write, so a half-uploaded file never replaces a whole one.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=8)
    loop = asyncio.get_running_loop()

    def chunks():
        while True:
            item = asyncio.run_coroutine_threadsafe(queue.get(), loop).result()
            if item is None:
                return
            if item is _ABORT:
                raise OSError("the upload was cut off")
            yield item

    writer = loop.run_in_executor(None, files.write_stream, root, path, chunks())
    try:
        async for chunk in request.stream():
            if chunk:
                await queue.put(chunk)
    except BaseException:
        await queue.put(_ABORT)
        try:
            await writer
        except OSError:
            pass
        raise
    await queue.put(None)
    return await writer


async def _stats_loop(sup: Supervisor, events: Events) -> None:
    while True:
        await asyncio.sleep(STATS_EVERY)
        batch = {}
        for uuid, srv in list(sup.servers.items()):
            if srv.running is not None:
                try:
                    batch[uuid] = await sup.stats(uuid)
                except Exception:       # noqa: BLE001
                    continue
        if batch:
            events.emit("stats", None, servers=batch)


async def _reconcile(cfg: AgentConfig, sup: Supervisor, events: Events) -> None:
    """Make this node match what the panel says should be on it."""
    delay = 2.0
    while True:
        try:
            r = await events.panel().get(f"{cfg.panel_url.rstrip('/')}/api/agent/servers",
                                         headers=events.headers(), timeout=30)
            if r.status_code >= 400:
                raise RuntimeError(f"the panel answered {r.status_code}")
            desired = r.json().get("servers") or []
            break
        except Exception as e:      # noqa: BLE001 -- the node runs without the panel
            log.warning("cannot fetch desired state yet: %s", e)
            await asyncio.sleep(delay)
            delay = min(60.0, delay * 2)
    wanted = {s["uuid"] for s in desired}
    created, updated = [], []
    for spec in desired:
        existing = sup.servers.get(spec["uuid"])
        try:
            sup.put(spec["uuid"], spec, strict=False)
        except Exception as e:      # noqa: BLE001 -- reported, the rest continue
            events.emit("drift", spec["uuid"], problem=str(e))
            continue
        (updated if existing else created).append(spec["uuid"])
    unknown = [u for u in sup.servers if u not in wanted]
    for uuid in unknown:
        events.emit("drift", uuid, problem="on this node but not in the panel")
    # One auto-start server per port: servers may share a port, and if an
    # older record has two marked, only the first (in a stable order) starts.
    claimed: dict[int, str] = {s.spec["port"]: s.uuid for s in sup.servers.values()
                               if s.running is not None}
    for spec in sorted(desired, key=lambda d: (str(d.get("name") or ""), d["uuid"])):
        srv = sup.servers.get(spec["uuid"])
        if not (srv and spec.get("auto_start") and srv.running is None):
            continue
        port = srv.spec["port"]
        if port in claimed:
            events.emit("drift", spec["uuid"], problem=f"not auto-started: port {port} "
                        "is another server's, which starts by itself or is running")
            continue
        claimed[port] = srv.uuid
        try:
            await sup.power(spec["uuid"], "start")
        except Exception as e:      # noqa: BLE001
            events.emit("drift", spec["uuid"], problem=f"could not auto-start: {e}")
    events.emit("reconciled", None, created=created, updated=updated, unknown=unknown)


app = create_app()
