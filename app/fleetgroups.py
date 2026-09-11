"""Racks, and when each server was last actually up.

Two small pieces of state that belong to the fleet as a whole rather than
to any one server, so neither can live in a `.blessforge.json` manifest:

  **racks** -- user-made groups, and which server hangs on which. Central
  rather than per-instance because the interesting operation is "show me
  the racks", and answering that from per-instance manifests means
  reading every manifest on every paint. Deleting a server drops its
  assignment, which `document()` does on the way out rather than by
  hunting for orphans on delete.

  **activity** -- the answer to "when did this last run", which Crafty
  does not keep. The fleet poll hands over what it can see every few
  seconds and this timestamps it with the server's own clock. That makes
  `last_running_at` honest whether or not BlessForge was the thing that
  started the server, and it is the key the rail sorts on.

Both are best effort. If `/data` cannot be written the state still works
for the life of the process and every response says `persisted: false`,
which the front end surfaces once rather than failing the action -- losing
a rack layout on restart is a disappointment, refusing to let someone make
one is a fault.

There is no database here by design; this follows `whitelist.py`.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from typing import Any

from .config import read_state, state_path, write_state

STATE_FILE = "fleet-state.json"

# The shrine marks a rack can wear. Kept in step with MOTIFS in app.js --
# the front end draws them, this only validates that a stored one is real
# so a hand-edited state file cannot put an arbitrary string into a class
# name.
MOTIFS = ("sakura", "kitsune", "torii", "matsu", "tsuki", "mizu",
          "kaminari", "yuki")

MAX_GROUPS = 40
MAX_NAME = 48

_state: dict[str, Any] | None = None
_persisted = True
_last_tick_save = 0.0


# --- storage ------------------------------------------------------------

def _blank() -> dict[str, Any]:
    return {"version": 1, "groups": [], "assign": {}, "activity": {},
            "updated_at": 0.0}


def _load() -> dict[str, Any]:
    global _state
    if _state is not None:
        return _state
    path = state_path(STATE_FILE)
    # read_state returns None for a file that is missing OR torn -- a
    # half-written or all-NUL file is not data, and starting empty is the
    # only safe reading of it.
    text = read_state(path)
    try:
        raw = json.loads(text) if text else None
    except ValueError:
        raw = None
    if raw is None:
        _state = _blank()
        return _state
    state = _blank()
    if isinstance(raw, dict):
        groups = raw.get("groups")
        if isinstance(groups, list):
            state["groups"] = [g for g in (_clean_group(x) for x in groups) if g]
        assign = raw.get("assign")
        if isinstance(assign, dict):
            state["assign"] = {str(k): str(v) for k, v in assign.items()
                               if isinstance(v, str)}
        activity = raw.get("activity")
        if isinstance(activity, dict):
            state["activity"] = {str(k): v for k, v in activity.items()
                                 if isinstance(v, dict)}
        state["updated_at"] = float(raw.get("updated_at") or 0)
    _state = state
    return _state


def _save() -> bool:
    """Write the state. Never raises -- the caller carries on either way."""
    global _persisted
    state = _load()
    state["updated_at"] = time.time()
    path = state_path(STATE_FILE)
    # Was tmp-write-then-rename, which is atomic but NOT durable: on a hard
    # restart the rename reached the disk before the bytes did and this file
    # came back as 559 NUL bytes. write_state fsyncs before renaming.
    _persisted = write_state(path, json.dumps(state, indent=1))
    return _persisted


def _slug(name: str) -> str:
    base = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base or "rack"


def _clean_group(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:MAX_NAME]
    if not name:
        return None
    gid = str(raw.get("id") or "").strip() or _slug(name)
    motif = raw.get("motif")
    return {
        "id": gid[:64],
        "name": name,
        "motif": motif if motif in MOTIFS else MOTIFS[0],
    }


def _unique_id(want: str, taken: set[str]) -> str:
    if want not in taken:
        return want
    for n in range(2, 200):
        candidate = f"{want}-{n}"
        if candidate not in taken:
            return candidate
    return f"{want}-{int(time.time())}"


# --- racks --------------------------------------------------------------

def document(known: set[str] | None = None) -> dict[str, Any]:
    """The whole rack layout, as the front end wants it.

    `known` is the set of server ids that currently exist. Assignments to
    anything outside it are dropped from the answer -- a server deleted in
    Crafty, or by someone else while this page was open, simply stops
    being on a rack. They are dropped from the ANSWER rather than from
    storage, so a Crafty that is briefly unreachable (and therefore
    reports no servers at all) does not silently wipe the layout.
    """
    state = _load()
    assign = state["assign"]
    if known is not None:
        assign = {k: v for k, v in assign.items() if k in known}
    ids = {g["id"] for g in state["groups"]}
    members: dict[str, list[str]] = {gid: [] for gid in ids}
    for server_id, gid in assign.items():
        if gid in members:
            members[gid].append(server_id)
    return {
        "groups": [dict(g, server_ids=sorted(members.get(g["id"], [])))
                   for g in state["groups"]],
        "assign": {k: v for k, v in assign.items() if v in ids},
        "motifs": list(MOTIFS),
        "updated_at": state["updated_at"],
        "persisted": _persisted,
    }


def save_group(gid: str | None, name: str, motif: str | None,
               known: set[str] | None = None) -> dict[str, Any]:
    """Create a rack, or rename/remark an existing one."""
    state = _load()
    cleaned = _clean_group({"id": gid, "name": name, "motif": motif})
    if cleaned is None:
        raise ValueError("A rack needs a name.")

    existing = next((g for g in state["groups"] if g["id"] == gid), None) \
        if gid else None
    if existing is None:
        if len(state["groups"]) >= MAX_GROUPS:
            raise ValueError(f"That is {MAX_GROUPS} racks already. Take one "
                             "down before hanging another.")
        cleaned["id"] = _unique_id(cleaned["id"],
                                   {g["id"] for g in state["groups"]})
        state["groups"].append(cleaned)
        saved = cleaned
    else:
        existing["name"] = cleaned["name"]
        existing["motif"] = cleaned["motif"]
        saved = existing

    _save()
    doc = document(known)
    doc["group"] = next((g for g in doc["groups"] if g["id"] == saved["id"]),
                        None)
    return doc


def delete_group(gid: str, known: set[str] | None = None) -> dict[str, Any]:
    state = _load()
    state["groups"] = [g for g in state["groups"] if g["id"] != gid]
    state["assign"] = {k: v for k, v in state["assign"].items() if v != gid}
    _save()
    return document(known)


def assign(server_ids: list[str], group_id: str | None,
           known: set[str] | None = None) -> dict[str, Any]:
    """Hang servers on a rack, or (with group_id None) back in the yard."""
    state = _load()
    if group_id and not any(g["id"] == group_id for g in state["groups"]):
        raise ValueError("That rack is not there any more.")
    for server_id in server_ids:
        sid = str(server_id)
        if group_id:
            state["assign"][sid] = group_id
        else:
            state["assign"].pop(sid, None)
    _save()
    return document(known)


def plan(groups: list[Any], known: set[str] | None = None) -> dict[str, Any]:
    """Hang several racks at once, from a suggested layout.

    Additive: a suggestion never takes down a rack that is already there,
    because the button that sends one of these says "hang these" and a
    button that silently replaced someone's layout would be a different
    button. A server named twice lands on the last rack that claims it,
    which is the same rule a manual drag follows.
    """
    state = _load()
    made: list[str] = []
    for raw in groups[:MAX_GROUPS]:
        cleaned = _clean_group(raw)
        if cleaned is None:
            continue
        if len(state["groups"]) >= MAX_GROUPS:
            break
        cleaned["id"] = _unique_id(cleaned["id"],
                                   {g["id"] for g in state["groups"]})
        state["groups"].append(cleaned)
        made.append(cleaned["id"])
        ids = raw.get("server_ids") if isinstance(raw, dict) else None
        for server_id in (ids or []):
            state["assign"][str(server_id)] = cleaned["id"]
    _save()
    doc = document(known)
    doc["made"] = made
    return doc


# --- activity -----------------------------------------------------------

def observe(items: list[Any], full: bool = False) -> dict[str, Any]:
    """Record what the fleet poll just saw, and hand back the whole record.

    `last_started` is only set on a witnessed down -> up edge. The first
    time an already-running server is seen there was no "down" to witness,
    so claiming an uptime would be a guess the user could disprove by
    opening the console -- `last_started_precision` says which of the two
    happened and the rail only prints an uptime for `observed`.
    """
    global _last_tick_save
    state = _load()
    activity = state["activity"]
    now = time.time()
    seen: set[str] = set()
    notable = False       # something happened that must reach disk now
    ticked = False        # only a clock update; it can wait

    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("server_id") or "").strip()
        if not sid:
            continue
        seen.add(sid)
        rec = activity.setdefault(sid, {})
        # No `running` key means this server has never been polled before,
        # so there is no previous state for it to have transitioned FROM.
        first_sighting = "running" not in rec
        if "first_seen" not in rec:
            rec["first_seen"] = now
            notable = True
        if raw.get("name"):
            rec["name"] = str(raw["name"])[:120]

        was_running = bool(rec.get("running"))
        running = bool(raw.get("running"))
        if running and not was_running:
            rec["last_started"] = now
            # An uptime is only honest when the down -> up edge was
            # actually witnessed. Finding a server already up on the very
            # first poll says nothing about when it started, and guessing
            # would be a lie the user can catch by opening the console.
            rec["last_started_precision"] = ("unknown" if first_sighting
                                             else "observed")
            notable = True
        if running:
            rec["last_running_at"] = now
            ticked = True
        rec["running"] = running

    # A full sweep is authoritative about what exists, so anything absent
    # has been deleted and its record can go. A partial one says nothing
    # about the servers it did not mention.
    if full and seen:
        for sid in [k for k in activity if k not in seen]:
            activity.pop(sid, None)
            notable = True

    # The fleet polls every few seconds and a running server moves
    # `last_running_at` on every single one. Writing /data that often, for
    # a number whose only reader says "ran 4 minutes ago", is pure wear on
    # the disk. Transitions land immediately; the ticking clock lands at
    # most once a minute.
    if notable or (ticked and now - _last_tick_save > 60):
        if ticked:
            _last_tick_save = now
        _save()
    return {"servers": activity, "at": now, "persisted": _persisted}


def forget(server_id: str) -> None:
    """Drop everything remembered about a server that has been deleted."""
    state = _load()
    if state["activity"].pop(str(server_id), None) is not None \
            or state["assign"].pop(str(server_id), None) is not None:
        _save()


def reset_for_tests() -> None:
    global _state, _persisted, _last_tick_save
    _state = None
    _persisted = True
    _last_tick_save = 0.0
