"""Mods the node downloads itself, straight from the mod sites' CDNs.

A modpack install used to move every jar three times: the panel downloaded
it, zipped it into a 64 MB batch, uploaded the batch to the node, and the
node unpacked it. Now the panel sends the list -- each file's address, where
it goes, its SHA-1 and its size -- as one `fetch_many` install step, and the
node fetches the files itself, several at once:

  * **Only from the CDNs.** Every address, and every redirect on the way,
    has to be on `download_hosts` (agent.toml); anything else is refused
    before a request is made. A panel that has been taken over can still
    name files, but cannot make the node fetch from anywhere it likes.
  * **Nothing unverified lands.** A file is streamed to a temp file, its
    size and SHA-1 checked against what the panel said, and only then
    renamed into place. A failure is retried with backoff; a file that keeps
    failing is reported by path, and the panel sends that one itself.
  * **Downloaded once per node.** Files are kept in a cache under the
    agent's state folder, by SHA-1, so ten servers on the same pack, or a
    reinstall, cost one download. Two fetches of the same file at the same
    moment share one download. Each server gets its own COPY: a hard link
    would share the inode with the cache, and the file jail refuses a file
    with more than one link (files.open_file) because its other name is
    outside the folder. The copy goes through the jail like any other
    write and is hashed again on the way, so a damaged cache entry is caught
    and fetched afresh rather than installed.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import random
import re
import shutil
import time
from pathlib import Path

import httpx

from agent import clients, files
from agent.containers import RuntimeError_

ATTEMPTS = 4
BACKOFF = (1.0, 4.0, 15.0)      # seconds before the second, third and fourth try
MAX_REDIRECTS = 5
MAX_FILES = 10000
MAX_FILE = 1024 ** 3
CHUNK = 1024 * 1024
# Between bytes, not for the whole file: a big jar on a slow line is fine, a
# connection that has gone quiet is not.
TIMEOUT = httpx.Timeout(120, connect=20)
REPORT_EVERY = 3.0              # seconds between progress lines
_SHA1 = re.compile(r"^[0-9a-f]{40}$")


class Refused(Exception):
    """Not something a retry fixes: an address this node does not fetch
    from, or one the CDN says plainly is not there."""


class Mismatch(Exception):
    """What arrived is not the file the panel described."""


def client() -> httpx.AsyncClient:
    """The node's one client for downloads (agent/clients.py)."""
    return clients.client("downloads", lambda: httpx.AsyncClient(
        timeout=600, follow_redirects=True,
        headers={"User-Agent": "BlessForge-agent"}))


def host_allowed(url: httpx.URL, hosts: set[str]) -> bool:
    return (url.scheme in ("https", "http")
            and (url.host or "").lower().rstrip(".") in hosts)


def check_entries(entries) -> list[dict]:
    """The fetch_many list, checked whole before anything is fetched."""
    if not isinstance(entries, list) or not entries:
        raise RuntimeError_("fetch_many must be a list of files", 400)
    if len(entries) > MAX_FILES:
        raise RuntimeError_(f"fetch_many takes at most {MAX_FILES} files", 413)
    out, seen = [], set()
    for e in entries:
        if not isinstance(e, dict):
            raise RuntimeError_("each fetch_many entry must be an object", 400)
        parts = files.parts_of(str(e.get("path") or ""))
        if not parts:
            raise RuntimeError_("a fetch_many entry has no path", 400)
        files.check_name(parts[-1])
        rel = "/".join(parts)
        url = str(e.get("url") or "")
        # A file nobody may download (its author turned that off), brought
        # by hand once on this node: from the cache if it is there, never
        # from anywhere else.
        cache_only = bool(e.get("cache_only"))
        sha1 = str(e.get("sha1") or "").lower()
        try:
            size = int(e.get("size"))
        except (TypeError, ValueError):
            raise RuntimeError_(f"{rel}: its size is not a number", 400)
        if cache_only:
            url = ""
        elif not url.startswith(("https://", "http://")):
            raise RuntimeError_(f"{rel}: only http(s) downloads are allowed", 400)
        if not _SHA1.match(sha1):
            raise RuntimeError_(f"{rel}: a SHA-1 is required to fetch it", 400)
        if not 0 < size <= MAX_FILE:
            raise RuntimeError_(f"{rel}: its size must be between 1 byte and 1 GB", 400)
        if rel in seen:
            raise RuntimeError_(f"{rel} is in the list twice", 400)
        seen.add(rel)
        out.append({"url": url, "path": rel, "sha1": sha1, "size": size,
                    "cache_only": cache_only})
    return out


