"""Modrinth API client.

Modrinth needs no API key, which makes it a useful second source. It also
exposes explicit `client_side` / `server_side` support flags that CurseForge
lacks -- we reuse those to warn about client-only mods before they crash a
server, and to identify jars by SHA-1 hash.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from html import unescape
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
    loaders: list[str] | None = None,
    game_versions: list[str] | None = None,
) -> str:
    """Build Modrinth's facet expression.

    The nesting is the whole API here and it is easy to get backwards: the
    outer list is ANDed, each inner list is ORed. A single `loader` is one
    group of one; `loaders` is one group of many, which is how "any
    Bukkit-family plugin" is expressed -- most plugins tag `paper` OR
    `spigot` OR `bukkit` and never all three, so ANDing them returns almost
    nothing.
    """
    facets: list[list[str]] = []
    if project_type:
        facets.append([f"project_type:{project_type}"])
    if game_versions:
        facets.append([f"versions:{v}" for v in game_versions])
    elif game_version:
        facets.append([f"versions:{game_version}"])
    if loaders:
        facets.append([f"categories:{l.lower()}" for l in loaders])
    elif loader:
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
    loaders: list[str] | None = None,
    game_versions: list[str] | None = None,
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
    facets = _facets(project_type, game_version, loader, categories,
                     loaders=loaders, game_versions=game_versions)
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
    id_or_slug: str, *, game_version: str | None = None,
    loader: str | None = None, loaders: list[str] | None = None,
) -> list[dict]:
    params = {}
    if game_version:
        params["game_versions"] = json.dumps([game_version])
    if loaders:
        params["loaders"] = json.dumps([l.lower() for l in loaders])
    elif loader:
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


# --- project detail ----------------------------------------------------
#
# What the detail popup on a mod or plugin row reads.
#
# Modrinth's long description is `body`, and it is Markdown -- with raw HTML
# allowed inside it, which plenty of projects use for banners and badges.
# Like the CurseForge description it is written by a third party, and like
# that one it never reaches the browser as markup: it is flattened here into
# the same list of typed text blocks, so the front end can only ever put it
# through `textContent`.
#
# Every URL handed over as an `href` is filtered to http(s) first, for the
# same reason: `source_url`, `issues_url`, `wiki_url` and `discord_url` are
# free-text fields on somebody else's project page.

_SAFE_SCHEMES = ("http://", "https://")

_BLOCK_CHARS = 1400
_MAX_BLOCKS = 140
_MAX_BODY = 200_000


def safe_url(url: Any) -> str | None:
    """An upstream URL, or None unless it is plainly http(s)."""
    if not url:
        return None
    text = str(url).strip()
    return text if text.lower().startswith(_SAFE_SCHEMES) else None


_FENCE = re.compile(r"^\s*(?:```|~~~)")
_HEAD = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_LI = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_RULE = re.compile(r"^\s{0,3}(?:-\s*){3,}$|^\s{0,3}(?:\*\s*){3,}$"
                   r"|^\s{0,3}(?:_\s*){3,}$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^<>]{0,400}>")
_SETEXT_H1 = re.compile(r"^\s{0,3}={3,}\s*$")
_SETEXT_H2 = re.compile(r"^\s{0,3}-{3,}\s*$")


def _inline(text: str) -> str:
    """Markdown (and the raw HTML inside it) down to plain words.

    Link destinations are dropped and only the link text kept. A body is the
    one place a project author could put an arbitrary destination in front of
    an operator, and nothing in this popup is worth that; the project's own
    source/issues/wiki links come from named fields instead.
    """
    text = _IMG.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _BR.sub(" ", text)
    text = _TAG.sub("", text)
    text = unescape(text)
    # Emphasis markers, unwound narrowly. A blanket strip of `_` would eat
    # the underscores out of config_file.json and every mod id in the body.
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*(?!\s)([^*]+?)(?<!\s)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_(?!\s)([^_]+?)(?<!\s)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return " ".join(text.split())


def body_blocks(markdown: Any) -> list[dict]:
    """Flatten a Modrinth body into blocks the front end can draw."""
    if not markdown or not isinstance(markdown, str):
        return []
    blocks: list[dict] = []
    para: list[str] = []
    fence: list[str] | None = None

    def flush() -> None:
        if not para:
            return
        text = _inline(" ".join(para))
        para.clear()
        if text and len(blocks) < _MAX_BLOCKS:
            blocks.append({"t": "p", "text": text[:_BLOCK_CHARS]})

    def add(kind: str, text: str, level: int = 0) -> None:
        if not text or len(blocks) >= _MAX_BLOCKS:
            return
        block: dict[str, Any] = {"t": kind, "text": text[:_BLOCK_CHARS]}
        if kind == "h":
            block["level"] = level or 2
        blocks.append(block)

    for line in markdown[:_MAX_BODY].splitlines():
        if _FENCE.match(line):
            if fence is None:
                flush()
                fence = []
            else:
                add("code", " ".join(" ".join(fence).split()))
                fence = None
            continue
        if fence is not None:
            fence.append(line)
            continue

        if not line.strip():
            flush()
            continue
        if _RULE.match(line):
            flush()
            if len(blocks) < _MAX_BLOCKS:
                blocks.append({"t": "rule"})
            continue
        # Setext headings underline the line above, so they only make sense
        # once there is a paragraph waiting to be promoted.
        if para and (_SETEXT_H1.match(line) or _SETEXT_H2.match(line)):
            text = _inline(" ".join(para))
            para.clear()
            add("h", text, 1 if _SETEXT_H1.match(line) else 2)
            continue

        head = _HEAD.match(line)
        if head:
            flush()
            add("h", _inline(head.group(2)), len(head.group(1)))
            continue
        quote = _QUOTE.match(line)
        if quote:
            flush()
            add("quote", _inline(quote.group(1)))
            continue
        item = _LI.match(line)
        if item:
            flush()
            add("li", _inline(item.group(1)))
            continue
        if line.lstrip().startswith("|"):
            flush()
            if _TABLE_SEP.match(line):
                continue
            cells = [_inline(c) for c in line.strip().strip("|").split("|")]
            add("p", " · ".join(c for c in cells if c))
            continue
        para.append(line)

    if fence:
        add("code", " ".join(" ".join(fence).split()))
    flush()
    return blocks[:_MAX_BLOCKS]


def _gallery(p: dict) -> list[dict]:
    shots = sorted(p.get("gallery") or [],
                   key=lambda g: (not g.get("featured"), g.get("ordering") or 0))
    out = []
    for shot in shots[:8]:
        full = safe_url(shot.get("raw_url")) or safe_url(shot.get("url"))
        if not full:
            continue
        out.append({
            "url": full,
            "thumb": safe_url(shot.get("url")) or full,
            "title": shot.get("title") or "",
            "description": shot.get("description") or "",
        })
    return out


def _license(p: dict) -> dict | None:
    lic = p.get("license") or {}
    ident = lic.get("id") or ""
    name = lic.get("name") or ""
    if not ident and not name:
        return None
    return {"id": ident, "name": name, "url": safe_url(lic.get("url"))}


async def project_members(id_or_slug: str) -> list[str]:
    """Who is on the project's team, owner first.

    `/search` hands back a single `author` string but `/project` hands back
    none at all, so a detail view that only read the project would show a
    mod with no author on it.
    """
    try:
        data = await _get(f"/project/{id_or_slug}/members") or []
    except Exception:
        return []
    rows = [m for m in data if m.get("accepted") is not False]
    rows.sort(key=lambda m: (str(m.get("role") or "").lower() != "owner",
                             m.get("ordering") or 0))
    names = []
    for member in rows:
        name = (member.get("user") or {}).get("username")
        if name and name not in names:
            names.append(name)
    return names[:6]


async def project_detail(
    id_or_slug: str,
    *,
    game_version: str | None = None,
    loader: str | None = None,
    loaders: list[str] | None = None,
) -> dict:
    """Everything the detail popup shows for one Modrinth project.

    The project is fatal if it fails -- without it there is nothing to draw.
    The team and the build on offer are both best-effort: a project with no
    published build for this (Minecraft, loader) pair is an ordinary state,
    not an error worth putting in front of anyone.
    """
    p = await _get(f"/project/{id_or_slug}")
    if not p:
        raise ModrinthError(f"Modrinth has no project '{id_or_slug}'")

    async def _offer() -> dict | None:
        try:
            items = await list_versions(
                id_or_slug, game_version=game_version, loader=loader,
                loaders=loaders,
            )
        except Exception:
            return None
        return items[0] if items else None

    authors, offer = await asyncio.gather(
        project_members(id_or_slug), _offer())

    kind = p.get("project_type") or "mod"
    categories = list(p.get("categories") or [])
    for extra in p.get("additional_categories") or []:
        if extra not in categories:
            categories.append(extra)

    return {
        "source": "modrinth",
        "kind": kind,
        "id": p.get("id"),
        "slug": p.get("slug"),
        "name": p.get("title"),
        "summary": p.get("description"),
        "downloads": p.get("downloads"),
        "follows": p.get("followers"),
        "logo": p.get("icon_url"),
        "logo_full": safe_url(p.get("raw_icon_url")) or p.get("icon_url"),
        "url": f"https://modrinth.com/{kind}/{p.get('slug')}",
        "authors": authors,
        "categories": categories,
        "client_side": p.get("client_side"),
        "server_side": p.get("server_side"),
        "game_versions": list(reversed(p.get("game_versions") or []))[:40],
        "loaders": p.get("loaders") or [],
        "date_created": p.get("published"),
        "date_modified": p.get("updated"),
        "date_released": p.get("approved"),
        "license": _license(p),
        "source_url": safe_url(p.get("source_url")),
        "issues_url": safe_url(p.get("issues_url")),
        "wiki_url": safe_url(p.get("wiki_url")),
        "discord_url": safe_url(p.get("discord_url")),
        "donation_urls": [
            {"platform": d.get("platform") or d.get("id") or "donate",
             "url": safe_url(d.get("url"))}
            for d in (p.get("donation_urls") or [])
            if safe_url(d.get("url"))
        ][:4],
        "gallery": _gallery(p),
        "body_blocks": body_blocks(p.get("body")),
        "body_format": "markdown",
        "offer": offer,
    }
