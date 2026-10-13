"""The engine driver for a node running the BlessForge agent.

One `AgentEngine` per registered node. A server's record -- name, port,
startup command, Java, limits -- lives in BlessForge's own `servers` table:
that row is the desired state, the agent's container is made from it, and
it is what the agent fetches (`/api/agent/servers`) to reconcile when it
starts. So every change is written to the row first and then pushed to the
agent (`PUT /v1/servers/{uuid}`); if the push fails, the change is still
recorded and the agent picks it up on its next reconcile.

Everything else -- power, console, files, installs -- is a call to the
agent's /v1 API with the node token.
"""
from __future__ import annotations

import asyncio
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from sqlalchemy import select

from app import db
from app.db.models import Node, Server
from app.engine.base import EngineError, power_action, required_java_major

# Fields the rest of the app calls a record by, and the columns behind them.
_FIELD_COLUMNS = {"server_name": "name", "server_port": "port",
                  "executable": "executable", "execution_command": "execution_command",
                  "java_major": "java_major", "auto_start": "auto_start"}


def spec_of(row: Server) -> dict:
    """The desired state the agent builds a container from."""
    limits = dict(row.limits or {})
    from app import config
    return {"uuid": row.uuid, "name": row.name, "port": row.port,
            "java_major": row.java_major, "execution_command": row.execution_command,
            "memory_mb": int(limits.get("memory_mb") or 2048), "limits": limits,
            "auto_start": bool(row.auto_start),
            # Port: automatic -- the node picks a free one at each start.
            "port_auto": bool(limits.get("port_auto")),
            "port_range": list(config.GAME_PORT_RANGE)}


def record_of(row: Server) -> dict:
    limits = dict(row.limits or {})
    return {"server_id": row.uuid, "server_name": row.name, "server_port": row.port,
            "executable": row.executable, "execution_command": row.execution_command,
            "java_major": row.java_major,
            "memory_mb": int(limits.get("memory_mb") or 2048), "limits": limits,
            "node": row.node_id, "path": f"{row.node_id}:{row.uuid}",
            # With its zone: SQLite hands back a naive UTC time, and a browser
            # reads a zoneless ISO string as local -- a server made a minute
            # ago in India showed "added 6 h ago".
            "created": (time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                      time.gmtime(db.epoch(row.created_at)))
                        if row.created_at else ""),
            "type": "minecraft-java", "auto_start": bool(row.auto_start),
            "kind": row.kind, "status": row.status, "template": row.template,
            "mc_version": row.mc_version}


