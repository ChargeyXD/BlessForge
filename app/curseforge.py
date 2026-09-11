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
from html.parser import HTMLParser
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


async def latest_file_ids(
    mod_ids: list[int], game_version: str, loader: str
) -> dict[int, int]:
    """Newest file id per mod for one (Minecraft, loader) pair, in bulk.

    `POST /v1/mods` returns `latestFilesIndexes` for every mod in one call,
    which answers "is there anything newer?" for a whole server in a handful
    of requests instead of one per mod. Checked against the per-mod path over
    a 225-mod pack it agreed on every mod it could resolve.

    It does not resolve everything: an index entry can carry `modLoader: null`
    for a file that declares no loader, and some mods list no entry at all for
    a given pair. Those come back absent so the caller can fall back rather
    than silently report "up to date".
    """
    type_id = LOADER_TYPE.get((loader or "").lower())
    if not mod_ids or not game_version or not type_id:
        return {}
    out: dict[int, int] = {}
    ids = list({int(m) for m in mod_ids})
    for i in range(0, len(ids), _BULK_CHUNK):
        chunk = ids[i : i + _BULK_CHUNK]
        try:
            data = await _post("/v1/mods", {"modIds": chunk}) or []
        except Exception:
            continue
        for mod in data:
            hit = next(
                (x for x in (mod.get("latestFilesIndexes") or [])
                 if x.get("gameVersion") == game_version
                 and x.get("modLoader") == type_id),
                None,
            )
            if hit and hit.get("fileId"):
                out[mod["id"]] = hit["fileId"]
    return out


async def get_categories(class_id: int = CLASS_MODPACKS) -> list[dict]:
    data = await _get("/v1/categories", {"gameId": GAME_ID, "classId": class_id}) or []
    return [
        {"id": c["id"], "name": c["name"], "slug": c.get("slug"), "icon": c.get("iconUrl")}
        for c in data
    ]


async def get_minecraft_versions() -> list[str]:
    data = await _get("/v1/minecraft/version") or []
    return [v["versionString"] for v in data]


def _strip_mc_suffix(name: str, mc_version: str) -> str:
    """Catalogue names and manifest ids disagree for Fabric and Quilt.

    The catalogue lists 'fabric-0.19.5-1.21.11', but every published Fabric
    pack's manifest says 'fabric-0.19.5'. Forge and NeoForge names carry no
    Minecraft version to begin with, so they pass through untouched.
    """
    tail = f"-{mc_version}"
    return name[: -len(tail)] if mc_version and name.endswith(tail) else name


async def loader_build_id(mc_version: str, family: str) -> str:
    """The exact modLoader id CurseForge expects in a manifest, e.g.
    'forge-47.4.12', 'neoforge-21.1.99', 'fabric-0.19.5-1.21.11'.

    The shape is not uniform -- Forge omits the Minecraft version, Fabric and
    Quilt append it -- so the name is taken from the catalogue rather than
    assembled here. A manifest carrying a bare family name ('neoforge') is
    rejected by the CurseForge app with MinecraftUnsupportedModLoader.

    Returns "" when the catalogue has nothing for that pair, which leaves the
    caller to decide whether that is fatal.
    """
    type_id = LOADER_TYPE.get(family.lower())
    if not mc_version or not type_id:
        return ""
    try:
        data = await _get(
            "/v1/minecraft/modloader",
            {"version": mc_version, "includeAll": "true"},
        ) or []
    except Exception:
        return ""

    builds = [b for b in data if b.get("type") == type_id and b.get("name")]
    if not builds:
        return ""
    # NeoForge marks neither `recommended` nor `latest` on any build, so the
    # ordering fallback is not a rare path -- it is the only path for it. The
    # catalogue returns newest first, which the dateModified sort preserves
    # while also fixing families where it does not.
    for flag in ("recommended", "latest"):
        hit = next((b for b in builds if b.get(flag)), None)
        if hit:
            return _strip_mc_suffix(hit["name"], mc_version)
    builds.sort(key=lambda b: str(b.get("dateModified") or ""), reverse=True)
    return _strip_mc_suffix(builds[0]["name"], mc_version)


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


async def cache_jar(
    file_meta: dict, client: httpx.AsyncClient | None = None
) -> Path | None:
    """Ensure a jar is on disk and return its path -- never its bytes.

    The install pipeline used to pass jars around as `bytes`, which meant a
    300-mod pack held every jar in memory at once purely so it could be
    written into an upload archive later. The jars are already being written
    to the cache; handing back the path lets the archive be built by
    streaming, so peak memory stops scaling with pack size.

    Returns None when there is no cache directory to write to, in which case
    the caller falls back to the in-memory path.
    """
    path = _jar_cache_path(file_meta)
    if not path:
        return None
    expected = file_meta.get("size") or 0
    if path.exists():
        try:
            if not expected or abs(path.stat().st_size - expected) < 1024:
                return path
        except OSError:
            pass
    data = await download(file_meta, client)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        return None
    finally:
        del data
    return path


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


