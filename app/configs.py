"""Mod config browsing and editing.

Mods scatter their settings across config/, defaultconfigs/, kubejs/,
scripts/ and a few loose files in the server root. This module presents
that as one tree, grouped by mod where the filename makes the owner
obvious, and guards the edit path so a stray click cannot corrupt a world.
"""
from __future__ import annotations

import posixpath
import re

from app import crafty

# Directories worth showing in the config editor.
CONFIG_ROOTS = [
    "config", "defaultconfigs", "kubejs", "scripts", "openloader",
    "global_packs", "ftbquests", "packmenu", "serverconfig",
]

# Loose files in the server root that people legitimately edit.
ROOT_FILES = [
    "server.properties", "ops.json", "whitelist.json", "banned-players.json",
    "banned-ips.json", "eula.txt", "user_jvm_args.txt",
]

EDITABLE_EXT = {
    ".toml", ".json", ".json5", ".cfg", ".conf", ".properties", ".txt", ".yaml",
    ".yml", ".snbt", ".js", ".zs", ".ini", ".xml", ".hjson", ".tsv", ".csv", ".md",
}

# Anything here is a world/data file, not configuration -- never list it.
BLOCKED_DIRS = {"world", "world_nether", "world_the_end", "logs", "crash-reports",
                "libraries", "versions", "backups", ".mixin.out", "cache"}

MAX_EDIT_BYTES = 2 * 1024 * 1024


def is_editable(name: str) -> bool:
    return posixpath.splitext(name)[1].lower() in EDITABLE_EXT


def _size_to_bytes(size: str | None) -> int:
    """Crafty reports human sizes like '4.7MB' -- turn that back into bytes."""
    if not size:
        return 0
    m = re.match(r"([\d.]+)\s*([KMGT]?)B?", str(size).strip(), re.I)
    if not m:
        return 0
    value = float(m.group(1))
    return int(value * {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3,
                        "T": 1024**4}[m.group(2).upper()])


def guess_owner(path: str) -> str:
    """Attribute a config file to a mod, for grouping in the UI."""
    base = posixpath.basename(path)
    stem = posixpath.splitext(base)[0]
    parts = [p for p in posixpath.dirname(path).split("/") if p]
    # config/<mod>/thing.toml -- the folder is the better signal.
    if len(parts) >= 2 and parts[0] in ("config", "defaultconfigs", "serverconfig"):
        return parts[1]
    return re.split(r"[-_.]", stem)[0] or stem


async def list_configs(server_id: str, root: str | None = None) -> dict:
    """List config files, either for one root or across all known roots."""
    roots = [root] if root else CONFIG_ROOTS
    files: list[dict] = []

    for r in roots:
        if r.split("/")[0] in BLOCKED_DIRS:
            continue
        try:
            entries = await crafty.walk(server_id, r, max_entries=4000)
        except Exception:
            continue
        for entry in entries:
            if entry.get("dir"):
                continue
            top = entry["path"].split("/")[0]
            if top in BLOCKED_DIRS:
                continue
            files.append({
                "path": entry["path"],
                "name": entry["name"],
                "size": entry.get("size"),
                "bytes": _size_to_bytes(entry.get("size")),
                "modified": entry.get("modified"),
                "editable": is_editable(entry["name"]),
                "owner": guess_owner(entry["path"]),
                "root": r,
            })

    if not root:
        try:
            entries = await crafty.list_dir(server_id, ".")
            for name, meta in entries.items():
                if name in ROOT_FILES and isinstance(meta, dict) and not meta.get("dir"):
                    files.append({
                        "path": name,
                        "name": name,
                        "size": meta.get("size"),
                        "bytes": _size_to_bytes(meta.get("size")),
                        "modified": meta.get("modified"),
                        "editable": True,
                        "owner": "server",
                        "root": ".",
                    })
        except Exception:
            pass

    groups: dict[str, int] = {}
    for f in files:
        groups[f["owner"]] = groups.get(f["owner"], 0) + 1

    files.sort(key=lambda f: (f["owner"].lower(), f["path"].lower()))
    return {
        "count": len(files),
        "files": files,
        "groups": sorted(
            ({"owner": k, "count": v} for k, v in groups.items()),
            key=lambda g: (-g["count"], g["owner"]),
        ),
        "roots": roots,
    }


async def read_config(server_id: str, path: str) -> dict:
    _guard(path)
    content = await crafty.read_file(server_id, path)
    return {
        "path": path,
        "content": content,
        "lines": content.count("\n") + 1,
        "editable": is_editable(path),
        "language": _language(path),
    }


async def write_config(server_id: str, path: str, content: str) -> dict:
    _guard(path)
    if not is_editable(path):
        raise ValueError(f"{path} is not an editable config file type")
    if len(content.encode()) > MAX_EDIT_BYTES:
        raise ValueError("file is too large to save through the editor")
    await crafty.write_file(server_id, path, content)
    return {"path": path, "saved": True, "bytes": len(content.encode())}


def _guard(path: str) -> None:
    clean = path.replace("\\", "/").strip()
    if clean.startswith("/") or re.match(r"^[A-Za-z]:", clean):
        raise ValueError("invalid path")
    # Segment-wise, and every all-dots segment is rejected -- not just "..".
    # "...." is not a traversal on any filesystem we target, but it sails past
    # a ".." check and reaches Crafty, which answers a 500 with a traceback in
    # it. A config path never has a dots-only segment, so refuse them here and
    # keep the failure a 400 with a sentence in it.
    parts = clean.split("/")
    if any(p == "" or set(p) == {"."} for p in parts):
        raise ValueError("invalid path")
    top = parts[0]
    if top in BLOCKED_DIRS:
        raise ValueError(f"{top}/ is not editable from the config editor")


def _language(path: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    return {
        ".json": "json", ".json5": "json", ".hjson": "json",
        ".toml": "toml", ".cfg": "ini", ".ini": "ini", ".conf": "ini",
        ".properties": "properties", ".yaml": "yaml", ".yml": "yaml",
        ".js": "javascript", ".zs": "javascript", ".snbt": "json",
        ".xml": "xml", ".md": "markdown",
    }.get(ext, "text")