# One long-lived client per node (per event loop): a new one per call cost a
# TCP connection -- and a TLS handshake in production -- per request, five a
# second per console viewer, and building one blocks the loop for 60-100 ms.
# Keyed by URL and token rather than kept on the AgentEngine, because
# load_nodes() rebuilds the drivers whenever a node is registered.
_clients: dict[tuple[str, str], tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def _shared(url: str, header: str) -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    held = _clients.get((url, header))
    if held and held[0] is loop and not held[1].is_closed:
        return held[1]
    made = httpx.AsyncClient(base_url=url, timeout=60,
                             headers={"Authorization": header})
    _clients[(url, header)] = (loop, made)
    return made


async def close_clients() -> None:
    """On shutdown: close the clients this event loop opened."""
    loop = asyncio.get_running_loop()
    for key, (owner, c) in list(_clients.items()):
        if owner is loop:
            await c.aclose()
        _clients.pop(key, None)


class AgentEngine:
    name = "agent"

    def __init__(self, node: Node, url: str, header: str):
        self.node_id = node.id
        self.url = url.rstrip("/")
        self.header = header

    # --- plumbing --------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        """This node's one long-lived client (see _shared)."""
        return _shared(self.url, self.header)

    async def _call(self, method: str, path: str, *, timeout: float = 60, **kw) -> Any:
        try:
            r = await self._client().request(method, path, timeout=timeout, **kw)
        except httpx.HTTPError as e:
            raise EngineError(f"node {self.node_id} is not answering ({type(e).__name__})",
                              503)
        if r.status_code >= 400:
            try:
                body = r.json()
                detail = body.get("detail")
            except ValueError:
                body, detail = None, r.text[:300]
            raise EngineError(str(detail or f"node {self.node_id} answered {r.status_code}"),
                              r.status_code, body if isinstance(body, dict) else detail)
        if not r.content:
            return None
        ctype = r.headers.get("content-type", "")
        return r.json() if "json" in ctype else r.content

    async def _row(self, s, server_id: str) -> Server:
        row = await s.get(Server, server_id)
        if row is None or row.node_id != self.node_id:
            raise EngineError(f"no server {server_id} on node {self.node_id}", 404)
        return row

    async def _push(self, row: Server) -> None:
        await self._call("PUT", f"/v1/servers/{row.uuid}", json=spec_of(row))

    # --- servers ------------------------------------------------------------------

    async def list_servers(self) -> list[dict]:
        async with db.session() as s:
            rows = (await s.execute(select(Server).where(Server.node_id == self.node_id)
                                    .order_by(Server.created_at))).scalars().all()
            return [record_of(r) for r in rows]

    async def get_server(self, server_id: str) -> dict:
        async with db.session() as s:
            return record_of(await self._row(s, server_id))

    async def get_stats(self, server_id: str) -> dict:
        live = await self._call("GET", f"/v1/servers/{server_id}", timeout=15)
        stats = dict(live.get("stats") or {})
        stats.setdefault("max", None)
        stats.setdefault("world_name", "world")
        stats.setdefault("world_size", None)
        return stats

    async def create_server(self, *, name: str, port: int, memory_mb: int,
                            mc_version: str = "", loader: str = "",
                            java_major: int | None = None,
                            server_id: str | None = None,
                            limits: dict | None = None) -> str:
        sid = server_id or str(uuidlib.uuid4())
        async with db.session() as s:
            # Servers may share a port (one runs at a time; the node checks
            # at start), so a port in use is not refused here.
            row = Server(uuid=sid, kind="personal", node_id=self.node_id,
                         name=name[:120], port=int(port), template=loader or "",
                         mc_version=mc_version or "",
                         java_major=java_major or (required_java_major(mc_version)
                                                   if mc_version else None),
                         limits={"memory_mb": int(memory_mb), **(limits or {})},
                         status="installing")
            s.add(row)
            await s.flush()
            spec = spec_of(row)
        try:
            await self._call("PUT", f"/v1/servers/{sid}", json=spec)
        except EngineError:
            # Nothing exists on the node, so nothing is left in the panel either.
            async with db.session() as s:
                row = await s.get(Server, sid)
                if row is not None:
                    await s.delete(row)
            raise
        return sid

    async def delete_server(self, server_id: str, delete_files: bool = True) -> None:
        async with db.session() as s:
            await self._row(s, server_id)
        try:
            await self._call("DELETE", f"/v1/servers/{server_id}",
                             params={"files": "true" if delete_files else "false"},
                             timeout=120)
        except EngineError as e:
            if e.status != 404:
                raise
        async with db.session() as s:
            row = await s.get(Server, server_id)
            if row is not None:
                await s.delete(row)

    async def update_server(self, server_id: str, fields: dict) -> None:
        async with db.session() as s:
            row = await self._row(s, server_id)
            port = int(fields.get("server_port") or row.port)
            if fields.get("auto_start") or (row.auto_start and "server_port" in fields):
                other = (await s.execute(select(Server).where(
                    Server.node_id == self.node_id, Server.port == port,
                    Server.uuid != server_id, Server.auto_start.is_(True)))).scalars().first()
                if other is not None:
                    raise EngineError(
                        f"{other.name} already starts by itself on port {port}; only one "
                        "server on a port can.", 409,
                        {"conflict": {"server": other.uuid, "name": other.name,
                                      "port": port}})
            for key, value in fields.items():
                if key in _FIELD_COLUMNS:
                    setattr(row, _FIELD_COLUMNS[key],
                            int(value) if key in ("server_port", "java_major")
                            and value is not None else value)
                elif key == "memory_mb":
                    row.limits = {**(row.limits or {}), "memory_mb": int(value)}
                elif key == "limits":
                    row.limits = {**(row.limits or {}), **dict(value or {})}
                else:
                    raise EngineError(f"cannot change {key}", 400)
            if fields.get("execution_command"):
                row.status = "ready"
            await s.flush()
            spec = spec_of(row)
        await self._call("PUT", f"/v1/servers/{server_id}", json=spec)

    async def power(self, server_id: str, action: str) -> None:
        await self._call("POST", f"/v1/servers/{server_id}/power",
                         json={"action": power_action(action)}, timeout=180)

    async def install(self, server_id: str, spec: dict) -> dict:
        job = await self._call("POST", f"/v1/servers/{server_id}/install", json=spec)
        deadline = time.time() + int(spec.get("timeout") or 900) + 120
        while time.time() < deadline:
            rec = await self._call("GET", f"/v1/servers/{server_id}/install/{job['job_id']}")
            if rec["status"] == "done":
                return {"ok": True, "files": rec.get("files") or [],
                        "output": rec.get("output") or []}
            if rec["status"] == "error":
                raise EngineError(rec.get("error") or "the install failed", 500,
                                  {"output": rec.get("output") or []})
            if rec["status"] == "waiting":
                # Queued behind another install on the node: its own clock
                # starts when it does.
                deadline = time.time() + int(spec.get("timeout") or 900) + 120
            await asyncio.sleep(1.0)
        raise EngineError("the install did not finish in time", 504)

    async def set_runtime(self, server_id: str, mc_version: str,
                          java_major: int | None = None) -> dict:
        want = int(java_major or required_java_major(mc_version))
        async with db.session() as s:
            row = await self._row(s, server_id)
            old = row.java_major
            row.java_major = want
            spec = spec_of(row)
        await self._call("PUT", f"/v1/servers/{server_id}", json=spec)
        return {"changed": old != want, "java_major": want,
                "java_path": f"java {want} runtime",
                **({} if old != want else {"reason": "already correct"})}

    # --- files ---------------------------------------------------------------------

    async def list_dir(self, server_id: str, path: str = ".") -> dict:
        out = await self._call("GET", f"/v1/servers/{server_id}/files",
                               params={"path": path})
        return out["entries"]

    async def folder_usage(self, server_id: str) -> dict:
        return await self._call("GET", f"/v1/servers/{server_id}/files/usage",
                                params={"path": "."})

    async def search_files(self, server_id: str, query: str, root: str,
                           limit: int) -> list[dict]:
        out = await self._call("GET", f"/v1/servers/{server_id}/files/search",
                               params={"q": query, "path": root, "limit": limit})
        return list(out.get("results") or [])[:limit]

    async def read_file(self, server_id: str, path: str) -> str:
        out = await self._call("GET", f"/v1/servers/{server_id}/files/content",
                               params={"path": path})
        return out["content"]

    async def read_tail(self, server_id: str, path: str, nbytes: int) -> str:
        out = await self._call("GET", f"/v1/servers/{server_id}/files/content",
                               params={"path": path, "tail": int(nbytes)})
        text = out["content"]
        # An agent from before `tail` returns the whole file: trim it here.
        if len(text) > nbytes:
            text = text[-nbytes:].split("\n", 1)[-1]
        return text

    async def write_file(self, server_id: str, path: str, contents: str) -> None:
        await self._call("PUT", f"/v1/servers/{server_id}/files/content",
                         params={"path": path}, json={"content": contents})

    async def delete_paths(self, server_id: str, rel_paths: list[str]) -> None:
        await self._call("POST", f"/v1/servers/{server_id}/files/delete",
                         json={"paths": rel_paths}, timeout=300)

    async def rename_path(self, server_id: str, path: str, new_name: str) -> None:
        await self._call("POST", f"/v1/servers/{server_id}/files/rename",
                         json={"path": path, "name": new_name})

    async def move_path(self, server_id: str, path: str, dest_dir: str) -> None:
        await self._call("POST", f"/v1/servers/{server_id}/files/move",
                         json={"path": path, "dest": dest_dir})

    async def create_entry(self, server_id: str, parent: str, name: str,
                           directory: bool = False) -> None:
        await self._call("POST", f"/v1/servers/{server_id}/files/"
                                 f"{'mkdir' if directory else 'create'}",
                         json={"parent": parent, "name": name})

    def _target(self, location: str, filename: str) -> str:
        loc = (location or ".").strip().strip("/")
        return filename if loc in ("", ".") else f"{loc}/{filename}"

    async def upload_file(self, server_id: str, location: str, filename: str,
                          payload: bytes) -> None:
        await self._call("PUT", f"/v1/servers/{server_id}/files/raw",
                         params={"path": self._target(location, filename)},
                         content=payload, timeout=1800)

    async def upload_path(self, server_id: str, location: str, filename: str,
                          src: Any) -> None:
        path = Path(src)

        async def body():
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        return
                    yield chunk

        await self._call("PUT", f"/v1/servers/{server_id}/files/raw",
                         params={"path": self._target(location, filename)},
                         content=body(), timeout=3600)

    async def extract(self, server_id: str, rel_zip_path: str) -> None:
        await self._call("POST", f"/v1/servers/{server_id}/files/extract",
                         json={"path": rel_zip_path}, timeout=3600)

    async def download_file(self, server_id: str, rel_path: str) -> bytes:
        return await self._call("GET", f"/v1/servers/{server_id}/files/raw",
                                params={"path": rel_path}, timeout=600)

    async def stream_file(self, server_id: str, rel_path: str) -> AsyncIterator[bytes]:
        async with self._client().stream("GET", f"/v1/servers/{server_id}/files/raw",
                                         params={"path": rel_path}, timeout=1800) as r:
            if r.status_code >= 400:
                await r.aread()
                raise EngineError(f"download {rel_path} failed", r.status_code)
            async for chunk in r.aiter_bytes(256 * 1024):
                yield chunk

    # --- console ---------------------------------------------------------------------

    async def console_lines(self, server_id: str, *, from_file: bool = False
                            ) -> list[str]:
        if from_file:
            try:
                return (await self.read_file(server_id, "logs/latest.log")).splitlines()
            except EngineError as e:
                if e.status != 413:
                    return []
            # Past the whole-read limit: its end, where "Done" and the crash
            # are. 2BROKE4LORE's log passed 4 MB mid-boot; read whole, it came
            # back empty, and a server that reached "Done" was called hung.
            try:
                return (await self.read_tail(server_id, "logs/latest.log",
                                             3 * 1024 * 1024)).splitlines()
            except EngineError:
                return []
        out = await self._call("GET", f"/v1/servers/{server_id}/console",
                               params={"lines": 2000})
        return out["lines"]

    async def host_ports(self, server_id: str) -> list[dict]:
        """What holds host ports on this server's machine (GET /v1/ports)."""
        out = await self._call("GET", "/v1/ports", timeout=10)
        return list(out.get("ports") or [])

    async def console_since(self, server_id: str, after: int | None) -> dict:
        """The node's numbered console: with `after`, only the lines after
        that number ({"lines", "seq", "reset"}); without, its last 2000."""
        params = {"lines": 2000} if after is None else {"after": int(after)}
        out = await self._call("GET", f"/v1/servers/{server_id}/console",
                               params=params, timeout=10)
        return {"lines": list(out.get("lines") or []), "seq": int(out.get("seq") or 0),
                "reset": bool(out.get("reset"))}

    async def send_command(self, server_id: str, command: str) -> dict:
        await self._call("POST", f"/v1/servers/{server_id}/command",
                         json={"command": command})
        return {"sent": command}

    # --- for the browser ---------------------------------------------------------------

    def socket_url(self, server_id: str, token: str, public_base: str) -> str:
        base = public_base.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/v1/servers/{server_id}/ws?token={token}"


def drivers_from_config() -> dict:
    """Agent drivers come from the database; `engine.load_nodes()` fills them."""
    return {}