def _download_urls(file_meta: dict) -> list[str]:
    """Where a file can be fetched from, best first.

    `downloadUrl` is null for any project whose author opted out of third-party
    distribution, so the deterministic CDN path is not a fallback for network
    trouble -- it is the only address most files have.
    """
    urls = []
    if file_meta.get("download_url"):
        urls.append(file_meta["download_url"])
    if file_meta.get("file_id") and file_meta.get("file_name"):
        fallback = cdn_url(file_meta["file_id"], file_meta["file_name"])
        if fallback not in urls:
            urls.append(fallback)
    if not urls:
        raise CurseForgeError(f"no download URL for {file_meta.get('file_name')}")
    return urls


async def download_to(file_meta: dict, dest: Path,
                      client: httpx.AsyncClient | None = None,
                      on_progress=None) -> Path:
    """Stream a file straight to `dest`, never holding it whole in memory.

    Pack archives are the one download here that is measured in hundreds of
    megabytes -- Tensura Evolutions' server pack is 617 MB -- and the container
    runs under a 1 GB cap, so buffering the body and then writing it out needs
    twice the archive's size in RAM and gets the process killed. This keeps the
    peak at one chunk.

    Writes to a sibling `.part` and renames on success, so an interrupted
    download can never be mistaken for a cached archive by a later run.
    """
    urls = _download_urls(file_meta)
    own = client is None
    c = client or httpx.AsyncClient(timeout=600, follow_redirects=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        last: Exception | None = None
        for url in urls:
            for attempt in range(3):
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    got = 0
                    async with c.stream("GET", url) as r:
                        if r.status_code != 200:
                            last = CurseForgeError(f"{url} -> {r.status_code}")
                            await r.aclose()
                            raise last
                        with open(tmp, "wb") as fh:
                            async for chunk in r.aiter_bytes(1 << 20):
                                fh.write(chunk)
                                got += len(chunk)
                                if on_progress:
                                    on_progress(got)
                    if got:
                        tmp.replace(dest)
                        return dest
                    last = CurseForgeError(f"{url} -> empty body")
                except Exception as e:
                    last = e
                finally:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                await asyncio.sleep(1.5 * (attempt + 1))
        raise CurseForgeError(
            f"failed to download {file_meta.get('file_name')}: {last}"
        )
    finally:
        if own:
            await c.aclose()


async def download(file_meta: dict, client: httpx.AsyncClient | None = None) -> bytes:
    """Fetch a file's bytes, falling back to the CDN path when needed.

    For mod jars, which are megabytes. Anything pack-sized goes through
    download_to() instead.
    """
    urls = _download_urls(file_meta)

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


# --- project detail ----------------------------------------------------
#
# What the detail popup on a mod row reads. Two things here are deliberate
# and worth not undoing:
#
#  * Every URL that reaches the browser as an `href` is filtered to http(s).
#    `sourceUrl`, `issuesUrl` and `wikiUrl` are typed by a third-party
#    project author into a form; `javascript:` in one of them is a script
#    the operator never wrote, running inside the operator's control panel.
#  * The long description is HTML written by that same third party, and it
#    NEVER crosses to the browser as markup. It is flattened here into a
#    list of typed text blocks, so the front end is handed data it can only
#    put through `textContent`. That is stricter than a sanitiser, and it
#    has no allow-list for a later change to get wrong.

_SAFE_SCHEMES = ("http://", "https://")

_BLOCK_CHARS = 1400          # per block, after whitespace collapse
_MAX_BLOCKS = 140            # a README-sized description, not a novel
_MAX_HTML = 400_000          # refuse to parse an absurd body at all


def safe_url(url: Any) -> str | None:
    """An upstream URL, or None unless it is plainly http(s)."""
    if not url:
        return None
    text = str(url).strip()
    return text if text.lower().startswith(_SAFE_SCHEMES) else None


_SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "head",
              "template", "object", "embed"}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "ul", "ol", "dl", "table", "thead", "tbody", "tr", "blockquote", "pre",
    "figure", "figcaption", "li", "dt", "dd", "details", "summary", "center",
    *_HEADINGS,
}


