"""Modrinth API client.

Modrinth needs no API key, which makes it a useful second source. It also
exposes explicit `client_side` / `server_side` support flags that CurseForge
lacks -- we reuse those to warn about client-only mods before they crash a
server, and to identify jars by SHA-1 hash.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

import httpx

from app import config

UA = "crafty-modpack-studio/1.0 (self-hosted; +https://github.com/)"


class ModrinthError(RuntimeError):
    pass


def _client(timeout: float = 45.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.MODRINTH_API_BASE,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )


async def _get(path: str, params: dict | None = None) -> Any:
    async with _client() as c:
        r = await c.get(path, params=params)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise ModrinthError(f"Modrinth GET {path} -> {r.status_code}")
        return r.json()


def _facets(
    project_type: str | None,
    game_version: str | None,
    loader: str | None,
    categories: list[str] | None = None,
) -> str:
    facets: list[list[str]] = []
    if project_type:
        facets.append([f"project_type:{project_type}"])
    if game_version:
        facets.append([f"versions:{game_version}"])
    if loader:
        facets.append([f"categories:{loader.lower()}"])
    for cat in categories or []:
        facets.append([f"categories:{cat}"])
    return json.dumps(facets)


async def search(
    *,
    query: str = "",
    project_type: str = "mod",
    game_version: str | None = None,
    loader: str | None = None,
    categories: list[str] | None = None,
    index: int = 0,
    page_size: int = 30,
    sort: str = "relevance",
) -> dict:
    params: dict[str, Any] = {
        "limit": min(page_size, 100),
        "offset": index,
        "index": sort if sort in ("relevance", "downloads", "follows", "newest",
                                  "updated") else "relevance",
    }
    if query:
        params["query"] = query
    facets = _facets(project_type, game_version, loader, categories)
    if facets != "[]":
        params["facets"] = facets

    data = await _get("/search", params) or {}
    return {
        "items": [_slim_hit(h) for h in data.get("hits", [])],
        "pagination": {
            "index": data.get("offset", 0),
            "pageSize": data.get("limit", 0),
            "totalCount": data.get("total_hits", 0),
        },
    }


def _slim_hit(h: dict) -> dict:
    return {
        "source": "modrinth",
        "id": h.get("project_id"),
        "slug": h.get("slug"),
        "name": h.get("title"),
        "summary": h.get("description"),
        "downloads": h.get("downloads"),
        "logo": h.get("icon_url"),
        "url": f"https://modrinth.com/{h.get('project_type','mod')}/{h.get('slug')}",
        "authors": [h.get("author")] if h.get("author") else [],
        "categories": h.get("categories", []),
        "updated": h.get("date_modified"),
        "client_side": h.get("client_side"),
        "server_side": h.get("server_side"),
        "game_versions": h.get("versions", []),
        "latest_files": [],
    }


async def get_project(id_or_slug: str) -> dict | None:
    p = await _get(f"/project/{id_or_slug}")
    if not p:
        return None
    return {
        "source": "modrinth",
        "id": p.get("id"),
        "slug": p.get("slug"),
        "name": p.get("title"),
        "summary": p.get("description"),
        "downloads": p.get("downloads"),
        "logo": p.get("icon_url"),
        "url": f"https://modrinth.com/{p.get('project_type','mod')}/{p.get('slug')}",
        "categories": p.get("categories", []),
        "client_side": p.get("client_side"),
        "server_side": p.get("server_side"),
        "game_versions": p.get("game_versions", []),
        "loaders": p.get("loaders", []),
        "body": p.get("body"),
    }


async def get_projects(ids: Iterable[str]) -> dict[str, dict]:
    ids = list({i for i in ids if i})
    if not ids:
        return {}
    data = await _get("/projects", {"ids": json.dumps(ids)}) or []
    out = {}
    for p in data:
        out[p["id"]] = {
            "source": "modrinth",
            "id": p.get("id"),
            "slug": p.get("slug"),
            "name": p.get("title"),
            "logo": p.get("icon_url"),
            "client_side": p.get("client_side"),
            "server_side": p.get("server_side"),
        }
    return out


def _slim_version(v: dict) -> dict:
    primary = None
    for f in v.get("files", []):
        if f.get("primary"):
            primary = f
            break
    primary = primary or (v.get("files") or [None])[0] or {}
    return {
        "source": "modrinth",
        "file_id": v.get("id"),
        "mod_id": v.get("project_id"),
        "display_name": v.get("name"),
        "version_number": v.get("version_number"),
        "file_name": primary.get("filename"),
        "download_url": primary.get("url"),
        "size": primary.get("size"),
        "sha1": (primary.get("hashes") or {}).get("sha1"),
        "release_type": v.get("version_type"),
        "date": v.get("date_published"),
        "game_versions": v.get("game_versions", []),
        "loaders": v.get("loaders", []),
        "dependencies": v.get("dependencies", []),
    }


async def list_versions(
    id_or_slug: str, *, game_version: str | None = None, loader: str | None = None
) -> list[dict]:
    params = {}
    if game_version:
        params["game_versions"] = json.dumps([game_version])
    if loader:
        params["loaders"] = json.dumps([loader.lower()])
    data = await _get(f"/project/{id_or_slug}/version", params) or []
    return [_slim_version(v) for v in data]


async def get_version(version_id: str) -> dict | None:
    v = await _get(f"/version/{version_id}")
    return _slim_version(v) if v else None


async def version_from_hash(sha1: str) -> dict | None:
    """Identify a jar by SHA-1 -- Modrinth's equivalent of CF fingerprints."""
    v = await _get(f"/version_file/{sha1}", {"algorithm": "sha1"})
    return _slim_version(v) if v else None


async def versions_from_hashes(hashes: list[str]) -> dict[str, dict]:
    if not hashes:
        return {}
    async with _client() as c:
        r = await c.post(
            "/version_files", json={"hashes": hashes, "algorithm": "sha1"}
        )
        if r.status_code >= 400:
            return {}
        data = r.json() or {}
    return {h: _slim_version(v) for h, v in data.items()}


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


async def download(file_meta: dict) -> bytes:
    url = file_meta.get("download_url")
    if not url:
        raise ModrinthError(f"no download URL for {file_meta.get('file_name')}")
    async with httpx.AsyncClient(
        timeout=600, follow_redirects=True, headers={"User-Agent": UA}
    ) as c:
        r = await c.get(url)
        if r.status_code != 200:
            raise ModrinthError(f"download failed: {url} -> {r.status_code}")
        return r.content