class Cache:
    """The node's downloads by SHA-1, shared by every server on it.

    Least recently used first out, once it is over its limit; a file used
    again has its time refreshed. A limit of 0 keeps nothing between
    installs (fetches running at the same moment still share)."""

    def __init__(self, root: Path, limit_gb: float):
        self.root = root
        self.tmp = root / "tmp"
        self.limit = int(float(limit_gb) * 1024 ** 3)
        # Who wants each file right now: one fetch downloads it while the
        # others wait on its lock, then copy what it fetched.
        self._locks: dict[str, asyncio.Lock] = {}
        self.users: dict[str, int] = {}
        shutil.rmtree(self.tmp, ignore_errors=True)     # parts left by a crash

    def path(self, sha1: str) -> Path:
        return self.root / sha1[:2] / sha1

    @contextlib.asynccontextmanager
    async def claim(self, sha1: str):
        lock = self._locks.setdefault(sha1, asyncio.Lock())
        self.users[sha1] = self.users.get(sha1, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self.users[sha1] -= 1
            if not self.users[sha1]:
                del self.users[sha1]
                self._locks.pop(sha1, None)

    def has(self, sha1: str, size: int) -> bool:
        p = self.path(sha1)
        try:
            if p.stat().st_size == size:
                os.utime(p)
                return True
        except FileNotFoundError:
            return False
        self.drop(sha1)
        return False

    def drop(self, sha1: str) -> None:
        try:
            self.path(sha1).unlink()
        except FileNotFoundError:
            pass

    def prune(self) -> int:
        """Delete the least recently used files until the cache fits its
        limit. A file some fetch is using right now is left. Returns the
        bytes freed."""
        entries = []
        for sub in self.root.glob("??"):
            for p in sub.iterdir():
                try:
                    st = p.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, p))
        size = sum(e[1] for e in entries)
        freed = 0
        for _mtime, nbytes, p in sorted(entries):
            if size - freed <= self.limit:
                break
            if p.name in self.users:
                continue
            try:
                p.unlink()
                freed += nbytes
            except OSError:
                pass
        return freed


async def fetch_many(cache: Cache, folder: Path, items: list[dict], hosts,
                     concurrency: int, out, record: dict) -> dict:
    """Every file into the server folder, `concurrency` at a time.

    Progress goes to `out` every few seconds and into record["progress"]
    for the panel's poll. Returns the counts, and the files that could not
    be fetched with why; the others are in place either way."""
    allowed = {str(h).lower().rstrip(".") for h in hosts}
    total_bytes = sum(i["size"] for i in items)
    progress = record["progress"] = {"done": 0, "total": len(items), "bytes": 0,
                                     "total_bytes": total_bytes, "downloaded": 0,
                                     "cached": 0, "failed": 0}
    failed: list[dict] = []
    gate = asyncio.Semaphore(max(1, int(concurrency)))
    said = time.monotonic()
    out(f"fetching {len(items)} files ({total_bytes / 1048576:.0f} MB), "
        f"{max(1, int(concurrency))} at a time")
    await asyncio.to_thread(_make_folders, folder, items)

    async def one(item: dict) -> None:
        nonlocal said
        async with gate:
            try:
                fresh = await _fetch_one(cache, folder, item, allowed)
                progress["downloaded" if fresh else "cached"] += 1
                progress["bytes"] += item["size"]
            except Exception as e:      # noqa: BLE001 -- reported per file
                why = str(e) or type(e).__name__
                failed.append({"path": item["path"], "url": item["url"], "error": why})
                progress["failed"] += 1
                out(f"could not fetch {item['path']}: {why}")
            progress["done"] += 1
            if time.monotonic() - said >= REPORT_EVERY and progress["done"] < len(items):
                said = time.monotonic()
                out(f"fetched {progress['done']}/{len(items)} "
                    f"({progress['bytes'] / 1048576:.0f}/{total_bytes / 1048576:.0f} MB, "
                    f"{progress['cached']} already on this node)")

    await asyncio.gather(*(one(i) for i in items))
    placed = progress["downloaded"] + progress["cached"]
    out(f"fetched {placed} of {len(items)} files: {progress['downloaded']} downloaded, "
        f"{progress['cached']} from this node's cache"
        + (f", {len(failed)} failed" if failed else ""))
    try:
        freed = await asyncio.to_thread(cache.prune)
        if freed:
            out(f"node download cache trimmed by {freed / 1048576:.0f} MB")
    except OSError as e:
        out(f"could not trim the node download cache: {e}")
    return {"fetched": placed, "downloaded": progress["downloaded"],
            "cached": progress["cached"], "failed": failed}


