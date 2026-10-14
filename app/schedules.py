"""Scheduled tasks: restarts, commands and backups on a clock, or chained.

From the server-manager comparison in design/ (item 2). BlessForge's
own performance advice says "a daily restart keeps the heap from creeping up",
and until now nothing could do it but a person.

A schedule is one row in `schedules`:

  * `cron` -- when: five cron fields (minute hour day month weekday), an alias
    (@hourly, @daily, @weekly, @midnight), or `@every <minutes>`. Empty for a
    step that only runs after its parent.
  * `action` -- command, say, start, stop, restart, backup, update_jar (a
    Paper/Purpur/Folia server onto its newest build, app/jarupdates.py).
  * `payload` -- the rest: name, command or message, tz, one_time, and for a
    chained step `parent` and `delay` (seconds after the parent ran); plus
    what happened last time (`last_run`, `last_result`, `last_ok`).
  * `next_run` -- when it is due, in UTC.

Chains are how "say Restarting in 5 minutes" -> 4 min -> "say 1 minute" ->
1 min -> restart is built: the first step has the clock, each later step
names its parent and a delay. When a step runs, its children are given a
next_run of now + delay.

Exactly once, however many processes run the loop: a run is *claimed* by
moving next_run on with a conditional UPDATE, and only the process whose
update matched runs it. A run missed by more than MISSED_GRACE (the panel was
down) is skipped and said so, not fired late -- a restart at 14:00 instead of
05:00 is worse than none.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from typing import Any

from sqlalchemy import delete, select, update

from app import config, db, engine
from app.db.models import Schedule

log = logging.getLogger(__name__)

ACTIONS = ("command", "say", "start", "stop", "restart", "backup", "update_jar")
TICK = 20                           # seconds between looks at the table
MISSED_GRACE = 600                  # a run this late is skipped, not fired
MAX_PER_SERVER = 64
ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
           "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}
_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_NAMES = {3: {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])},
          4: {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}}


class ScheduleError(ValueError):
    """A schedule that cannot be made as asked (a 400, with the reason)."""


# --- when -------------------------------------------------------------------


def _field(text: str, index: int) -> tuple[set[int], bool]:
    """One cron field: its values, and whether it was '*' (unrestricted)."""
    lo, hi = _FIELDS[index]
    names = _NAMES.get(index, {})
    out: set[int] = set()
    star = text.strip() == "*"
    for part in text.lower().split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise ScheduleError(f"bad step in '{text}'")
            step = int(step_s)
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = names.get(a, a), names.get(b, b)
        else:
            start = end = names.get(part, part)
        try:
            start, end = int(start), int(end)
        except ValueError:
            raise ScheduleError(f"'{text}' is not a cron field")
        if index == 4:          # 7 is Sunday too
            start, end = (0 if start == 7 else start), (0 if end == 7 else end)
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise ScheduleError(f"'{text}' is out of range ({lo}-{hi})")
        out.update(range(start, end + 1, step))
    return out, star


def parse(expr: str) -> dict:
    """Validate a `cron` value. Returns {"every": minutes} or the five fields."""
    expr = (expr or "").strip().lower()
    expr = ALIASES.get(expr, expr)
    m = re.fullmatch(r"@every\s+(\d+)\s*(m|min|minutes?|h|hours?)?", expr)
    if m:
        minutes = int(m.group(1)) * (60 if (m.group(2) or "m").startswith("h") else 1)
        if not 1 <= minutes <= 60 * 24 * 31:
            raise ScheduleError("@every is between 1 minute and 31 days")
        return {"every": minutes}
    parts = expr.split()
    if len(parts) != 5:
        raise ScheduleError("a schedule is five cron fields (minute hour day month "
                            "weekday), @daily / @hourly / @weekly, or @every 30m")
    fields = [_field(p, i) for i, p in enumerate(parts)]
    return {"fields": fields}


def _zone(name: str | None) -> dt.tzinfo:
    if not name:
        name = config.SCHEDULE_TZ
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:       # noqa: BLE001 -- an unknown or missing zone: UTC
        return dt.timezone.utc


def next_after(expr: str, after: dt.datetime, tz: str | None = None,
               last_run: dt.datetime | None = None) -> dt.datetime | None:
    """The next time `expr` fires strictly after `after`, as UTC."""
    spec = parse(expr)
    if "every" in spec:
        base = last_run or after
        step = dt.timedelta(minutes=spec["every"])
        nxt = base + step
        while nxt <= after:
            nxt += step
        return nxt.astimezone(dt.timezone.utc)
    (mins, _), (hours, _), (doms, dom_star), (months, _), (dows, dow_star) = spec["fields"]
    zone = _zone(tz)
    local = after.astimezone(zone).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    day = local.date()
    for _ in range(366 * 5):                 # a Feb 29 schedule waits up to 4 years
        if day.month in months:
            dom_ok, dow_ok = day.day in doms, (day.isoweekday() % 7) in dows
            if (dom_ok and dow_ok) if (dom_star or dow_star) else (dom_ok or dow_ok):
                for h in sorted(hours):
                    for mi in sorted(mins):
                        when = dt.datetime(day.year, day.month, day.day, h, mi, tzinfo=zone)
                        if when >= local:
                            return when.astimezone(dt.timezone.utc)
        day += dt.timedelta(days=1)
    return None


def upcoming(expr: str, tz: str | None = None, count: int = 5) -> list[float]:
    """The next few times, as epoch seconds -- for a form to show before saving."""
    out, cursor, last = [], dt.datetime.now(dt.timezone.utc), None
    for _ in range(count):
        nxt = next_after(expr, cursor, tz, last)
        if nxt is None:
            break
        out.append(nxt.timestamp())
        cursor = last = nxt
    return out


# --- what --------------------------------------------------------------------


def _clean(action: str, payload: dict) -> dict:
    if action not in ACTIONS:
        raise ScheduleError(f"action is one of {', '.join(ACTIONS)}")
    out = {"name": str(payload.get("name") or action.title())[:80]}
    if action in ("command", "say"):
        key = "command" if action == "command" else "message"
        text = str(payload.get(key) or "").strip().lstrip("/" if action == "command" else "")
        if not text or len(text.splitlines()) != 1 or len(text) > 256:
            raise ScheduleError(f"a {action} task needs one line of {key}, up to 256 characters")
        out[key] = text
    if action == "backup":
        keep = payload.get("keep")
        if keep is not None:
            try:
                keep = int(keep)
            except (TypeError, ValueError):
                raise ScheduleError("keep is a number of backups")
            if not 1 <= keep <= 500:
                raise ScheduleError("keep is between 1 and 500")
            out["keep"] = keep
    if payload.get("tz"):
        out["tz"] = str(payload["tz"])[:64]
    if payload.get("one_time"):
        out["one_time"] = True
    if payload.get("parent") is not None:
        out["parent"] = int(payload["parent"])
        delay = int(payload.get("delay") or 0)
        if not 0 <= delay <= 86400:
            raise ScheduleError("a step runs 0 seconds to 24 hours after the one before")
        out["delay"] = delay
    if payload.get("group"):
        out["group"] = str(payload["group"])[:80]
    return out


def view(row: Schedule) -> dict:
    p = dict(row.payload or {})
    return {"id": row.id, "server_id": row.server_id, "cron": row.cron,
            "action": row.action, "enabled": row.enabled,
            "next_run": db.epoch(row.next_run), **p}


async def list_for(server_id: str) -> list[dict]:
    async with db.session() as s:
        rows = (await s.execute(select(Schedule).where(Schedule.server_id == server_id)
                                .order_by(Schedule.id))).scalars().all()
    return [view(r) for r in rows]


async def create(server_id: str, *, action: str, cron: str = "",
                 payload: dict | None = None, enabled: bool = True) -> dict:
    payload = _clean(action, dict(payload or {}))
    now = dt.datetime.now(dt.timezone.utc)
    if payload.get("parent") is not None:
        cron = ""
        nxt = None
        async with db.session() as s:
            parent = await s.get(Schedule, payload["parent"])
            if parent is None or parent.server_id != server_id:
                raise ScheduleError("that step's parent is not a task of this server")
    else:
        if not cron:
            raise ScheduleError("a task needs a time (or a parent to follow)")
        nxt = next_after(cron, now, payload.get("tz"))
    async with db.session() as s:
        count = len((await s.execute(select(Schedule.id).where(
            Schedule.server_id == server_id))).all())
        if count >= MAX_PER_SERVER:
            raise ScheduleError(f"a server has at most {MAX_PER_SERVER} scheduled tasks")
        row = Schedule(server_id=server_id, cron=cron, action=action, payload=payload,
                       enabled=bool(enabled), next_run=nxt if enabled else None)
        s.add(row)
        await s.flush()
        return view(row)


async def update_task(server_id: str, schedule_id: int, changes: dict) -> dict:
    async with db.session() as s:
        row = await s.get(Schedule, schedule_id)
        if row is None or row.server_id != server_id:
            raise LookupError("no such task")
        payload = dict(row.payload or {})
        action = changes.get("action", row.action)
        merged = {**payload, **{k: v for k, v in (changes.get("payload") or {}).items()}}
        clean = _clean(action, merged)
        for k in ("last_run", "last_result", "last_ok"):
            if k in payload:
                clean[k] = payload[k]
        row.action = action
        row.payload = clean
        if "enabled" in changes:
            row.enabled = bool(changes["enabled"])
        if clean.get("parent") is None:
            cron = changes.get("cron", row.cron)
            parse(cron)
            row.cron = cron
            row.next_run = (next_after(cron, dt.datetime.now(dt.timezone.utc), clean.get("tz"))
                            if row.enabled else None)
        elif not row.enabled:
            row.next_run = None
        await s.flush()
        return view(row)


async def remove(server_id: str, schedule_id: int) -> list[int]:
    """Delete a task and every step chained after it."""
    async with db.session() as s:
        rows = (await s.execute(select(Schedule).where(
            Schedule.server_id == server_id))).scalars().all()
        by_parent: dict[int, list[int]] = {}
        for r in rows:
            parent = (r.payload or {}).get("parent")
            if parent is not None:
                by_parent.setdefault(int(parent), []).append(r.id)
        if schedule_id not in {r.id for r in rows}:
            raise LookupError("no such task")
        gone, todo = [], [schedule_id]
        while todo:
            sid = todo.pop()
            gone.append(sid)
            todo.extend(by_parent.get(sid, []))
        await s.execute(delete(Schedule).where(Schedule.id.in_(gone)))
    return gone


# --- presets ------------------------------------------------------------------


async def preset(server_id: str, kind: str, opts: dict) -> list[dict]:
    """One-click chains. `daily_restart`: warnings at the minutes in `warn`
    (default 5 and 1) before a restart at `time` (HH:MM, in `tz`).
    `announce`: `message` on `cron`. `backup_every`: a backup every `hours`,
    keeping `keep`."""
    tz = opts.get("tz")
    if kind == "daily_restart":
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(opts.get("time") or "05:00"))
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ScheduleError("time is HH:MM")
        warn = sorted({int(w) for w in (opts.get("warn") or [5, 1]) if 0 < int(w) <= 120},
                      reverse=True)
        at = dt.datetime(2000, 1, 1, int(m.group(1)), int(m.group(2)))
        group = f"Daily restart at {m.group(1).zfill(2)}:{m.group(2)}"
        made, first = [], at - dt.timedelta(minutes=warn[0] if warn else 0)
        cron = f"{first.minute} {first.hour} * * *"
        steps = [(w, "say", {"message": f"Server restarts in {w} minute"
                                        f"{'' if w == 1 else 's'}."}) for w in warn]
        steps.append((0, "restart", {}))
        parent, prev_min = None, warn[0] if warn else 0
        for minutes_before, action, extra in steps:
            payload = {"name": group if parent is None else f"{action} ({minutes_before} min)",
                       "group": group, "tz": tz, **extra}
            if parent is None:
                row = await create(server_id, action=action, cron=cron, payload=payload)
            else:
                payload.update(parent=parent, delay=(prev_min - minutes_before) * 60)
                row = await create(server_id, action=action, payload=payload)
            made.append(row)
            parent, prev_min = row["id"], minutes_before
        return made
    if kind == "announce":
        return [await create(server_id, action="say", cron=str(opts.get("cron") or "@hourly"),
                             payload={"name": "Announcement", "message": opts.get("message"),
                                      "tz": tz})]
    if kind == "backup_every":
        hours = int(opts.get("hours") or 6)
        if not 1 <= hours <= 24 * 7:
            raise ScheduleError("hours is between 1 and 168")
        return [await create(server_id, action="backup", cron=f"@every {hours}h",
                             payload={"name": f"Backup every {hours} h",
                                      "keep": opts.get("keep") or 8, "tz": tz})]
    raise ScheduleError("preset is daily_restart, announce or backup_every")


# --- running ------------------------------------------------------------------


async def _do(row: Schedule) -> tuple[bool, str]:
    """Carry out one task. (ok, what happened) -- never raises."""
    p = row.payload or {}
    sid = row.server_id
    try:
        running = bool((await engine.get_stats(sid)).get("running"))
    except Exception:       # noqa: BLE001
        running = False
    try:
        if row.action in ("command", "say"):
            if not running:
                return True, "skipped: the server is not running"
            line = p["command"] if row.action == "command" else f"say {p['message']}"
            await engine.send_command(sid, line)
            return True, f"sent: {line[:120]}"
        if row.action == "start":
            if running:
                return True, "skipped: already running"
            await engine.power(sid, "start")
            return True, "started"
        if row.action == "stop":
            if not running:
                return True, "skipped: not running"
            await engine.power(sid, "stop")
            return True, "stopped"
        if row.action == "restart":
            if not running:
                return True, "skipped: not running, so nothing to restart"
            await engine.power(sid, "restart")
            return True, "restarted"
        if row.action == "backup":
            from app import worldbackups
            made = await worldbackups.take(sid, reason=p.get("name") or "scheduled",
                                           keep=p.get("keep"))
            return True, f"backup {made.get('id')} ({made.get('size_human', '')})"
        if row.action == "update_jar":
            from app import jarupdates
            out = await jarupdates.apply(sid)
            if not out.get("updated"):
                return True, f"already on the newest build ({out.get('current')})"
            return True, (f"updated {out.get('from') or '?'} -> {out.get('to')}"
                          + (", restarted" if out.get("restarted") else ""))
    except Exception as e:      # noqa: BLE001 -- recorded on the task
        return False, str(e)[:300]
    return False, f"unknown action {row.action}"


async def run_task(row: Schedule, *, actor: str = "schedule") -> dict:
    """Run one task now and set its chained steps going."""
    ok, result = await _do(row)
    now = dt.datetime.now(dt.timezone.utc)
    async with db.session() as s:
        fresh = await s.get(Schedule, row.id)
        if fresh is not None:
            fresh.payload = {**(fresh.payload or {}), "last_run": now.timestamp(),
                             "last_result": result, "last_ok": ok}
            children = (await s.execute(select(Schedule).where(
                Schedule.server_id == row.server_id))).scalars().all()
            for child in children:
                cp = child.payload or {}
                if cp.get("parent") == row.id and child.enabled:
                    child.next_run = now + dt.timedelta(seconds=int(cp.get("delay") or 0))
    try:
        from app import auth
        await auth.audit(actor=actor, action="schedule.run", server_id=row.server_id,
                         detail=f"{(row.payload or {}).get('name') or row.action}: {result}",
                         ok=ok)
    except Exception:       # noqa: BLE001
        pass
    return {"ok": ok, "result": result}


async def run_due(now: dt.datetime | None = None) -> list[int]:
    """Claim and run every task that is due. Returns the ids that ran."""
    now = now or dt.datetime.now(dt.timezone.utc)
    async with db.session() as s:
        due = (await s.execute(select(Schedule).where(
            Schedule.enabled.is_(True), Schedule.next_run.is_not(None),
            Schedule.next_run <= now))).scalars().all()
    ran = []
    for row in due:
        p = row.payload or {}
        old = row.next_run
        if p.get("parent") is not None or p.get("one_time"):
            new = None
        else:
            try:
                new = next_after(row.cron, now, p.get("tz"), last_run=old)
            except ScheduleError:
                new = None
        async with db.session() as s:
            claimed = (await s.execute(
                update(Schedule).where(Schedule.id == row.id, Schedule.next_run == old)
                .values(next_run=new).execution_options(synchronize_session=False)
            )).rowcount
        if claimed != 1:
            continue                        # another process has it
        late = (now - _aware(old)).total_seconds()
        if late > MISSED_GRACE:
            async with db.session() as s:
                fresh = await s.get(Schedule, row.id)
                if fresh is not None:
                    fresh.payload = {**(fresh.payload or {}), "last_run": now.timestamp(),
                                     "last_ok": False,
                                     "last_result": f"missed: due {int(late // 60)} min ago "
                                                    "while BlessForge was not running"}
            continue
        await run_task(row)
        if p.get("one_time"):
            async with db.session() as s:
                await s.execute(update(Schedule).where(Schedule.id == row.id)
                                .values(enabled=False))
        ran.append(row.id)
    return ran


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


async def run_forever() -> None:
    """The scheduler loop: one per process that runs it (the worker with
    Redis, the panel without). Safe to run in several: runs are claimed."""
    await asyncio.sleep(5)
    while True:
        try:
            await run_due()
        except asyncio.CancelledError:
            raise
        except Exception as e:      # noqa: BLE001 -- next tick tries again
            log.warning("scheduler: %s", e)
        await asyncio.sleep(TICK)
