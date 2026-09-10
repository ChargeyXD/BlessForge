"""Operator decisions about the client-only review, remembered.

The review is deliberately cautious, and cautious is sometimes wrong in both
directions. A mod whose author writes `server_side: unsupported` usually means
"this adds nothing on a server", not "this breaks one" -- and a mod that
declares nothing at all can still be pure client code that takes the server
down on first boot. Without a way to say either, the only options are to fight
the review on every install or to stop trusting it.

So a decision made once is remembered, in two directions:

  * **allow**  -- "this is safe on a server, stop flagging it"
  * **block**  -- "this is client-only whatever it claims, always disable it"

and matched three ways, strongest first: the Modrinth/CurseForge project id,
the mod id out of the jar, then the filename stem with its version stripped.
Matching on the project id is what makes a decision survive a rename, and
matching on the stem is what makes it survive a version bump.

A decision is **global by default** -- the reason a mod is fine is a property
of the mod, not of the server it happens to be on -- but can be scoped to one
instance when it genuinely is situational.

Stored as one JSON file under DATA_DIR/state: small, hand-editable, and
obvious when someone wonders why a mod stopped being flagged. The v1 format (a flat
`items` map of stem -> entry) is read and migrated in place, so nobody loses
a list they already built.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

from app import config

_PATH = config.state_path("client-only-whitelist.json")

ALLOW, BLOCK = "allow", "block"
GLOBAL = "global"

# A trailing version, a loader tag, or a Minecraft version, in any of the
# shapes real jars use. Stripped so `jei-1.21.1-19.21.0` and
# `jei-1.21.1-19.22.0` are recognisably the same decision.
_VERSION_TAIL = re.compile(
    r"[-_+](?:v?\d[\w.]*|mc\d[\w.]*|forge|neoforge|fabric|quilt|"
    r"(?:for)?\d+\.\d+(?:\.\d+)?)$",
    re.I,
)


def stem_key(name: str) -> str:
    """The stable half of a jar name -- no extension, no version tail."""
    n = (name or "").strip().lower()
    n = n.rsplit("/", 1)[-1]
    for suffix in (".jar.disabled", ".jar", ".disabled"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    # Strip repeatedly: `create-1.20.1-0.5.1.f` has three tails.
    for _ in range(4):
        stripped = _VERSION_TAIL.sub("", n)
        if stripped == n:
            break
        n = stripped
    n = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    return n or (name or "").strip().lower()


# Kept as the v1 name so existing callers and tests keep working.
_key = stem_key


def _blank() -> dict[str, Any]:
    return {"schema": 2, "entries": []}


_cache: dict[str, Any] | None = None


def load() -> dict[str, Any]:
    """The list, read once and then held.

    Held in memory as well as on disk so a decision still applies for this run
    when the data directory turns out not to be writable -- a real state on
    this app (see the /data trap) that must not silently discard what the
    operator just chose.
    """
    global _cache
    if _cache is not None:
        return _cache
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    _cache = _migrate(data)
    return _cache


def _migrate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _blank()
    if isinstance(data.get("entries"), list):
        for e in data["entries"]:
            e.setdefault("verdict", ALLOW)
            e.setdefault("scope", GLOBAL)
        data.setdefault("schema", 2)
        return data
    # v1: {"items": {stem: {...}}}, every one of them an allow.
    entries = []
    for key, value in (data.get("items") or {}).items():
        if not isinstance(value, dict):
            continue
        entries.append({
            "key": key, "verdict": ALLOW, "scope": GLOBAL,
            "name": value.get("name") or key,
            "example_file": value.get("example_file") or "",
            "reason": value.get("reason") or "",
            "added_at": value.get("added_at") or time.time(),
            "mod_id": None, "project_id": None, "source": None,
        })
    return {"schema": 2, "entries": entries, "migrated_from": 1}


def _save(data: dict) -> bool:
    """True if it reached disk. The caller decides whether to say so."""
    global _cache
    _cache = data
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(data, indent=1, sort_keys=True),
                         encoding="utf-8")
        return True
    except OSError:
        return False


def items(*, server_id: str | None = None, verdict: str | None = None
          ) -> list[dict]:
    """Everything on the list, newest first, optionally scoped."""
    out = []
    for e in load()["entries"]:
        if verdict and e.get("verdict") != verdict:
            continue
        if server_id and e.get("scope") not in (GLOBAL, server_id):
            continue
        out.append(dict(e))
    out.sort(key=lambda i: i.get("added_at") or 0, reverse=True)
    return out


def _matches(entry: dict, *, key: str, mod_id: str | None,
             project_id: str | None) -> bool:
    if project_id and entry.get("project_id") \
            and str(entry["project_id"]) == str(project_id):
        return True
    if mod_id and entry.get("mod_id") \
            and str(entry["mod_id"]).lower() == str(mod_id).lower():
        return True
    return bool(key) and entry.get("key") == key


def decide(file_name: str, *, mod_id: str | None = None,
           project_id: str | None = None, server_id: str | None = None
           ) -> dict | None:
    """The entry that applies to this jar, or None.

    An instance-scoped decision beats a global one -- that is the only reason
    to scope anything -- and a block beats an allow, because "never run this
    on a server" is the more consequential of the two claims to get wrong.
    """
    key = stem_key(file_name)
    candidates = [
        e for e in load()["entries"]
        if e.get("scope") in (GLOBAL, server_id)
        and _matches(e, key=key, mod_id=mod_id, project_id=project_id)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda e: (
        0 if e.get("scope") != GLOBAL else 1,
        0 if e.get("verdict") == BLOCK else 1,
    ))
    return dict(candidates[0])


def allows(file_name: str, *, mod_id: str | None = None,
           project_id: str | None = None, server_id: str | None = None
           ) -> bool:
    entry = decide(file_name, mod_id=mod_id, project_id=project_id,
                   server_id=server_id)
    return bool(entry and entry["verdict"] == ALLOW)


def blocks(file_name: str, *, mod_id: str | None = None,
           project_id: str | None = None, server_id: str | None = None
           ) -> bool:
    entry = decide(file_name, mod_id=mod_id, project_id=project_id,
                   server_id=server_id)
    return bool(entry and entry["verdict"] == BLOCK)


def allowed_set() -> set[str]:
    """Every globally-allowed stem, for callers checking a whole pack."""
    return {e["key"] for e in load()["entries"]
            if e.get("verdict") == ALLOW and e.get("scope") == GLOBAL
            and e.get("key")}


def add(file_name: str, *, name: str = "", reason: str = "",
        verdict: str = ALLOW, scope: str = GLOBAL, mod_id: str | None = None,
        project_id: str | None = None, source: str | None = None,
        evidence: Iterable[str] | None = None) -> dict:
    """Record a decision, replacing any earlier one for the same mod."""
    if verdict not in (ALLOW, BLOCK):
        raise ValueError("verdict must be 'allow' or 'block'")
    if not (file_name or mod_id or project_id):
        raise ValueError("a file name, mod id or project id is required")

    data = load()
    key = stem_key(file_name) if file_name else ""
    entry = {
        "key": key,
        "verdict": verdict,
        "scope": scope or GLOBAL,
        "name": name or file_name or mod_id or str(project_id),
        "example_file": file_name or "",
        "mod_id": mod_id,
        "project_id": str(project_id) if project_id else None,
        "source": source,
        "reason": reason or (
            "the operator decided it is safe on a server" if verdict == ALLOW
            else "the operator marked it client-only"
        ),
        "evidence": list(evidence or [])[:6],
        "added_at": time.time(),
    }
    data["entries"] = [
        e for e in data["entries"]
        if not (e.get("scope") == entry["scope"]
                and _matches(e, key=key, mod_id=mod_id, project_id=project_id))
    ]
    data["entries"].append(entry)
    entry["persisted"] = _save(data)
    return entry


def remove(key_or_file: str, *, scope: str | None = None) -> bool:
    data = load()
    key = stem_key(key_or_file)
    before = len(data["entries"])
    data["entries"] = [
        e for e in data["entries"]
        if not (
            (e.get("key") in (key_or_file, key)
             or str(e.get("project_id") or "") == key_or_file
             or str(e.get("mod_id") or "") == key_or_file)
            and (scope is None or e.get("scope") == scope)
        )
    ]
    if len(data["entries"]) == before:
        return False
    _save(data)
    return True


def clear(*, scope: str | None = None) -> int:
    data = load()
    before = len(data["entries"])
    if scope:
        data["entries"] = [e for e in data["entries"]
                           if e.get("scope") != scope]
    else:
        data["entries"] = []
    _save(data)
    return before - len(data["entries"])


def export() -> dict:
    """The whole list, for backing up or moving between installs."""
    return {"schema": 2, "exported_at": time.time(),
            "entries": load()["entries"]}


def import_entries(payload: dict, *, replace: bool = False) -> dict:
    """Merge a previously exported list back in."""
    incoming = payload.get("entries")
    if not isinstance(incoming, list):
        # Tolerate a v1 export.
        migrated = _migrate(payload)
        incoming = migrated["entries"]
    data = _blank() if replace else load()
    existing = data["entries"] if not replace else []
    added = 0
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        entry = {
            "key": raw.get("key") or stem_key(raw.get("example_file") or ""),
            "verdict": raw.get("verdict") if raw.get("verdict") in (ALLOW, BLOCK)
            else ALLOW,
            "scope": raw.get("scope") or GLOBAL,
            "name": raw.get("name") or raw.get("example_file") or "?",
            "example_file": raw.get("example_file") or "",
            "mod_id": raw.get("mod_id"),
            "project_id": raw.get("project_id"),
            "source": raw.get("source"),
            "reason": raw.get("reason") or "imported",
            "evidence": raw.get("evidence") or [],
            "added_at": raw.get("added_at") or time.time(),
        }
        existing = [
            e for e in existing
            if not (e.get("scope") == entry["scope"]
                    and _matches(e, key=entry["key"], mod_id=entry["mod_id"],
                                 project_id=entry["project_id"]))
        ]
        existing.append(entry)
        added += 1
    data["entries"] = existing
    persisted = _save(data)
    return {"imported": added, "total": len(existing), "persisted": persisted}


def _reset_for_tests() -> None:
    global _cache
    _cache = None
