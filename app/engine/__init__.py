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

import asyncio
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
    "walk", "search_files", "folder_usage", "node_system", "console_lines", "send_command",
    "read_manifest",
    "write_manifest",
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
    drivers = list(_drivers.items())
    # Every node at once: one slow node costs its own wait, not everyone's.
    found = await asyncio.gather(*(drv.list_servers() for _n, drv in drivers),
                                 return_exceptions=True)
    for (node, _drv), servers in zip(drivers, found):
        if isinstance(servers, EngineError):
            if len(_drivers) == 1:
                raise servers
            errors.append(f"{node}: {servers}")
            continue
        if isinstance(servers, BaseException):
            raise servers
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


async def install(server_id: str, spec: dict, on_progress=None) -> dict:
    """`on_progress(progress)` is called as a fetch_many step moves."""
    drv = await _driver(server_id)
    if on_progress is None:
        return await drv.install(server_id, spec)
    return await drv.install(server_id, spec, on_progress=on_progress)


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


async def world_backups(server_id: str) -> dict:
    return await (await _driver(server_id)).world_backups(server_id)


async def world_backup_start(server_id: str, opts: dict) -> str:
    return await (await _driver(server_id)).world_backup_start(server_id, opts)


async def world_backup_job(job_id: str) -> dict:
    """A node's backup job. Every node is asked until one knows it: job ids
    are random, and a panel with one node asks once."""
    last: Exception | None = None
    for drv in drivers().values():
        try:
            return await drv.world_backup_job(job_id)
        except EngineError as e:
            last = e
    raise last or EngineError("no such backup job", 404)


async def world_backup_restore(server_id: str, snapshot_id: str, opts: dict) -> str:
    return await (await _driver(server_id)).world_backup_restore(server_id, snapshot_id,
                                                                  opts)


async def world_backup_delete(server_id: str, snapshot_id: str) -> None:
    await (await _driver(server_id)).world_backup_delete(server_id, snapshot_id)


async def world_backup_stream(server_id: str, snapshot_id: str) -> AsyncIterator[bytes]:
    async for chunk in (await _driver(server_id)).world_backup_stream(server_id, snapshot_id):
        yield chunk


async def compress(server_id: str, rel_paths: list[str], dest_rel: str) -> str:
    """Zip files or folders into dest_rel, in the server folder, on the node."""
    return await (await _driver(server_id)).compress(server_id, rel_paths, dest_rel)


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


async def search_files(server_id: str, query: str, root: str = ".",
                       limit: int = 300) -> list[dict]:
    """Entries under `root` whose name contains `query`. On the node in one
    request where the driver can; walked from here otherwise."""
    drv = await _driver(server_id)
    own = getattr(drv, "search_files", None)
    if own is not None:
        return await own(server_id, query, root, limit)
    q = query.lower()
    return [e for e in await walk(server_id, root, max_entries=8000)
            if q in e["name"].lower()][:limit]


async def node_system(server_id: str | None = None) -> dict | None:
    """What the node a server runs on (or the default node) has: RAM, CPUs,
    load. None where the driver cannot say -- the mock, or a node down."""
    drv = await _driver(server_id) if server_id else _drivers.get(default_node())
    if drv is None or not hasattr(drv, "_call"):
        return None
    try:
        return await drv._call("GET", "/v1/system", timeout=10)
    except EngineError:
        return None


async def nodes_memory() -> list[dict]:
    """Per node: the memory its running servers' limits commit, against its
    RAM -- concurrently, and a node that does not answer is just left out."""
    _ensure()

    async def one(node_id: str, drv) -> dict | None:
        if not hasattr(drv, "_call"):
            return None
        try:
            sysinfo = await drv._call("GET", "/v1/system", timeout=3)
        except EngineError:
            return None
        return {"node": node_id, "total_mb": int((sysinfo.get("ram_bytes") or 0) // 1048576),
                "committed_mb": int((sysinfo.get("committed_bytes") or 0) // 1048576),
                "running": sysinfo.get("running")}
    found = await asyncio.gather(*(one(n, d) for n, d in list(_drivers.items())))
    return [f for f in found if f]


async def folder_usage(server_id: str) -> dict | None:
    """Bytes and files per top-level folder, measured on the node in one
    request -- or None where the driver cannot, and the caller walks."""
    drv = await _driver(server_id)
    own = getattr(drv, "folder_usage", None)
    return await own(server_id) if own is not None else None


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


async def keep_in_cache(server_id: str, path: str, sha1: str) -> bool:
    """Keep a file from the server's folder in its node's download cache, for
    the next install there. False where the driver has no such cache."""
    drv = await _driver(server_id)
    fn = getattr(drv, "keep_in_cache", None)
    if fn is None:
        return False
    await fn(server_id, path, sha1)
    return True


async def host_ports(server_id: str) -> list[dict]:
    """Host ports in use on the machine that runs this server, by anything
    (other apps' containers too); empty where the driver cannot say."""
    drv = await _driver(server_id)
    fn = getattr(drv, "host_ports", None)
    return [] if fn is None else await fn(server_id)


async def node_host_ports(node: str | None = None) -> list[dict]:
    """host_ports for a node rather than a server: where a new server would
    go (the default node) when no node is named."""
    _ensure()
    drv = _drivers.get(node or default_node()) if _drivers else None
    fn = getattr(drv, "host_ports", None)
    return [] if fn is None else await fn("")


async def console_since(server_id: str, after: int | None) -> dict | None:
    """The numbered console after `after`, or None for a driver whose
    console is not numbered (the in-memory engine)."""
    drv = await _driver(server_id)
    fn = getattr(drv, "console_since", None)
    return None if fn is None else await fn(server_id, after)


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
