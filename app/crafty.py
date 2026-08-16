"""Crafty Controller 4 API v2 client.

Every endpoint here was verified against a live Crafty 4.x instance and,
where the docs are silent, against Crafty's own request handlers. The
non-obvious parts are called out because getting them wrong fails silently:

  list dir / read file  POST   /api/v2/servers/{id}/files
                        body {"page": p, "path": p}  (GET is 405 -- the
                        handler only implements post())
  write file            PATCH  /api/v2/servers/{id}/files
                        body {"path", "contents", "overwrite": true}
                        Without overwrite the handler compares mtime against
                        `modified_epoch` and returns a bare 409.
  rename                PATCH  /api/v2/servers/{id}/files/create
                        body {"path", "new_name"}  (new_name is a bare name,
                        not a path)
  create file/dir       PUT    /api/v2/servers/{id}/files/create
                        body {"parent", "name", "directory"}
  delete                DELETE /api/v2/servers/{id}/files
                        body {"file_system_objects": [{"filename": rel}]}
  upload                POST   /api/v2/servers/{id}/files/upload
                        headers fileId/fileName/location/fileSize, raw body.
                        `fileId` is mandatory even for non-chunked uploads
                        (the handler builds a temp path from it and 500s on
                        None), and `location` must be non-empty -- use "."
                        for the server root.
  unzip                 POST   /api/v2/servers/{id}/files/zip
                        body {"folder": rel_zip}. Extracts into the zip's
                        PARENT dir and runs on a background thread, so the
                        200 means "started", not "done".
  download              GET    /api/v2/servers/{id}/files/{urlquoted}/download
                        Path is URL-encoded, not base64.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import urllib.parse
import uuid
from typing import Any

import httpx

from app import config


class CraftyError(RuntimeError):
    """A Crafty API call failed."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _headers(extra: dict | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {config.CRAFTY_TOKEN}",
        "Accept": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _client(timeout: float = 60.0) -> httpx.AsyncClient:
    if not config.CRAFTY_URL or not config.CRAFTY_TOKEN:
        raise CraftyError("Crafty is not configured: set CRAFTY_URL and CRAFTY_TOKEN")
    return httpx.AsyncClient(
        base_url=config.CRAFTY_URL,
        verify=config.CRAFTY_VERIFY_SSL,
        timeout=timeout,
        follow_redirects=True,
    )


def _unwrap(resp: httpx.Response, what: str) -> Any:
    if resp.status_code >= 400:
        # Crafty returns JSON errors for most failures but raw tracebacks for
        # a few (e.g. a missing upload header), so degrade gracefully.
        try:
            body = resp.json()
            detail = body.get("error_data") or body.get("error") or body
        except Exception:
            body = resp.text[:600]
            detail = body
        raise CraftyError(f"{what} failed: {detail}", resp.status_code, body)
    if not resp.content:
        return None
    try:
        payload = resp.json()
    except Exception:
        return resp.text
    if isinstance(payload, dict) and payload.get("status") == "error":
        raise CraftyError(
            f"{what} failed: {payload.get('error_data') or payload.get('error')}",
            resp.status_code,
            payload,
        )
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


# --- servers -----------------------------------------------------------


async def list_servers() -> list[dict]:
    async with _client() as c:
        return _unwrap(
            await c.get("/api/v2/servers", headers=_headers()), "list servers"
        )


async def get_server(server_id: str) -> dict:
    async with _client() as c:
        return _unwrap(
            await c.get(f"/api/v2/servers/{server_id}", headers=_headers()),
            "get server",
        )


async def get_stats(server_id: str) -> dict:
    async with _client() as c:
        return _unwrap(
            await c.get(f"/api/v2/servers/{server_id}/stats", headers=_headers()),
            "get stats",
        )


async def create_server(
    *,
    name: str,
    loader_type: str,
    mc_version: str,
    mem_min: int,
    mem_max: int,
    port: int,
    category: str = "mc_java_servers",
) -> str:
    """Create a server via Crafty's own jar catalog and return its id.

    `loader_type` must be a catalog key: vanilla, fabric, forge-installer,
    neoforge-installer, paper, purpur, folia...
    """
    body = {
        "name": name,
        "roles": [],
        "monitoring_type": "minecraft_java",
        "minecraft_java_monitoring_data": {"host": "127.0.0.1", "port": port},
        "create_type": "minecraft_java",
        "minecraft_java_create_data": {
            "create_type": "download_jar",
            "download_jar_create_data": {
                "category": category,
                "type": loader_type,
                "version": mc_version,
                "mem_min": mem_min,
                "mem_max": mem_max,
                "server_properties_port": port,
                "agree_to_eula": True,
            },
        },
    }
    async with _client(timeout=120) as c:
        data = _unwrap(
            await c.post(
                "/api/v2/servers",
                headers=_headers({"Content-Type": "application/json"}),
                content=json.dumps(body),
            ),
            "create server",
        )
    sid = data.get("new_server_id") or data.get("new_server_uuid")
    if not sid:
        raise CraftyError(f"Crafty did not return a server id: {data}")
    return sid


