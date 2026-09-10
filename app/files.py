"""A file manager for a server instance.

Crafty already exposes every operation this needs -- list, read, write,
create, rename, delete, upload, download, unzip -- but it exposes them as
raw calls with sharp edges: sizes arrive as human strings, a directory
listing is a dict keyed by name with a stray `root_path` in it, and every
path is trusted. This module is the layer that makes those safe and
uniform enough to hang a UI off.

Three rules the whole module is built around:

  * **Paths are checked here, not downstream.** Crafty does reject a
    traversal, but it does it with a 500 and a traceback -- "the component
    downstream happens to refuse" is not a guard. Every path that reaches
    Crafty from this module has been through `guard_path`.
  * **The world is not a toy.** `world/`, `backups/` and friends are
    listable (people legitimately want to see how big a world is) but
    writing into them, and deleting them, needs an explicit override so a
    mis-click cannot cost someone their save.
  * **Text is only text when it is.** Reading a jar through the text
    endpoint returns mojibake or an error; the browser is told up front
    which entries it can open in an editor and which it can only download.
"""
from __future__ import annotations

import posixpath
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app import crafty

# Extensions the built-in editor will open. Everything else is
# download-only, which is the honest answer for a jar or a region file.
TEXT_EXT = {
    ".txt", ".md", ".json", ".json5", ".jsonc", ".toml", ".yaml", ".yml",
    ".properties", ".cfg", ".conf", ".ini", ".snbt", ".js", ".mjs", ".ts",
    ".zs", ".zs.txt", ".lua", ".py", ".sh", ".bat", ".cmd", ".xml", ".html",
    ".css", ".csv", ".tsv", ".log", ".hjson", ".env", ".gitignore", ".mcmeta",
    ".nbt.json", ".sk", ".yml.disabled", ".lang",
}

ARCHIVE_EXT = {".zip", ".jar", ".gz", ".tgz", ".tar", ".rar", ".7z"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}

# Anything under one of these is world or runtime data. Listing is fine --
# knowing a world is 4 GB is exactly what a file manager is for -- but a
# write or a delete inside one needs `allow_world=True`, which the UI only
# sends after a second confirmation.
PROTECTED_ROOTS = {
    "world", "world_nether", "world_the_end", "backups", "libraries",
    "versions", "cache", ".mixin.out",
}

# Files whose loss breaks the instance in a way that is not obvious later.
CRITICAL_FILES = {
    "server.properties", "eula.txt", "server.jar", "user_jvm_args.txt",
    ".blessforge.json", ".modpack-studio.json", "run.sh", "run.bat",
}

MAX_EDIT_BYTES = 4 * 1024 * 1024
MAX_UPLOAD_BYTES = 512 * 1024 * 1024


class FileError(ValueError):
    """A refusal that is the caller's fault -- surfaced as a 400."""


def guard_path(path: str | None, *, allow_root: bool = True) -> str:
    """Normalise a server-relative path, or refuse it.

    Returns "." for the server root. Anything absolute, anything with a
    `..` segment, anything with a NUL or a drive letter is refused before
    it can reach Crafty.
    """
    raw = (path or ".").replace("\\", "/").strip()
    if not raw or raw in (".", "./"):
        if not allow_root:
            raise FileError("a file name is required")
        return "."
    if "\x00" in raw:
        raise FileError("that path contains a null byte")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise FileError("only paths inside the server directory are allowed")
    parts = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise FileError("that path points outside the server directory")
        parts.append(part)
    if not parts:
        if not allow_root:
            raise FileError("a file name is required")
        return "."
    return "/".join(parts)


def guard_name(name: str) -> str:
    """A bare file or directory name -- no separators, no traversal."""
    n = (name or "").replace("\\", "/").strip()
    if not n or "/" in n or "\x00" in n or set(n) <= {"."}:
        raise FileError(f"{name!r} is not a valid name")
    if len(n) > 255:
        raise FileError("that name is too long")
    return n


def top_segment(path: str) -> str:
    p = guard_path(path)
    return "." if p == "." else p.split("/", 1)[0]


def is_protected(path: str) -> bool:
    return top_segment(path) in PROTECTED_ROOTS


def _ext(name: str) -> str:
    return posixpath.splitext(name)[1].lower()