def _make_folders(folder: Path, items: list[dict]) -> None:
    """Every file's folder, made through the jail before the copies start.
    The jail makes a missing folder as it walks to a file, and two writes
    making the same new folder at the same moment fail one of them (EEXIST)."""
    made: set[str] = set()
    for item in items:
        parts = item["path"].split("/")[:-1]
        if not parts or "/".join(parts) in made:
            continue
        made.add("/".join(parts))
        try:
            files.mkdir(folder, "/".join(parts[:-1]) or ".", parts[-1])
        except files.JailError:
            pass        # a link or a file in the way: each copy into it is refused


async def _fetch_one(cache: Cache, folder: Path, item: dict, allowed: set[str]) -> bool:
    """One file into place. True when it had to be downloaded, False when
    the node already had it."""
    sha1, size = item["sha1"], item["size"]
    for _ in range(2):
        fresh = False
        async with cache.claim(sha1):
            if not await asyncio.to_thread(cache.has, sha1, size):
                if item.get("cache_only"):
                    raise Refused("it is not in this node's cache, and it is not "
                                  "fetched from anywhere else")
                await _download(cache, item, allowed)
                fresh = True
        try:
            await asyncio.to_thread(_copy_in, cache.path(sha1), folder, item)
            return fresh
        except Mismatch:
            # The cached copy is damaged, or was pruned between the two
            # steps: dropped, and fetched once more.
            await asyncio.to_thread(cache.drop, sha1)
    raise Mismatch("the copy in this node's cache keeps failing its check")


async def _download(cache: Cache, item: dict, allowed: set[str]) -> None:
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        if attempt:
            delay = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
            await asyncio.sleep(delay * random.uniform(1.0, 1.25))
        try:
            await _get(cache, item, allowed)
            return
        except Refused:
            raise
        except (httpx.HTTPError, Mismatch) as e:
            last = e
    raise RuntimeError_(f"{_describe(last)} (after {ATTEMPTS} tries)", 502)


def _describe(e: Exception | None) -> str:
    if isinstance(e, httpx.HTTPError):
        return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    return str(e)


