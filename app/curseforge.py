"""CurseForge Core API client.

Notable behaviours this client works around:

* `downloadUrl` is null for any file whose author disabled third-party
  distribution. The official `/download-url` endpoint returns empty for
  those too, but the CDN path is deterministic and still serves the file:
      https://edge.forgecdn.net/files/<id[:4]>/<id[4:]>/<fileName>
  (verified returning 200 for a file with a null downloadUrl).
* Search and file listing are paginated with a hard 10k index ceiling.
* Bulk endpoints (`POST /v1/mods`, `POST /v1/mods/files`) accept up to a few
  hundred ids per call and are dramatically faster than per-id requests --
  a 485-mod pack resolves in a handful of round trips.
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

import httpx

from app import config

GAME_ID = config.GAME_ID_MINECRAFT
CLASS_MODPACKS = config.CLASS_ID_MODPACKS
CLASS_MODS = config.CLASS_ID_MODS

# CurseForge modLoaderType enum, used when filtering files.
LOADER_TYPE = {
    "any": 0,
    "forge": 1,
    "cauldron": 2,
    "liteloader": 3,
    "fabric": 4,
    "quilt": 5,
    "neoforge": 6,
}

_BULK_CHUNK = 200


class CurseForgeError(RuntimeError):
    pass


def _headers() -> dict:
    if not config.CURSEFORGE_API_KEY:
        raise CurseForgeError("CURSEFORGE_API_KEY is not set")
    return {
        "x-api-key": config.CURSEFORGE_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _client(timeout: float = 45.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.CURSEFORGE_API_BASE, timeout=timeout, follow_redirects=True
    )


async def _get(path: str, params: dict | None = None) -> Any:
    async with _client() as c:
        r = await c.get(path, params=params, headers=_headers())
        if r.status_code == 403:
            raise CurseForgeError("CurseForge rejected the API key (403)")
        if r.status_code >= 400:
            raise CurseForgeError(f"CurseForge GET {path} -> {r.status_code}")
        return r.json().get("data")


async def _post(path: str, body: dict) -> Any:
    async with _client() as c:
        r = await c.post(path, json=body, headers=_headers())
        if r.status_code >= 400:
            raise CurseForgeError(f"CurseForge POST {path} -> {r.status_code}")
        return r.json().get("data")


# --- discovery ---------------------------------------------------------


async def search(
    *,
    query: str = "",
    class_id: int = CLASS_MODPACKS,
    game_version: str | None = None,
    mod_loader: str | None = None,
    category_id: int | None = None,
    sort_field: int = 2,  # 2 = Popularity, 6 = TotalDownloads, 3 = LastUpdated
    index: int = 0,
    page_size: int = 30,
) -> dict:
    params: dict[str, Any] = {
        "gameId": GAME_ID,
        "classId": class_id,
        "sortField": sort_field,
        "sortOrder": "desc",
        "index": index,
        "pageSize": min(page_size, 50),
    }
    if query:
        params["searchFilter"] = query
    if game_version:
        params["gameVersion"] = game_version
    if category_id:
        params["categoryId"] = category_id
    if mod_loader and mod_loader.lower() in LOADER_TYPE:
        params["modLoaderType"] = LOADER_TYPE[mod_loader.lower()]

    async with _client() as c:
        r = await c.get("/v1/mods/search", params=params, headers=_headers())
        if r.status_code >= 400:
            raise CurseForgeError(f"CurseForge search -> {r.status_code}")
        payload = r.json()
    return {
        "items": [_slim_project(m) for m in payload.get("data", [])],
        "pagination": payload.get("pagination", {}),
    }


def _slim_project(m: dict) -> dict:
    logo = (m.get("logo") or {}).get("thumbnailUrl") or (m.get("logo") or {}).get("url")
    return {
        "source": "curseforge",
        "id": m.get("id"),
        "slug": m.get("slug"),
        "name": m.get("name"),
        "summary": m.get("summary"),
        "downloads": m.get("downloadCount"),
        "logo": logo,
        "url": (m.get("links") or {}).get("websiteUrl"),
        "authors": [a.get("name") for a in m.get("authors", [])],
        "categories": [c.get("name") for c in m.get("categories", [])],
        "updated": m.get("dateModified"),
        "latest_files": [_slim_file(f) for f in (m.get("latestFiles") or [])],
    }


def _slim_file(f: dict) -> dict:
    versions = f.get("gameVersions") or []
    loaders = [v for v in versions if v.lower() in
               ("forge", "fabric", "neoforge", "quilt", "liteloader")]
    mc = [v for v in versions if v and v[0].isdigit()]
    return {
        "source": "curseforge",
        "file_id": f.get("id"),
        "mod_id": f.get("modId"),
        "display_name": f.get("displayName"),
        "file_name": f.get("fileName"),
        "release_type": {1: "release", 2: "beta", 3: "alpha"}.get(
            f.get("releaseType"), "unknown"
        ),
        "date": f.get("fileDate"),
        "size": f.get("fileLength"),
        "download_url": f.get("downloadUrl"),
        "server_pack_file_id": f.get("serverPackFileId"),
        "is_server_pack": f.get("isServerPack", False),
        "game_versions": mc,
        "loaders": loaders,
        "dependencies": f.get("dependencies") or [],
        "hashes": f.get("hashes") or [],
    }


async def get_mod(mod_id: int) -> dict:
    return _slim_project(await _get(f"/v1/mods/{mod_id}"))


async def get_mods(mod_ids: Iterable[int]) -> dict[int, dict]:
    ids = list({int(i) for i in mod_ids})
    out: dict[int, dict] = {}
    for i in range(0, len(ids), _BULK_CHUNK):
        chunk = ids[i : i + _BULK_CHUNK]
        data = await _post("/v1/mods", {"modIds": chunk}) or []
        for m in data:
            out[m["id"]] = _slim_project(m)
    return out


async def get_file(mod_id: int, file_id: int) -> dict:
    return _slim_file(await _get(f"/v1/mods/{mod_id}/files/{file_id}"))


async def get_files(file_ids: Iterable[int]) -> dict[int, dict]:
    """Bulk-resolve file ids -> file metadata."""
    ids = list({int(i) for i in file_ids})
    out: dict[int, dict] = {}
    for i in range(0, len(ids), _BULK_CHUNK):
        chunk = ids[i : i + _BULK_CHUNK]
        data = await _post("/v1/mods/files", {"fileIds": chunk}) or []
        for f in data:
            out[f["id"]] = _slim_file(f)
    return out


async def list_files(
    mod_id: int,
    *,
    game_version: str | None = None,
    mod_loader: str | None = None,
    index: int = 0,
    page_size: int = 50,
) -> list[dict]:
    params: dict[str, Any] = {"index": index, "pageSize": min(page_size, 50)}
    if game_version:
        params["gameVersion"] = game_version
    if mod_loader and mod_loader.lower() in LOADER_TYPE:
        params["modLoaderType"] = LOADER_TYPE[mod_loader.lower()]
    data = await _get(f"/v1/mods/{mod_id}/files", params) or []
    return [_slim_file(f) for f in data]


async def get_categories(class_id: int = CLASS_MODPACKS) -> list[dict]:
    data = await _get("/v1/categories", {"gameId": GAME_ID, "classId": class_id}) or []
    return [
        {"id": c["id"], "name": c["name"], "slug": c.get("slug"), "icon": c.get("iconUrl")}
        for c in data
    ]


async def get_minecraft_versions() -> list[str]:
    data = await _get("/v1/minecraft/version") or []
    return [v["versionString"] for v in data]


# --- downloading -------------------------------------------------------


def cdn_url(file_id: int, file_name: str) -> str:
    """Deterministic CDN path, used when downloadUrl is null."""
    fid = str(file_id)
    return (
        f"https://edge.forgecdn.net/files/{fid[:4]}/{fid[4:]}/"
        f"{urllib.parse.quote(file_name)}"
    )


def resolve_download_url(file_meta: dict) -> str:
    url = file_meta.get("download_url")
    if url:
        return url
    fid = file_meta.get("file_id")
    name = file_meta.get("file_name")
    if not fid or not name:
        raise CurseForgeError("cannot resolve a download URL for this file")
    return cdn_url(fid, name)


def _jar_cache_path(file_meta: dict) -> "Path | None":
    fid = file_meta.get("file_id")
    name = file_meta.get("file_name")
    if not fid or not name:
        return None
    safe = re.sub(r"[^\w.\-]+", "_", str(name))
    return config.CACHE_DIR / "mods" / f"{fid}-{safe}"


async def download_cached(
    file_meta: dict, client: httpx.AsyncClient | None = None
) -> bytes:
    """Download a mod jar, reusing the on-disk copy when we already have it.

    Preflight reads every jar's metadata to decide which are client-only, and
    the install then needs those same jars. Caching means the review step
    costs one download instead of two.
    """
    path = _jar_cache_path(file_meta)
    expected = file_meta.get("size") or 0
    if path and path.exists():
        try:
            data = path.read_bytes()
            if not expected or abs(len(data) - expected) < 1024:
                return data
        except OSError:
            pass

    data = await download(file_meta, client)
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            # A read-only or mis-owned cache dir must never break an install.
            pass
    return data


async def download(file_meta: dict, client: httpx.AsyncClient | None = None) -> bytes:
    """Fetch a file's bytes, falling back to the CDN path when needed."""
    urls = []
    if file_meta.get("download_url"):
        urls.append(file_meta["download_url"])
    if file_meta.get("file_id") and file_meta.get("file_name"):
        fallback = cdn_url(file_meta["file_id"], file_meta["file_name"])
        if fallback not in urls:
            urls.append(fallback)
    if not urls:
        raise CurseForgeError(f"no download URL for {file_meta.get('file_name')}")

    own = client is None
    c = client or httpx.AsyncClient(timeout=600, follow_redirects=True)
    try:
        last: Exception | None = None
        for url in urls:
            for attempt in range(3):
                try:
                    r = await c.get(url)
                    if r.status_code == 200 and r.content:
                        return r.content
                    last = CurseForgeError(f"{url} -> {r.status_code}")
                except Exception as e:  # network hiccup: retry with backoff
                    last = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise CurseForgeError(
            f"failed to download {file_meta.get('file_name')}: {last}"
        )
    finally:
        if own:
            await c.aclose()