def classify(name: str, is_dir: bool) -> str:
    """One word for what an entry is, so the UI can pick an icon."""
    if is_dir:
        return "folder"
    ext = _ext(name)
    lowered = name.lower()
    if lowered.endswith(".jar") or lowered.endswith(".jar.disabled"):
        return "mod"
    if ext in ARCHIVE_EXT:
        return "archive"
    if ext in IMAGE_EXT:
        return "image"
    if ext in TEXT_EXT or lowered in {"eula.txt", "banned-ips.json"}:
        return "text"
    if lowered.endswith(".mca") or lowered.endswith(".mcr") or ext == ".dat":
        return "world"
    return "binary"


def is_editable(name: str, size_bytes: int = 0) -> bool:
    if classify(name, False) != "text":
        return False
    return not size_bytes or size_bytes <= MAX_EDIT_BYTES


def size_to_bytes(size: Any) -> int:
    """Crafty reports human sizes like '4.7MB'. Turn that back into bytes."""
    if size is None:
        return 0
    if isinstance(size, (int, float)):
        return int(size)
    m = re.match(r"([\d.]+)\s*([KMGT]?)i?B?", str(size).strip(), re.I)
    if not m:
        return 0
    try:
        value = float(m.group(1))
    except ValueError:
        return 0
    return int(value * {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
                        "T": 1024 ** 4}[m.group(2).upper()])


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}".replace(".0 ", " ")
    return f"{n:.1f} TB"


def _entry(name: str, meta: dict, parent: str) -> dict:
    rel = (meta.get("path") or (
        name if parent == "." else f"{parent}/{name}"
    )).replace("\\", "/").lstrip("/")
    is_dir = bool(meta.get("dir"))
    raw_size = meta.get("size")
    size_b = size_to_bytes(raw_size)
    kind = classify(name, is_dir)
    return {
        "name": name,
        "path": rel,
        "dir": is_dir,
        "kind": kind,
        "size": raw_size if isinstance(raw_size, str) else (
            human_size(size_b) if size_b else None),
        "bytes": size_b,
        "modified": meta.get("modified"),
        "editable": (not is_dir) and is_editable(name, size_b),
        "protected": top_segment(rel) in PROTECTED_ROOTS,
        "critical": name in CRITICAL_FILES and "/" not in rel,
        "disabled": name.endswith(".disabled"),
    }


async def browse(server_id: str, path: str = ".") -> dict:
    """One directory, sorted folders-first, with breadcrumbs."""
    path = guard_path(path)
    try:
        raw = await crafty.list_dir(server_id, path)
    except crafty.CraftyError as e:
        raise FileError(f"could not open {path}: {e}")

    entries = [
        _entry(name, meta, path)
        for name, meta in (raw or {}).items()
        if name != "root_path" and isinstance(meta, dict)
    ]
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))

    crumbs = [{"name": "server root", "path": "."}]
    if path != ".":
        walked = []
        for part in path.split("/"):
            walked.append(part)
            crumbs.append({"name": part, "path": "/".join(walked)})

    total = sum(e["bytes"] for e in entries if not e["dir"])
    return {
        "path": path,
        "parent": (posixpath.dirname(path) or ".") if path != "." else None,
        "crumbs": crumbs,
        "entries": entries,
        "count": len(entries),
        "folders": sum(1 for e in entries if e["dir"]),
        "files": sum(1 for e in entries if not e["dir"]),
        "bytes": total,
        "size": human_size(total) if total else "0 B",
        "protected": is_protected(path) if path != "." else False,
    }


async def read_text(server_id: str, path: str) -> dict:
    path = guard_path(path, allow_root=False)
    name = posixpath.basename(path)
    if classify(name, False) not in ("text", "binary"):
        raise FileError(f"{name} is not a text file — download it instead")
    try:
        content = await crafty.read_file(server_id, path)
    except crafty.CraftyError as e:
        raise FileError(f"could not read {path}: {e}")
    if len(content.encode("utf-8", "replace")) > MAX_EDIT_BYTES:
        raise FileError("that file is too large to open in the editor")
    return {
        "path": path,
        "name": name,
        "content": content,
        "lines": content.count("\n") + 1,
        "bytes": len(content.encode("utf-8", "replace")),
        "language": language_of(name),
        "protected": is_protected(path),
        "critical": name in CRITICAL_FILES,
    }


def language_of(name: str) -> str:
    ext = _ext(name)
    return {
        ".json": "json", ".json5": "json", ".jsonc": "json", ".mcmeta": "json",
        ".toml": "toml", ".yaml": "yaml", ".yml": "yaml",
        ".properties": "properties", ".cfg": "properties", ".conf": "properties",
        ".ini": "properties", ".js": "javascript", ".mjs": "javascript",
        ".ts": "javascript", ".zs": "javascript", ".lua": "lua",
        ".py": "python", ".sh": "shell", ".bat": "shell", ".cmd": "shell",
        ".xml": "xml", ".html": "xml", ".css": "css", ".snbt": "snbt",
        ".log": "log", ".md": "markdown",
    }.get(ext, "text")


