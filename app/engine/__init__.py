"""The one door to every server: `from app import engine`.

Call sites stay short -- `await engine.read_file(server_id, "eula.txt")` --
and the routing lives here: `for_server(server_id)` finds the driver for the
node that server is on, and each wrapper below hands the call to it. Which
drivers exist is decided by `configure()`, from `ENGINE` in the environment:

  * `mock`  -- one in-memory node (`app/engine/mock.py`). Tests, and a UI
              you can click through with no node at all.
  * `agent` -- every node registered with BlessForge, each reached through
              its agent (`app/engine/agent.py`, WP-05).

A server is mapped to its node when it is listed or created. With a single
node there is nothing to look up.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from app.engine.base import (EULA_ACCEPTED, POWER_ACTIONS, EngineError,
                             ServerEngine, java_in_command, java_of,
                             power_action, required_java_major, uptime_seconds)

__all__ = [
    "EULA_ACCEPTED", "POWER_ACTIONS", "EngineError", "ServerEngine",
    "java_in_command", "java_of", "power_action", "required_java_major",
    "uptime_seconds", "configure", "kind", "drivers", "remember", "for_server",
    "default_node", "load_nodes", "list_servers", "get_server", "get_stats", "create_server",
    "delete_server", "update_server", "power", "install", "set_runtime",
    "list_dir", "dir_exists", "read_file", "read_tail", "write_file", "delete_paths",
    "rename_path", "move_path", "create_entry", "ensure_dir", "upload_file",
    "upload_path", "extract", "download_file", "stream_file", "wait_for_path",
    "walk", "console_lines", "send_command", "read_manifest", "write_manifest",
]

_drivers: dict[str, ServerEngine] = {}
_server_node: dict[str, str] = {}
_configured: str | None = None


def configure(kind: str | None = None, *, drivers: dict[str, ServerEngine] | None = None
              ) -> None:
    """Choose the drivers. Called lazily on first use; tests call it directly.

    `drivers` installs an explicit {node_id: driver} map, which is how a test
    puts a fresh mock in place, and how the agent driver set is rebuilt when
    a node is added.
    """
    global _configured
    _drivers.clear()
    _server_node.clear()
    if drivers is not None:
        _drivers.update(drivers)
        _configured = kind or "custom"
        return
    from app import config
    kind = (kind or config.ENGINE or "mock").strip().lower()
    if kind == "mock":
        from app.engine.mock import MockEngine
        _drivers["mock"] = MockEngine(seed=config.ENGINE_MOCK_SEED)
    elif kind == "agent":
        try:
            from app.engine import agent
        except ImportError:
            raise EngineError("the agent driver is not installed in this build", 503)
        _drivers.update(agent.drivers_from_config())
    else:
        raise EngineError(f"unknown ENGINE {kind!r}; expected mock or agent")
    _configured = kind


def kind() -> str:
    _ensure()
    return _configured or ""


def drivers() -> dict[str, ServerEngine]:
    _ensure()
    return dict(_drivers)


def _ensure() -> None:
    if _configured is None:
        configure()


def remember(server_id: str, node: str) -> None:
    """Record which node a server lives on (from a listing, a create, the DB)."""
    if server_id and node:
        _server_node[server_id] = node


def for_server(server_id: str) -> ServerEngine:
    """The driver for the node holding this server."""
    _ensure()
    if not _drivers:
        raise EngineError("no node is registered, so there are no servers yet",
                          503)
    node = _server_node.get(server_id)
    if node and node in _drivers:
        return _drivers[node]
    if len(_drivers) == 1:
        return next(iter(_drivers.values()))
    raise EngineError(f"no node is known to hold server {server_id}", 404)


async def load_nodes() -> int:
    """Build an agent driver for every registered node, and map their servers.

    Only for ENGINE=agent. Called at start (API and worker) and after a node
    is registered. Returns how many nodes there are.
    """
    _ensure()
    if _configured != "agent":
        return len(_drivers)
    from sqlalchemy import select
    from app import db, nodes
    from app.db.models import Server
    from app.engine.agent import AgentEngine
    rows = await nodes.all_rows()
    fresh = {}
    for node in rows:
        try:
            fresh[node.id] = AgentEngine(node, nodes.agent_url(node),
                                         nodes.token_header(node))
        except Exception as e:      # noqa: BLE001 -- one bad node is not all of them
            import logging
            logging.getLogger(__name__).warning("node %s skipped: %s", node.id, e)
    _drivers.clear()
    _drivers.update(fresh)
    async with db.session() as s:
        for sid, nid in (await s.execute(select(Server.uuid, Server.node_id))).all():
            if nid:
                remember(sid, nid)
    return len(_drivers)


async def _driver(server_id: str) -> ServerEngine:
    """for_server, falling back to the database for a server created elsewhere."""
    try:
        return for_server(server_id)
    except EngineError as e:
        if e.status != 404 or _configured != "agent":
            raise
    from app import db
    from app.db.models import Server
    async with db.session() as s:
        row = await s.get(Server, server_id)
    if row is None or not row.node_id:
        raise EngineError(f"no server {server_id}", 404)
    remember(server_id, row.node_id)
    if row.node_id not in _drivers:
        await load_nodes()
    return for_server(server_id)


def default_node() -> str:
    """Where a new server goes when the caller does not say."""
    _ensure()
    if not _drivers:
        raise EngineError("no node is configured to create a server on", 503)
    return next(iter(_drivers))


# --- servers ------------------------------------------------------------------


async def list_servers() -> list[dict]:
    """Every server on every node. A node that does not answer is skipped,
    not fatal: one unplugged box must not blank the whole fleet."""
    _ensure()
    out: list[dict] = []
    errors: list[str] = []
    for node, drv in _drivers.items():
        try:
            servers = await drv.list_servers()
        except EngineError as e:
            if len(_drivers) == 1:
                raise
            errors.append(f"{node}: {e}")
            continue
        for s in servers:
            s.setdefault("node", node)
            remember(s.get("server_id"), node)
            out.append(s)
    return out


async def get_server(server_id: str) -> dict:
    return await (await _driver(server_id)).get_server(server_id)


async def get_stats(server_id: str) -> dict:
    return await (await _driver(server_id)).get_stats(server_id)


async def create_server(*, name: str, port: int, memory_mb: int,
                        mc_version: str = "", loader: str = "",
                        java_major: int | None = None, node: str | None = None,
                        server_id: str | None = None,
                        limits: dict | None = None) -> str:
    _ensure()
    node = node or default_node()
    drv = _drivers.get(node)
    if drv is None:
        raise EngineError(f"unknown node {node!r}", 404)
    sid = await drv.create_server(
        name=name, port=port, memory_mb=memory_mb, mc_version=mc_version,
        loader=loader, java_major=java_major, server_id=server_id, limits=limits)
    remember(sid, node)
    return sid


async def delete_server(server_id: str, delete_files: bool = True) -> None:
    await (await _driver(server_id)).delete_server(server_id, delete_files)
    _server_node.pop(server_id, None)


async def update_server(server_id: str, fields: dict) -> None:
    await (await _driver(server_id)).update_server(server_id, fields)


async def power(server_id: str, action: str) -> None:
    await (await _driver(server_id)).power(server_id, power_action(action))


async def install(server_id: str, spec: dict) -> dict:
    return await (await _driver(server_id)).install(server_id, spec)


async def set_runtime(server_id: str, mc_version: str,
                      java_major: int | None = None) -> dict:
    """Put a server on the right Java. `java_major` from a template (which
    asked Mojang); without one, the offline table decides."""
    return await (await _driver(server_id)).set_runtime(server_id, mc_version,
                                                        java_major)


# --- files --------------------------------------------------------------------


async def list_dir(server_id: str, path: str = ".") -> dict:
    return await (await _driver(server_id)).list_dir(server_id, path)


async def dir_exists(server_id: str, path: str) -> bool:
    try:
        await list_dir(server_id, path)
        return True
    except EngineError:
        return False


async def read_file(server_id: str, path: str) -> str:
    return await (await _driver(server_id)).read_file(server_id, path)


async def read_tail(server_id: str, path: str, nbytes: int = 2 * 1024 * 1024) -> str:
    """The end of a text file, from a whole line on -- for logs past the
    size a whole read allows."""
    return await (await _driver(server_id)).read_tail(server_id, path, nbytes)


async def write_file(server_id: str, path: str, contents: str) -> None:
    await (await _driver(server_id)).write_file(server_id, path, contents)


async def delete_paths(server_id: str, rel_paths: list[str]) -> None:
    if rel_paths:
        await (await _driver(server_id)).delete_paths(server_id, rel_paths)


async def rename_path(server_id: str, path: str, new_name: str) -> None:
    await (await _driver(server_id)).rename_path(server_id, path, new_name)


async def move_path(server_id: str, path: str, dest_dir: str) -> None:
    await (await _driver(server_id)).move_path(server_id, path, dest_dir)


async def create_entry(server_id: str, parent: str, name: str,
                       directory: bool = False) -> None:
    await (await _driver(server_id)).create_entry(server_id, parent, name, directory)


async def ensure_dir(server_id: str, rel_dir: str) -> None:
    """mkdir -p: one level at a time, each one idempotent."""
    parts = [p for p in rel_dir.replace("\\", "/").split("/") if p and p != "."]
    parent = "."
    for part in parts:
        await create_entry(server_id, parent, part, directory=True)
        parent = part if parent == "." else f"{parent}/{part}"


async def upload_file(server_id: str, location: str, filename: str,
                      payload: bytes) -> None:
    await (await _driver(server_id)).upload_file(server_id, location, filename, payload)


async def upload_path(server_id: str, location: str, filename: str, src: Any) -> None:
    await (await _driver(server_id)).upload_path(server_id, location, filename, src)


async def extract(server_id: str, rel_zip_path: str) -> None:
    """Unpack a zip that is already in the server folder, into its folder.

    Finished when this returns -- unlike the controller this replaced, whose
    unzip answered "started" and left the caller to poll for the result.
    """
    await (await _driver(server_id)).extract(server_id, rel_zip_path)


async def download_file(server_id: str, rel_path: str) -> bytes:
    return await (await _driver(server_id)).download_file(server_id, rel_path)


async def stream_file(server_id: str, rel_path: str) -> AsyncIterator[bytes]:
    async for chunk in (await _driver(server_id)).stream_file(server_id, rel_path):
        yield chunk


async def wait_for_path(server_id: str, rel_path: str, timeout: float,
                        poll: float = 3.0) -> bool:
    """Poll until `rel_path` exists. False on timeout."""
    import asyncio
    import posixpath
    parent = posixpath.dirname(rel_path.rstrip("/")) or "."
    target = posixpath.basename(rel_path.rstrip("/"))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            if target in await list_dir(server_id, parent):
                return True
        except EngineError:
            pass
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(poll)


async def walk(server_id: str, root: str = ".", max_entries: int = 20000) -> list[dict]:
    """Every entry under `root`, breadth first, at most `max_entries`."""
    import posixpath
    out: list[dict] = []
    queue = [root]
    seen = 0
    while queue and seen < max_entries:
        current = queue.pop(0)
        try:
            entries = await list_dir(server_id, current)
        except EngineError:
            continue
        for name, meta in entries.items():
            if not isinstance(meta, dict):
                continue
            rel = (meta.get("path") or (
                name if current == "." else posixpath.join(current, name)
            )).replace("\\", "/")
            seen += 1
            if meta.get("dir"):
                queue.append(rel)
                out.append({"path": rel, "name": name, "dir": True})
            else:
                out.append({"path": rel, "name": name, "dir": False,
                            "size": meta.get("size"),
                            "modified": meta.get("modified")})
    return out


# --- console ------------------------------------------------------------------


async def console_lines(server_id: str, *, from_file: bool = False) -> list[str]:
    return await (await _driver(server_id)).console_lines(server_id, from_file=from_file)


async def send_command(server_id: str, command: str) -> dict:
    line = (command or "").strip()
    if not line:
        raise ValueError("no command given")
    return await (await _driver(server_id)).send_command(server_id, line)


# --- the BlessForge manifest ----------------------------------------------------


async def read_manifest(server_id: str) -> dict:
    """Our own per-server record of what was installed and from where."""
    import json
    from app import config
    for name in (config.STUDIO_MANIFEST, *config.LEGACY_MANIFESTS):
        try:
            data = json.loads(await read_file(server_id, name))
            if isinstance(data, dict):
                return data
        except Exception:      # noqa: BLE001 -- absent or torn: try the next
            continue
    return {}


async def write_manifest(server_id: str, data: dict) -> None:
    import json
    from app import config
    await write_file(server_id, config.STUDIO_MANIFEST, json.dumps(data, indent=2))