# --- fingerprinting ----------------------------------------------------


def murmur2(data: bytes, seed: int = 1) -> int:
    m = 0x5BD1E995
    r = 24
    length = len(data)
    h = (seed ^ length) & 0xFFFFFFFF
    i = 0
    while length - i >= 4:
        k = int.from_bytes(data[i : i + 4], "little")
        k = (k * m) & 0xFFFFFFFF
        k ^= k >> r
        k = (k * m) & 0xFFFFFFFF
        h = (h * m) & 0xFFFFFFFF
        h ^= k
        i += 4
    rem = length - i
    if rem == 3:
        h ^= data[i] | (data[i + 1] << 8)
        h ^= data[i + 2] << 16
        h = (h * m) & 0xFFFFFFFF
    elif rem == 2:
        h ^= data[i] | (data[i + 1] << 8)
        h = (h * m) & 0xFFFFFFFF
    elif rem == 1:
        h ^= data[i]
        h = (h * m) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * m) & 0xFFFFFFFF
    h ^= h >> 15
    return h


def fingerprint(data: bytes) -> int:
    """CurseForge's murmur2 variant: whitespace bytes are stripped first."""
    filtered = bytes(b for b in data if b not in (9, 10, 13, 32))
    return murmur2(filtered, 1)


async def match_fingerprints(prints: list[int]) -> dict[int, dict]:
    """Identify unknown jars. Returns {fingerprint: {mod, file}}."""
    if not prints:
        return {}
    out: dict[int, dict] = {}
    for i in range(0, len(prints), _BULK_CHUNK):
        chunk = prints[i : i + _BULK_CHUNK]
        data = await _post(f"/v1/fingerprints/{GAME_ID}", {"fingerprints": chunk})
        for match in (data or {}).get("exactMatches", []):
            f = match.get("file") or {}
            out[f.get("fileFingerprint")] = {
                "mod_id": match.get("id"),
                "file": _slim_file(f),
            }
    return out