async def write_text(server_id: str, path: str, content: str,
                     *, allow_world: bool = False) -> dict:
    path = guard_path(path, allow_root=False)
    if is_protected(path) and not allow_world:
        raise FileError(
            f"{top_segment(path)}/ holds world or runtime data. Confirm the "
            "override if you really mean to edit inside it."
        )
    if len(content.encode("utf-8")) > MAX_EDIT_BYTES:
        raise FileError("that file is too large to save through the editor")
    try:
        await crafty.write_file(server_id, path, content)
    except crafty.CraftyError as e:
        # Crafty answers a write to a file that does not exist with an error
        # rather than creating it, so create-then-write is the honest retry.
        parent = posixpath.dirname(path) or "."
        name = posixpath.basename(path)
        try:
            await crafty.create_entry(server_id, parent, name, directory=False)
            await crafty.write_file(server_id, path, content)
        except crafty.CraftyError:
            raise FileError(f"could not save {path}: {e}")
    return {"path": path, "saved": True, "bytes": len(content.encode("utf-8"))}


async def create(server_id: str, parent: str, name: str, directory: bool
                 ) -> dict:
    parent = guard_path(parent)
    name = guard_name(name)
    try:
        await crafty.create_entry(server_id, parent, name, directory=directory)
    except crafty.CraftyError as e:
        raise FileError(f"could not create {name}: {e}")
    rel = name if parent == "." else f"{parent}/{name}"
    return {"path": rel, "dir": directory, "created": True}


async def rename(server_id: str, path: str, new_name: str) -> dict:
    path = guard_path(path, allow_root=False)
    new_name = guard_name(new_name)
    if is_protected(path) and top_segment(path) == path:
        raise FileError(f"{path} is a world directory and is not renameable here")
    try:
        await crafty.rename_path(server_id, path, new_name)
    except crafty.CraftyError as e:
        raise FileError(f"could not rename {path}: {e}")
    parent = posixpath.dirname(path) or "."
    return {"path": new_name if parent == "." else f"{parent}/{new_name}",
            "renamed": True}


async def delete(server_id: str, paths: list[str], *, allow_world: bool = False
                 ) -> dict:
    if not isinstance(paths, list) or not paths:
        raise FileError("nothing was selected to delete")
    cleaned = [guard_path(p, allow_root=False) for p in paths]
    blocked = [p for p in cleaned if is_protected(p) and not allow_world]
    if blocked:
        raise FileError(
            "these are world or runtime data and were not deleted: "
            + ", ".join(blocked[:4])
            + ". Confirm the override to delete them anyway."
        )
    critical = [p for p in cleaned
                if posixpath.basename(p) in CRITICAL_FILES and "/" not in p]
    try:
        await crafty.delete_paths(server_id, cleaned)
    except crafty.CraftyError as e:
        raise FileError(f"delete failed: {e}")
    return {"deleted": cleaned, "count": len(cleaned),
            "warning": (
                "Deleted files the server needs to start: "
                + ", ".join(critical)
                + ". Recreate them before starting this instance."
            ) if critical else None}


async def upload(server_id: str, folder: str, filename: str, data: bytes
                 ) -> dict:
    folder = guard_path(folder)
    filename = guard_name(filename)
    if len(data) > MAX_UPLOAD_BYTES:
        raise FileError(
            f"that file is larger than the {MAX_UPLOAD_BYTES // 1024**2} MB "
            "browser-upload limit"
        )
    if is_protected(folder):
        raise FileError(
            f"{top_segment(folder)}/ holds world data — uploads into it are "
            "not allowed from here"
        )
    try:
        await crafty.upload_file(server_id, folder, filename, data)
    except crafty.CraftyError as e:
        raise FileError(f"upload failed: {e}")
    rel = filename if folder == "." else f"{folder}/{filename}"
    return {"path": rel, "bytes": len(data), "uploaded": True,
            "archive": _ext(filename) in ARCHIVE_EXT}


def download(server_id: str, path: str) -> tuple[Any, str]:
    """`(byte iterator, filename)` -- streamed, never buffered.

    A world folder or a server jar is bigger than this container's whole
    memory limit, so the bytes go straight from Crafty to the browser.
    """
    path = guard_path(path, allow_root=False)
    return crafty.stream_file(server_id, path), posixpath.basename(path)