async def delete_server(server_id: str, delete_files: bool = True) -> None:
    async with _client(timeout=120) as c:
        _unwrap(
            await c.delete(
                f"/api/v2/servers/{server_id}",
                params={"files": "true" if delete_files else "false"},
                headers=_headers(),
            ),
            "delete server",
        )


# Crafty gates every start on this file, comparing `readline()` against a
# fixed list of exact strings ("eula=true", "eula = true", ...). readline()
# keeps the trailing newline, so a file written as "eula=true\n" never
# matches and Crafty refuses to launch -- with no log and no error, because
# it just pushes an "agree to the EULA" prompt to the web UI instead.
# The byte-exact form below is what Crafty itself writes.
EULA_ACCEPTED = "eula=true"


async def patch_server(server_id: str, fields: dict) -> None:
    async with _client(timeout=60) as c:
        _unwrap(
            await c.patch(
                f"/api/v2/servers/{server_id}",
                headers=_headers({"Content-Type": "application/json"}),
                content=json.dumps(fields),
            ),
            "patch server",
        )


# Minecraft version -> the Java major it must run on. Newer Javas are not
# automatically safe: NeoForge/Forge reject anything past the version they
# were built for, which is why a host defaulting to Java 25 silently kills
# a 1.21.1 server at startup.
def required_java_major(mc_version: str) -> int:
    try:
        parts = [int(p) for p in (mc_version or "").split(".")[:3]]
    except ValueError:
        return 21
    if not parts:
        return 21
    major, minor = (parts + [0, 0])[:2]
    patch = parts[2] if len(parts) > 2 else 0
    if major != 1:
        return 21
    if minor > 20 or (minor == 20 and patch >= 5):
        return 21
    if minor >= 17:
        return 17
    return 8


def java_candidates(major: int) -> list[str]:
    """Standard Debian/Ubuntu JVM paths, matching the official Crafty image."""
    if major == 8:
        return [
            "/usr/lib/jvm/java-8-openjdk-amd64/jre/bin/java",
            "/usr/lib/jvm/java-1.8.0-openjdk-amd64/jre/bin/java",
            "/usr/lib/jvm/java-8-openjdk-amd64/bin/java",
        ]
    return [
        f"/usr/lib/jvm/java-{major}-openjdk-amd64/bin/java",
        f"/usr/lib/jvm/java-1.{major}.0-openjdk-amd64/bin/java",
        f"/usr/lib/jvm/java-{major}-openjdk/bin/java",
        f"/opt/java/openjdk-{major}/bin/java",
    ]


async def set_java_version(server_id: str, mc_version: str) -> dict:
    """Point an instance at a Java runtime its loader actually supports.

    Crafty's own handler has a sharp edge here: if the requested path is not
    in its detected install list, it stores `None` as the execution command
    and the server can never start. So we snapshot the command first and put
    it back if a candidate turns out to be invalid.
    """
    major = required_java_major(mc_version)
    server = await get_server(server_id)
    original = server.get("execution_command") or ""

    if not original:
        return {"changed": False, "reason": "server has no execution command yet"}
    # Already pointing at the right major -- leave it alone.
    if f"java-{major}-" in original or f"java-1.{major}.0-" in original:
        return {"changed": False, "java_major": major, "reason": "already correct"}

    for candidate in java_candidates(major):
        try:
            await patch_server(server_id, {"java_selection": candidate})
        except CraftyError:
            continue
        updated = (await get_server(server_id)).get("execution_command") or ""
        if candidate in updated:
            # Crafty regenerates the launch command when a loader install
            # finishes, which can silently undo this. Confirm it survives.
            await asyncio.sleep(6)
            final = (await get_server(server_id)).get("execution_command") or ""
            if candidate not in final:
                await patch_server(server_id, {"java_selection": candidate})
                final = (await get_server(server_id)).get("execution_command") or ""
            return {
                "changed": True,
                "java_major": major,
                "java_path": candidate,
                "verified": candidate in final,
            }
        # Crafty rejected it and may have blanked the command -- restore.
        if not updated.strip():
            await patch_server(server_id, {"execution_command": original})

    return {
        "changed": False,
        "java_major": major,
        "reason": f"no Java {major} runtime found on the Crafty host",
    }


async def server_action(server_id: str, action: str) -> None:
    """action: start_server | stop_server | restart_server | kill_server"""
    async with _client(timeout=120) as c:
        _unwrap(
            await c.post(
                f"/api/v2/servers/{server_id}/action/{action}", headers=_headers()
            ),
            f"action {action}",
        )