class _Flatten(HTMLParser):
    """Third-party HTML in, typed text blocks out. No markup survives.

    Block kinds are the handful the popup can draw: `h` (with a level),
    `p`, `li`, `quote`, `code` and `rule`. Anything else collapses into a
    paragraph, which is the right failure: an unusual tag loses its styling
    and keeps its words.

    Links are flattened to their text on purpose. A description is the one
    place an author could put an arbitrary destination in front of an
    operator, and nothing in this popup is worth that -- the project's own
    source/issues/wiki links come from named fields instead, filtered.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._buf: list[str] = []
        self._kind = "p"
        self._level = 0
        self._skip = 0

    # -- internals
    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if not text or len(self.blocks) >= _MAX_BLOCKS:
            return
        block: dict[str, Any] = {"t": self._kind, "text": text[:_BLOCK_CHARS]}
        if self._kind == "h":
            block["level"] = self._level or 2
        self.blocks.append(block)

    def _open(self, tag: str) -> None:
        self._flush()
        if tag in _HEADINGS:
            self._kind, self._level = "h", _HEADINGS[tag]
        elif tag in ("li", "dt", "dd"):
            self._kind, self._level = "li", 0
        elif tag == "blockquote":
            self._kind, self._level = "quote", 0
        elif tag == "pre":
            self._kind, self._level = "code", 0
        else:
            self._kind, self._level = "p", 0

    # -- HTMLParser
    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "br":
            self._flush()
        elif tag == "hr":
            self._flush()
            if len(self.blocks) < _MAX_BLOCKS:
                self.blocks.append({"t": "rule"})
        elif tag in ("td", "th"):
            if self._buf:
                self._buf.append(" · ")
        elif tag in _BLOCK_TAGS:
            self._open(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in _BLOCK_TAGS:
            self._flush()
            self._kind, self._level = "p", 0

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def description_blocks(html_text: Any) -> list[dict]:
    """Flatten a CurseForge description into blocks the front end can draw."""
    if not html_text or not isinstance(html_text, str):
        return []
    parser = _Flatten()
    try:
        parser.feed(html_text[:_MAX_HTML])
        parser.close()
    except Exception:
        # A malformed body is worth losing; it is not worth failing on.
        pass
    return parser.blocks[:_MAX_BLOCKS]


def _gallery(m: dict) -> list[dict]:
    out = []
    for shot in (m.get("screenshots") or [])[:8]:
        full = safe_url(shot.get("url")) or safe_url(shot.get("thumbnailUrl"))
        if not full:
            continue
        out.append({
            "url": full,
            "thumb": safe_url(shot.get("thumbnailUrl")) or full,
            "title": shot.get("title") or "",
            "description": shot.get("description") or "",
        })
    return out


_LOADER_NAME = {v: k for k, v in LOADER_TYPE.items() if v}


async def get_description(mod_id: int) -> str:
    """The project page's long description, as raw HTML."""
    data = await _get(f"/v1/mods/{mod_id}/description")
    return data if isinstance(data, str) else ""


async def project_detail(
    mod_id: int,
    *,
    game_version: str | None = None,
    mod_loader: str | None = None,
) -> dict:
    """Everything the detail popup shows for one CurseForge project.

    The project itself is fetched first and is fatal if it fails -- without
    it there is nothing to draw. The description and the build on offer are
    then fetched together and are both optional: a project page with no
    description, or no build published for this (Minecraft, loader) pair, is
    a normal state and not an error to put in front of anyone.
    """
    raw = await _get(f"/v1/mods/{mod_id}")
    if not raw:
        raise CurseForgeError(f"CurseForge has no project {mod_id}")

    async def _body() -> list[dict]:
        try:
            return description_blocks(await get_description(mod_id))
        except Exception:
            return []

    async def _offer() -> dict | None:
        try:
            files = await list_files(
                mod_id, game_version=game_version, mod_loader=mod_loader,
                page_size=20,
            )
        except Exception:
            return None
        return files[0] if files else None

    body, offer = await asyncio.gather(_body(), _offer())

    links = raw.get("links") or {}
    logo = raw.get("logo") or {}
    indexes = raw.get("latestFilesIndexes") or []

    out = _slim_project(raw)
    out.pop("latest_files", None)
    out.update({
        "kind": "mod",
        "url": safe_url(links.get("websiteUrl")),
        "logo_full": safe_url(logo.get("url")) or out.get("logo"),
        "source_url": safe_url(links.get("sourceUrl")),
        "issues_url": safe_url(links.get("issuesUrl")),
        "wiki_url": safe_url(links.get("wikiUrl")),
        "discord_url": None,
        "donation_urls": [],
        # CurseForge's Core API publishes no licence on a project at all --
        # it is on the web page and not in the API. Saying so is better than
        # an empty row, which reads as "we failed to load it".
        "license": None,
        "follows": raw.get("thumbsUpCount"),
        "date_created": raw.get("dateCreated"),
        "date_modified": raw.get("dateModified"),
        "date_released": raw.get("dateReleased"),
        # CurseForge states no client/server split anywhere in its API. The
        # front end already knows to say "side unknown" for that.
        "client_side": None,
        "server_side": None,
        "loaders": sorted({
            _LOADER_NAME[i["modLoader"]] for i in indexes
            if i.get("modLoader") in _LOADER_NAME
        }),
        "game_versions": sorted({
            i["gameVersion"] for i in indexes if i.get("gameVersion")
        }, reverse=True)[:40],
        "gallery": _gallery(raw),
        "body_blocks": body,
        "body_format": "html",
        "offer": offer,
    })
    return out