async def upload_stream(server_id: str, folder: str, filename: str,
                        chunks) -> dict:
    """Take an upload to disk first, then hand Crafty the file.

    Reading a browser upload into memory costs its full size at once, and the
    limit here is half a gigabyte. Spooling to a temp file and using Crafty's
    chunked protocol keeps the cost to one chunk regardless.
    """
    folder = guard_path(folder)
    filename = guard_name(filename)
    if is_protected(folder):
        raise FileError(
            f"{top_segment(folder)}/ holds world data — uploads into it are "
            "not allowed from here"
        )

    tmp = Path(tempfile.mkdtemp(prefix="bf-upload-"))
    dest = tmp / filename
    size = 0
    try:
        with dest.open("wb") as fh:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise FileError(
                        f"that file is larger than the "
                        f"{MAX_UPLOAD_BYTES // 1024**2} MB browser-upload "
                        "limit"
                    )
                fh.write(chunk)
        if not size:
            raise FileError("that file is empty")
        try:
            await crafty.upload_path(server_id, folder, filename, dest)
        except crafty.CraftyError as e:
            raise FileError(f"upload failed: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    rel = filename if folder == "." else f"{folder}/{filename}"
    return {"path": rel, "bytes": size, "uploaded": True,
            "archive": _ext(filename) in ARCHIVE_EXT}


async def extract(server_id: str, path: str) -> dict:
    """Ask Crafty to unzip an archive already sitting in the instance."""
    path = guard_path(path, allow_root=False)
    if _ext(path) != ".zip":
        raise FileError("only .zip archives can be extracted here")
    try:
        await crafty.unzip(server_id, path)
    except crafty.CraftyError as e:
        raise FileError(f"could not extract {path}: {e}")
    return {
        "path": path,
        "started": True,
        # Crafty extracts on a daemon thread and answers 200 the moment it
        # starts, so promising "done" here would be a lie.
        "note": "Extraction runs in the background — refresh in a moment to "
                "see the files.",
    }


async def search(server_id: str, query: str, root: str = ".",
                 limit: int = 300) -> dict:
    """Find files by name under a subtree. Bounded, because Crafty walks."""
    q = (query or "").strip().lower()
    if len(q) < 2:
        raise FileError("type at least two characters to search")
    root = guard_path(root)
    try:
        entries = await crafty.walk(server_id, root, max_entries=8000)
    except Exception as e:
        raise FileError(f"could not search {root}: {e}")
    hits = []
    for entry in entries:
        if q not in entry["name"].lower():
            continue
        hits.append({
            "name": entry["name"],
            "path": entry["path"],
            "dir": bool(entry.get("dir")),
            "kind": classify(entry["name"], bool(entry.get("dir"))),
            "size": entry.get("size"),
            "bytes": size_to_bytes(entry.get("size")),
            "modified": entry.get("modified"),
            "editable": (not entry.get("dir"))
            and is_editable(entry["name"], size_to_bytes(entry.get("size"))),
            "protected": top_segment(entry["path"]) in PROTECTED_ROOTS,
        })
        if len(hits) >= limit:
            break
    hits.sort(key=lambda h: (not h["dir"], h["path"].lower()))
    return {"query": query, "root": root, "count": len(hits),
            "truncated": len(hits) >= limit, "results": hits}


async def usage(server_id: str) -> dict:
    """Rough per-top-level-folder disk usage, for the manager's sidebar.

    One `walk` per top-level directory would be a request storm on a pack
    with 30 config folders, so only the directories worth knowing about are
    measured and the rest are reported as unmeasured rather than as zero.
    """
    measured = ("mods", "plugins", "config", "world", "logs", "backups",
                "libraries", "crash-reports", "datapacks")
    out: list[dict] = []
    try:
        root = await crafty.list_dir(server_id, ".")
    except crafty.CraftyError as e:
        raise FileError(str(e))
    present = {n for n, m in (root or {}).items()
               if n != "root_path" and isinstance(m, dict) and m.get("dir")}
    for name in measured:
        if name not in present:
            continue
        try:
            entries = await crafty.walk(server_id, name, max_entries=4000)
        except Exception:
            out.append({"name": name, "bytes": 0, "files": 0,
                        "size": "unmeasured"})
            continue
        total = sum(size_to_bytes(e.get("size")) for e in entries
                    if not e.get("dir"))
        out.append({
            "name": name,
            "files": sum(1 for e in entries if not e.get("dir")),
            "bytes": total,
            "size": human_size(total),
            "protected": name in PROTECTED_ROOTS,
        })
    out.sort(key=lambda d: -d["bytes"])
    return {"folders": out, "total_bytes": sum(d["bytes"] for d in out)}
