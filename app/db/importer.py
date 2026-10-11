"""Carry an existing install's JSON state into the database, once.

Before 2.2 everything BlessForge remembered lived in /data/state:

  users.json                   accounts and scrypt password records
  sessions.json                live sessions, keyed by token hash
  audit.jsonl                  the audit trail, one JSON object a line
  fleet-state.json             racks, rack assignments, server activity
  client-only-whitelist.json   the operator's allow/block decisions

On the first start with a database, and only if the database has no
accounts yet, each file that exists is read and its contents inserted --
all in ONE transaction, so an import is either complete or not there. Then
the files are renamed `<name>.imported` and a `meta` row records what was
carried, which is what stops it running twice even when the rename could
not happen (a read-only /data).

A file that is torn -- the 559-NUL-bytes case a power cut once produced --
is read as absent, exactly as the JSON stores always treated it: an install
whose users.json is unreadable starts with no accounts and gets the
first-run admin, rather than failing to start.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import func, select

from app import config, db, fleetgroups, whitelist
from app.db.models import (AuditEntry, Meta, ModDecision, Rack, RackMember,
                           ServerActivity, Session, User)

log = logging.getLogger(__name__)

FILES = {
    "users": "users.json",
    "sessions": "sessions.json",
    "audit": "audit.jsonl",
    "fleet": fleetgroups.STATE_FILE,
    "decisions": whitelist.LEGACY_FILE,
}
META_KEY = "json_import"


def _read_json(name: str) -> Any:
    text = config.read_state(config.state_path(name))
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _read_audit(name: str) -> list[dict]:
    text = config.read_state(config.state_path(name))
    out = []
    for line in (text or "").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("action"):
            out.append(rec)
    return out


async def run() -> dict:
    """Import once. Returns what happened, for the log and the tests."""
    async with db.session() as s:
        if await s.get(Meta, META_KEY) is not None:
            return {"imported": False, "reason": "already imported"}
        if (await s.execute(select(func.count(User.id)))).scalar_one():
            # A database somebody is already using: never merge into it.
            s.add(Meta(key=META_KEY, value={"at": time.time(), "skipped":
                                            "the database already had accounts"}))
            return {"imported": False, "reason": "database already in use"}

    present = {k: config.state_path(n) for k, n in FILES.items()
               if config.state_path(n).exists()}
    if not present:
        async with db.session() as s:
            s.add(Meta(key=META_KEY, value={"at": time.time(), "files": {}}))
        return {"imported": False, "reason": "nothing to import"}

    counts: dict[str, int] = {}
    async with db.session() as s:
        ids: dict[str, int] = {}

        users = _read_json(FILES["users"])
        for u in (users or {}).get("users", []) if isinstance(users, dict) else []:
            if not isinstance(u, dict) or not u.get("username") \
                    or not isinstance(u.get("password"), dict):
                continue
            row = User(
                username=str(u["username"])[:32], display=str(u.get("display") or "")[:60],
                role=u.get("role") if u.get("role") in ("admin", "operator", "member",
                                                        "guest") else "member",
                password_hash=u["password"],
                permissions=[p for p in (u.get("permissions") or []) if isinstance(p, str)],
                denied=[p for p in (u.get("denied") or []) if isinstance(p, str)],
                scope=u.get("scope") if isinstance(u.get("scope"), dict) else {},
                must_change=bool(u.get("must_change")),
                failures=int(u.get("failures") or 0),
                locked_until=db.from_epoch(u.get("locked_until")),
                last_login=db.from_epoch(u.get("last_login")),
                disabled=bool(u.get("disabled")),
                created_at=db.from_epoch(u.get("created_at")) or db.from_epoch(time.time()),
            )
            s.add(row)
            await s.flush()
            ids[row.username] = row.id
        counts["users"] = len(ids)

        sessions = _read_json(FILES["sessions"])
        n = 0
        now = time.time()
        for key, rec in ((sessions or {}).get("sessions") or {}).items() \
                if isinstance(sessions, dict) else []:
            if not isinstance(rec, dict) or (rec.get("expires") or 0) <= now:
                continue
            uid = ids.get(rec.get("username"))
            if not uid or len(str(key)) != 64:
                continue
            s.add(Session(token_sha256=str(key), user_id=uid,
                          created_at=db.from_epoch(rec.get("created")) or db.from_epoch(now),
                          last_seen=db.from_epoch(rec.get("last_seen")) or db.from_epoch(now),
                          expires_at=db.from_epoch(rec["expires"]),
                          ip=str(rec.get("ip") or "")[:64],
                          user_agent=str(rec.get("agent") or "")[:200]))
            n += 1
        counts["sessions"] = n

        audit = _read_audit(FILES["audit"])
        for rec in audit:
            s.add(AuditEntry(
                at=db.from_epoch(rec.get("at")) or db.from_epoch(now),
                actor=str(rec.get("actor") or "?")[:64],
                action=str(rec.get("action"))[:64],
                target=str(rec.get("server_id") or "")[:64],
                detail=str(rec.get("detail") or "")[:2000],
                ip=str(rec.get("ip") or "")[:64], ok=bool(rec.get("ok", True))))
        counts["audit"] = len(audit)

        fleet = fleetgroups.clean_state(_read_json(FILES["fleet"]))
        rack_ids = set()
        for i, g in enumerate(fleet["groups"]):
            if g["id"] in rack_ids:
                continue
            rack_ids.add(g["id"])
            s.add(Rack(id=g["id"], name=g["name"], motif=g["motif"], position=i))
        await s.flush()
        for sid, gid in fleet["assign"].items():
            if gid in rack_ids:
                s.add(RackMember(server_id=sid[:64], rack_id=gid))
        for sid, rec in fleet["activity"].items():
            s.add(ServerActivity(server_id=sid[:64], record=rec))
        counts["racks"] = len(rack_ids)

        decisions = whitelist._migrate(_read_json(FILES["decisions"]))
        for e in decisions["entries"]:
            if not isinstance(e, dict):
                continue
            s.add(ModDecision(
                key=str(e.get("key") or "")[:200], verdict=e.get("verdict") or "allow",
                scope=str(e.get("scope") or "global")[:64],
                name=str(e.get("name") or "")[:200],
                example_file=str(e.get("example_file") or "")[:255],
                mod_id=e.get("mod_id"),
                project_id=str(e["project_id"]) if e.get("project_id") else None,
                source=e.get("source"), reason=str(e.get("reason") or ""),
                evidence=list(e.get("evidence") or []),
                added_at=float(e.get("added_at") or now)))
        counts["decisions"] = len(decisions["entries"])

        s.add(Meta(key=META_KEY, value={"at": now, "files": sorted(present),
                                        "counts": counts}))

    # Only once the import has committed: renaming first and then failing
    # would leave the data in neither place.
    renamed = []
    for key, path in present.items():
        try:
            path.replace(path.with_name(path.name + ".imported"))
            renamed.append(path.name)
        except OSError as e:
            log.warning("imported %s but could not rename it: %s", path, e)
    log.info("imported the JSON state into the database: %s", counts)
    return {"imported": True, "counts": counts, "renamed": renamed}