async def _get(cache: Cache, item: dict, allowed: set[str]) -> None:
    """One attempt: into a part file in the cache, checked, renamed in."""
    url = httpx.URL(item["url"])
    await asyncio.to_thread(cache.tmp.mkdir, parents=True, exist_ok=True)
    part = cache.tmp / f"{item['sha1']}.{os.getpid()}.{time.monotonic_ns()}.part"
    try:
        # Redirects are followed by hand: each hop has to be on the list too,
        # or an allowed host could hand the node on to any other.
        for _hop in range(MAX_REDIRECTS + 1):
            if not host_allowed(url, allowed):
                raise Refused(f"{url.host or url} is not a download host this node "
                              "allows (download_hosts in agent.toml)")
            async with client().stream("GET", url, follow_redirects=False,
                                       timeout=TIMEOUT) as r:
                if r.is_redirect:
                    url = url.join(r.headers["location"])
                    continue
                if r.status_code in (404, 410) or (400 <= r.status_code < 500
                                                   and r.status_code not in (408, 429)):
                    raise Refused(f"{url.host} answered {r.status_code}")
                if r.status_code != 200:
                    raise httpx.HTTPStatusError(f"{url.host} answered {r.status_code}",
                                                request=r.request, response=r)
                await _receive(r, part, item)
            target = cache.path(item["sha1"])
            await asyncio.to_thread(_place, part, target)
            return
        raise Refused(f"more than {MAX_REDIRECTS} redirects")
    finally:
        try:
            part.unlink()
        except FileNotFoundError:
            pass


async def _receive(r: httpx.Response, part: Path, item: dict) -> None:
    """The body into `part`, written and hashed on a thread so a slow disk
    never holds up the event loop, and abandoned the moment it is longer
    than it should be."""
    h = hashlib.sha1()
    got = 0
    fh = await asyncio.to_thread(open, part, "xb")
    try:
        async for chunk in r.aiter_bytes(CHUNK):
            got += len(chunk)
            if got > item["size"]:
                raise Mismatch(f"it is larger than the {item['size']} bytes it should be")
            await asyncio.to_thread(_sink, fh, h, chunk)
    finally:
        await asyncio.to_thread(fh.close)
    if got != item["size"]:
        raise Mismatch(f"{got} of its {item['size']} bytes arrived")
    if h.hexdigest() != item["sha1"]:
        raise Mismatch("its SHA-1 does not match")


def _sink(fh, h, chunk: bytes) -> None:
    h.update(chunk)
    fh.write(chunk)


async def keep(cache: Cache, folder: Path, rel: str, sha1: str) -> int:
    """A file already in a server folder -- a jar brought by hand -- into
    the node's cache, so the next server or reinstall that wants it gets it
    from here. Read through the jail and checked against `sha1`; returns its
    size."""
    sha1 = sha1.lower()
    if not _SHA1.match(sha1):
        raise RuntimeError_("a SHA-1 is required", 400)
    async with cache.claim(sha1):
        return await asyncio.to_thread(_keep, cache, folder, rel, sha1)


def _keep(cache: Cache, folder: Path, rel: str, sha1: str) -> int:
    cache.tmp.mkdir(parents=True, exist_ok=True)
    part = cache.tmp / f"{sha1}.{os.getpid()}.{time.monotonic_ns()}.keep"
    h, got = hashlib.sha1(), 0
    try:
        with files.open_file(folder, rel, "rb") as src, open(part, "xb") as dst:
            while chunk := src.read(CHUNK):
                h.update(chunk)
                dst.write(chunk)
                got += len(chunk)
        if h.hexdigest() != sha1:
            raise RuntimeError_(f"{rel} does not match that SHA-1", 409)
        _place(part, cache.path(sha1))
        return got
    finally:
        try:
            part.unlink()
        except FileNotFoundError:
            pass


def _place(part: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(part, target)


def _copy_in(src: Path, folder: Path, item: dict) -> None:
    """The cached file into the server folder, through the jail, hashed on
    the way. A copy, never a link (see the module docstring)."""
    try:
        fh = open(src, "rb")
    except FileNotFoundError:
        raise Mismatch("the copy in this node's cache is gone")
    with fh, files.open_file(folder, item["path"], "wb") as dst:
        h = hashlib.sha1()
        got = 0
        while chunk := fh.read(CHUNK):
            h.update(chunk)
            dst.write(chunk)
            got += len(chunk)
        # Raised inside the jail's write: its temp file goes, nothing lands.
        if got != item["size"] or h.hexdigest() != item["sha1"]:
            raise Mismatch("the copy in this node's cache is damaged")
