"""Mods the operator has decided are safe on a server, whatever the check says.

The client-only review is deliberately cautious: a mod whose author marks it
`server_side: unsupported` is disabled, because the common case is that the
author is right and the server would crash. But "unsupported" and "will not
work" are not the same claim -- plenty of mods declare no server side because
they simply add nothing there, and run harmlessly when present.

Without a way to say so, the only options are to fight the review on every
install or to stop using it. So a decision made once is remembered, and it is
remembered *globally* rather than per instance: the reason a mod is fine is a
property of the mod, not of the server it happens to be on.

Stored as one JSON file under DATA_DIR. Small, hand-editable, and easy to
inspect when someone wonders why a mod stopped being flagged.
"""
from __future__ import annotations

import json
import time
from typing import Any

from app import config

_PATH = config.DATA_DIR / "client-only-whitelist.json"


def _key(name: str) -> str:
    """Match on the jar's stem, so a version bump does not undo the decision."""
    n = (name or "").strip().lower()
    for suffix in (".jar.disabled", ".jar"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    # Strip a trailing version so `jei-1.21.1-19.21.0` and `jei-1.21.1-19.22.0`
    # are the same decision.
    parts = n.split("-")
    while len(parts) > 1 and any(c.isdigit() for c in parts[-1]):
        parts.pop()
    return "-".join(parts) or n


_cache: dict[str, Any] | None = None


def load() -> dict[str, Any]:
    """The list, read once and then held.

    Held in memory as well as on disk so a decision still applies for this run
    when the data directory turns out not to be writable -- which is a real
    state on this app (see the /data trap) and must not silently discard what
    the operator just chose.
    """
    global _cache
    if _cache is not None:
        return _cache
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        data = {"items": {}}
    _cache = data
    return _cache


def _save(data: dict) -> bool:
    """True if it reached disk. The caller decides whether to say so."""
    global _cache
    _cache = data
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        return True
    except OSError:
        return False


def items() -> list[dict]:
    """Everything on the list, newest first."""
    out = [{"key": k, **v} for k, v in load()["items"].items()]
    out.sort(key=lambda i: i.get("added_at") or 0, reverse=True)
    return out


def allows(file_name: str) -> bool:
    return _key(file_name) in load()["items"]


def allowed_set() -> set[str]:
    """Every whitelisted stem, for callers checking a whole pack at once."""
    return set(load()["items"])


def add(file_name: str, *, name: str = "", reason: str = "") -> dict:
    data = load()
    k = _key(file_name)
    entry = {
        "name": name or file_name,
        "example_file": file_name,
        "reason": reason or "the operator decided it is safe on a server",
        "added_at": time.time(),
    }
    data["items"][k] = entry
    entry["persisted"] = _save(data)
    return {"key": k, **entry}


def remove(key_or_file: str) -> bool:
    data = load()
    k = key_or_file if key_or_file in data["items"] else _key(key_or_file)
    if k in data["items"]:
        del data["items"][k]
        _save(data)
        return True
    return False


def _reset_for_tests() -> None:
    global _cache
    _cache = None