# --- files -------------------------------------------------------------


async def list_dir(server_id: str, path: str = ".") -> dict:
    """Return {name: meta} for a directory. `path` is relative to server root."""
    async with _client() as c:
        data = _unwrap(
            await c.post(
                f"/api/v2/servers/{server_id}/files",
                headers=_headers({"Content-Type": "application/json"}),
                content=json.dumps({"page": path, "path": path}),
            ),
            f"list {path}",
        )
    return data or {}


async def dir_exists(server_id: str, path: str) -> bool:
    try:
        await list_dir(server_id, path)
        return True
    except CraftyError:
        return False


async def read_file(server_id: str, path: str) -> str:
    """Read a UTF-8 text file. Raises CraftyError for binary/missing files."""
    async with _client() as c:
        data = _unwrap(
            await c.post(
                f"/api/v2/servers/{server_id}/files",
                headers=_headers({"Content-Type": "application/json"}),
                content=json.dumps({"page": path, "path": path}),
            ),
            f"read {path}",
        )
    if isinstance(data, dict) and "content" in data:
        return data["content"]
    raise CraftyError(f"{path} is not a readable text file")


async def write_file(server_id: str, path: str, contents: str) -> None:
    async with _client() as c:
        resp = await c.patch(
            f"/api/v2/servers/{server_id}/files",
            headers=_headers({"Content-Type": "application/json"}),
            content=json.dumps({"path": path, "contents": contents, "overwrite": True}),
        )
        if resp.status_code == 409:
            raise CraftyError(f"{path} changed on disk since it was read", 409)
        _unwrap(resp, f"write {path}")


async def delete_paths(server_id: str, rel_paths: list[str]) -> None:
    if not rel_paths:
        return
    body = {"file_system_objects": [{"filename": p} for p in rel_paths]}
    async with _client(timeout=120) as c:
        _unwrap(
            await c.request(
                "DELETE",
                f"/api/v2/servers/{server_id}/files",
                headers=_headers({"Content-Type": "application/json"}),
                content=json.dumps(body),
            ),
            "delete files",
        )


async def rename_path(server_id: str, path: str, new_name: str) -> None:
    """Rename within the same directory. `new_name` is a bare filename."""
    async with _client() as c:
        _unwrap(
            await c.patch(
                f"/api/v2/servers/{server_id}/files/create",
                headers=_headers({"Content-Type": "application/json"}),
                content=json.dumps({"path": path, "new_name": new_name}),
            ),
            f"rename {path}",
        )


async def create_entry(
    server_id: str, parent: str, name: str, directory: bool = False
) -> None:
    async with _client() as c:
        try:
            _unwrap(
                await c.put(
                    f"/api/v2/servers/{server_id}/files/create",
                    headers=_headers({"Content-Type": "application/json"}),
                    content=json.dumps(
                        {"parent": parent, "name": name, "directory": directory}
                    ),
                ),
                f"create {name}",
            )
        except CraftyError as e:
            # "already exists" is success for our purposes (mkdir -p semantics).
            if e.body and "FILE EXISTS" in str(e.body):
                return
            raise


async def ensure_dir(server_id: str, rel_dir: str) -> None:
    """mkdir -p, one level at a time (Crafty has no recursive create)."""
    parts = [p for p in rel_dir.replace("\\", "/").split("/") if p and p != "."]
    parent = "."
    for part in parts:
        await create_entry(server_id, parent, part, directory=True)
        parent = part if parent == "." else posixpath.join(parent, part)


# Crafty's own UI chunks at 10 MB, and Tornado rejects very large single
# bodies, so anything above this goes through the chunked protocol.
CHUNK_THRESHOLD = 8 * 1024 * 1024
CHUNK_SIZE = 8 * 1024 * 1024


def _upload_headers(filename: str, location: str, total_size: int, file_id: str,
                    extra: dict | None = None) -> dict:
    h = _headers(
        {
            "fileId": file_id,
            "fileName": filename,
            "location": location,
            "fileSize": str(total_size),
            "Content-Type": "application/octet-stream",
        }
    )
    if extra:
        h.update(extra)
    return h


async def upload_file(
    server_id: str, location: str, filename: str, payload: bytes
) -> None:
    """Upload bytes into `location` (relative dir; "." for server root).

    Small files go through in one request. Larger ones use Crafty's chunked
    protocol: Tornado refuses oversized single bodies, which is exactly why
    Crafty's own UI chunks every upload. Each chunk carries a SHA-256 that
    the server verifies before accepting it.
    """
    loc = location.strip().strip("/") or "."
    file_id = str(uuid.uuid4())
    url = f"/api/v2/servers/{server_id}/files/upload"

    if len(payload) <= CHUNK_THRESHOLD:
        async with _client(timeout=600) as c:
            _unwrap(
                await c.post(
                    url,
                    headers=_upload_headers(filename, loc, len(payload), file_id),
                    content=payload,
                ),
                f"upload {filename}",
            )
        return

    total = len(payload)
    total_chunks = (total + CHUNK_SIZE - 1) // CHUNK_SIZE

    async with _client(timeout=1800) as c:
        # Initiation request: chunked, but no chunkId yet.
        _unwrap(
            await c.post(
                url,
                headers=_upload_headers(
                    filename, loc, total, file_id,
                    {"chunked": "true", "totalChunks": str(total_chunks)},
                ),
            ),
            f"begin upload {filename}",
        )

        for index in range(total_chunks):
            start = index * CHUNK_SIZE
            chunk = payload[start : start + CHUNK_SIZE]
            headers = _upload_headers(
                filename, loc, total, file_id,
                {
                    "chunked": "true",
                    "totalChunks": str(total_chunks),
                    "chunkId": str(index),
                    "chunkHash": hashlib.sha256(chunk).hexdigest(),
                    "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}",
                },
            )
            last: Exception | None = None
            for attempt in range(3):
                try:
                    _unwrap(
                        await c.post(url, headers=headers, content=chunk),
                        f"upload {filename} chunk {index + 1}/{total_chunks}",
                    )
                    last = None
                    break
                except (httpx.TransportError, CraftyError) as e:
                    last = e
                    await asyncio.sleep(1.5 * (attempt + 1))
            if last:
                raise last


async def unzip(server_id: str, rel_zip_path: str) -> None:
    """Ask Crafty to extract a zip already sitting in the server directory.

    Extraction happens on a daemon thread, so a 200 only means it started --
    callers must poll for the expected output (see wait_for_path).
    """
    async with _client(timeout=120) as c:
        _unwrap(
            await c.post(
                f"/api/v2/servers/{server_id}/files/zip",
                headers=_headers({"Content-Type": "application/json"}),
                content=json.dumps({"folder": rel_zip_path}),
            ),
            f"unzip {rel_zip_path}",
        )


async def download_file(server_id: str, rel_path: str) -> bytes:
    quoted = urllib.parse.quote(rel_path, safe="")
    async with _client(timeout=600) as c:
        resp = await c.get(
            f"/api/v2/servers/{server_id}/files/{quoted}/download", headers=_headers()
        )
        if resp.status_code >= 400:
            raise CraftyError(f"download {rel_path} failed", resp.status_code)
        return resp.content


async def wait_for_path(
    server_id: str, rel_path: str, timeout: float, poll: float = 3.0
) -> bool:
    """Poll until `rel_path` exists (file or dir). Returns False on timeout."""
    parent = posixpath.dirname(rel_path.rstrip("/")) or "."
    target = posixpath.basename(rel_path.rstrip("/"))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            entries = await list_dir(server_id, parent)
            if target in entries:
                return True
        except CraftyError:
            pass
        await asyncio.sleep(poll)
    return False


async def walk(server_id: str, root: str = ".", max_entries: int = 20000) -> list[dict]:
    """Recursively list files under `root`. Used by the config browser."""
    out: list[dict] = []
    queue = [root]
    seen = 0
    while queue and seen < max_entries:
        current = queue.pop(0)
        try:
            entries = await list_dir(server_id, current)
        except CraftyError:
            continue
        for name, meta in entries.items():
            if name == "root_path" or not isinstance(meta, dict):
                continue
            rel = meta.get("path") or (
                name if current == "." else posixpath.join(current, name)
            )
            rel = rel.replace("\\", "/")
            seen += 1
            if meta.get("dir"):
                queue.append(rel)
                out.append({"path": rel, "name": name, "dir": True})
            else:
                out.append(
                    {
                        "path": rel,
                        "name": name,
                        "dir": False,
                        "size": meta.get("size"),
                        "modified": meta.get("modified"),
                        "mime": meta.get("mime"),
                    }
                )
    return out


# --- studio manifest ---------------------------------------------------


async def read_studio_manifest(server_id: str) -> dict:
    """Our own per-instance record of what was installed and from where."""
    for name in (config.STUDIO_MANIFEST, *config.LEGACY_MANIFESTS):
        try:
            raw = await read_file(server_id, name)
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


async def write_studio_manifest(server_id: str, data: dict) -> None:
    payload = json.dumps(data, indent=2)
    try:
        await write_file(server_id, config.STUDIO_MANIFEST, payload)
    except CraftyError:
        # File does not exist yet -- create then write.
        await create_entry(server_id, ".", config.STUDIO_MANIFEST, directory=False)
        await write_file(server_id, config.STUDIO_MANIFEST, payload)
