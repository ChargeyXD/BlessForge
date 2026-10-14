"""Stats history: players, CPU and memory over time, per server.

From the server-manager comparison in design/ (item 6). The live gauges say
how a server is doing now; this says how it has been doing, which is the
question behind "does it lag every evening" and "is the heap creeping up".

Once a minute the loop reads every server's stats (the same cached reading
the gauges use, so it costs the nodes nothing extra) and writes one row per
server into `server_stats`. A node that is not answering is skipped, not
recorded as zeros: a gap in the chart is honest, a dip to nothing is not.

Rows are one-minute readings for the last KEEP_RAW, then rolled up into
ROLLED-minute buckets (CPU and memory averaged, the player peak kept, the
running minutes counted) and kept for KEEP_ALL. The unique key on
(server, minute, resolution) means two processes recording the same minute
write it once, and the roll-up only ever takes whole buckets, so it can run
anywhere, any number of times.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Iterable

from sqlalchemy import and_, delete, select

from app import db, engine
from app.db.models import Server, ServerStat

log = logging.getLogger(__name__)

EVERY = 60                              # seconds between readings
ROLLED = 15                             # minutes in a rolled-up row
KEEP_RAW = dt.timedelta(hours=48)       # one-minute rows kept this long
KEEP_ALL = dt.timedelta(days=30)        # anything older is deleted
COMPACT_EVERY = 3600                    # seconds between roll-ups
MAX_HOURS = 24 * 30
MAX_POINTS = 360                        # what a chart is given, at most
_BATCH = 20000                          # raw rows rolled up per pass
_CHUNK = 500                            # rows per statement (bound-parameter limits)


def _minute(t: dt.datetime) -> dt.datetime:
    return t.replace(second=0, microsecond=0)


def _bucket(t: dt.datetime, minutes: int) -> dt.datetime:
    t = _minute(t)
    since = t.hour * 60 + t.minute
    return t.replace(hour=0, minute=0) + dt.timedelta(minutes=since - since % minutes)


def _aware(t: dt.datetime) -> dt.datetime:
    # SQLite gives naive datetimes back; everything written here is UTC.
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


async def _insert(rows: list[dict]) -> None:
    """Insert, leaving any (server, minute, resolution) already there alone."""
    if not rows:
        return
    if db.dialect() == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    async with db.session() as s:
        for i in range(0, len(rows), _CHUNK):
            await s.execute(insert(ServerStat).values(rows[i:i + _CHUNK])
                            .on_conflict_do_nothing(index_elements=["server_id", "at", "res"]))


def _reading(server_id: str, at: dt.datetime, stats: dict) -> dict:
    up = bool(stats.get("running"))
    cpu = stats.get("cpu")
    mem = stats.get("mem")
    return {"server_id": server_id, "at": at, "res": 1, "samples": 1, "up": int(up),
            "online": int(stats.get("online") or 0) if up else 0,
            "max_players": stats.get("max") if up else None,
            "cpu": float(cpu) if up and cpu is not None else None,
            "mem": int(mem) if up and mem else None,
            "mem_limit": int(stats["mem_limit"]) if stats.get("mem_limit") else None}


async def record(now: dt.datetime | None = None) -> int:
    """One reading of every server the panel knows. Returns rows offered."""
    at = _minute(now or db_now())
    async with db.session() as s:
        known = list((await s.execute(select(Server.uuid))).scalars().all())
    if not known:
        return 0
    found = await asyncio.gather(*(engine.get_stats(sid) for sid in known),
                                 return_exceptions=True)
    rows = []
    for sid, stats in zip(known, found):
        if isinstance(stats, BaseException) or not isinstance(stats, dict):
            continue                # not reachable: a gap, not a zero
        if stats.get("node_down"):
            continue                # an old reading, not this minute's
        rows.append(_reading(sid, at, stats))
    await _insert(rows)
    return len(rows)


def db_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _roll(rows: Iterable[Any], minutes: int) -> list[dict]:
    """Rows -> one row per (server, bucket): CPU and memory averaged over the
    running readings, the player peak, the running minutes summed."""
    groups: dict[tuple[str, dt.datetime], list[Any]] = {}
    for r in rows:
        groups.setdefault((r.server_id, _bucket(_aware(r.at), minutes)), []).append(r)
    out = []
    for (sid, at), rs in groups.items():
        cpus = [r.cpu for r in rs if r.cpu is not None]
        mems = [r.mem for r in rs if r.mem]
        limits = [r.mem_limit for r in rs if r.mem_limit]
        maxes = [r.max_players for r in rs if r.max_players]
        out.append({"server_id": sid, "at": at, "res": minutes,
                    "samples": sum(r.samples for r in rs), "up": sum(r.up for r in rs),
                    "online": max(r.online for r in rs),
                    "max_players": max(maxes) if maxes else None,
                    "cpu": round(sum(cpus) / len(cpus), 2) if cpus else None,
                    "mem": int(sum(mems) / len(mems)) if mems else None,
                    "mem_limit": max(limits) if limits else None})
    return out


async def compact(now: dt.datetime | None = None) -> dict:
    """Roll one-minute rows older than KEEP_RAW into ROLLED-minute rows and
    delete anything older than KEEP_ALL. Only whole buckets are taken (the
    cutoff is on a bucket edge), so a bucket is never split between two
    passes, and running it twice changes nothing."""
    now = now or db_now()
    cutoff = _bucket(now - KEEP_RAW, ROLLED)
    rolled = 0
    while True:
        async with db.session() as s:
            raw = (await s.execute(
                select(ServerStat).where(ServerStat.res == 1, ServerStat.at < cutoff)
                .order_by(ServerStat.at).limit(_BATCH))).scalars().all()
        if not raw:
            break
        # A batch may end mid-bucket; leave that bucket for the next pass so
        # it is rolled from all its rows at once.
        if len(raw) == _BATCH:
            edge = _bucket(_aware(raw[-1].at), ROLLED)
            whole = [r for r in raw if _aware(r.at) < edge]
            if not whole:
                # One bucket holds more than a batch (a very large fleet):
                # take that bucket whole, however many rows it is.
                edge += dt.timedelta(minutes=ROLLED)
                async with db.session() as s:
                    whole = (await s.execute(
                        select(ServerStat).where(ServerStat.res == 1, ServerStat.at < edge)
                    )).scalars().all()
        else:
            whole, edge = raw, None
        await _insert(_roll(whole, ROLLED))
        ids = [r.id for r in whole]
        async with db.session() as s:
            for i in range(0, len(ids), _CHUNK):
                await s.execute(delete(ServerStat).where(ServerStat.id.in_(ids[i:i + _CHUNK])))
        rolled += len(whole)
        if edge is None:
            break
    async with db.session() as s:
        gone = (await s.execute(delete(ServerStat).where(
            ServerStat.at < now - KEEP_ALL))).rowcount or 0
    return {"rolled": rolled, "deleted": gone}


async def _rows(server_ids: list[str], since: dt.datetime) -> list[Any]:
    if not server_ids:
        return []
    async with db.session() as s:
        return list((await s.execute(
            select(ServerStat).where(and_(ServerStat.server_id.in_(server_ids),
                                          ServerStat.at >= since))
            .order_by(ServerStat.at))).scalars().all())


def _step(hours: int) -> int:
    """Minutes per chart point: one-minute readings for short windows,
    coarser as the window grows, never more than MAX_POINTS points."""
    want = hours * 60 / MAX_POINTS
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 240):
        if step >= want:
            return step
    return 240


def _hours(hours: Any) -> int:
    try:
        return max(1, min(MAX_HOURS, int(hours)))
    except (TypeError, ValueError):
        return 24


async def for_server(server_id: str, hours: Any = 24,
                     now: dt.datetime | None = None) -> dict:
    """The chart and its summary for one server over the last `hours`."""
    hours = _hours(hours)
    now = now or db_now()
    rows = await _rows([server_id], now - dt.timedelta(hours=hours))
    step = _step(hours)
    if hours > KEEP_RAW.total_seconds() / 3600:
        step = max(step, ROLLED)        # the older rows are ROLLED minutes each
    points = []
    for p in sorted(_roll(rows, step), key=lambda p: p["at"]):
        points.append({"t": db.epoch(p["at"]), "online": p["online"],
                       "max_players": p["max_players"], "cpu": p["cpu"], "mem": p["mem"],
                       "mem_limit": p["mem_limit"],
                       "up": round(p["up"] / p["samples"], 3) if p["samples"] else 0})
    samples = sum(r.samples for r in rows)
    up = sum(r.up for r in rows)
    cpus = [(r.cpu, r.up) for r in rows if r.cpu is not None and r.up]
    mems = [(r.mem, r.up) for r in rows if r.mem and r.up]
    return {
        "server_id": server_id, "hours": hours, "step": step, "points": points,
        "summary": {
            "samples": samples,
            "uptime": round(up / samples * 100, 1) if samples else None,
            "peak_online": max((r.online for r in rows), default=0),
            "avg_cpu": round(sum(c * n for c, n in cpus) / sum(n for _, n in cpus), 1)
                       if cpus else None,
            "peak_cpu": max((r.cpu for r in rows if r.cpu is not None), default=None),
            "avg_mem": int(sum(m * n for m, n in mems) / sum(n for _, n in mems))
                       if mems else None,
            "peak_mem": max((r.mem for r in rows if r.mem), default=None),
        },
    }


async def fleet(server_ids: list[str], hours: Any = 24,
                now: dt.datetime | None = None) -> dict:
    """Players and running servers across the given servers over time: the
    per-server peak in each step, summed."""
    hours = _hours(hours)
    now = now or db_now()
    rows = await _rows(server_ids, now - dt.timedelta(hours=hours))
    step = _step(hours)
    if hours > KEEP_RAW.total_seconds() / 3600:
        step = max(step, ROLLED)
    totals: dict[dt.datetime, dict] = {}
    for p in _roll(rows, step):
        t = totals.setdefault(p["at"], {"online": 0, "running": 0, "servers": 0})
        t["online"] += p["online"]
        t["running"] += 1 if p["up"] else 0
        t["servers"] += 1
    points = [{"t": db.epoch(at), **v} for at, v in sorted(totals.items())]
    return {"hours": hours, "step": step, "points": points,
            "peak_online": max((p["online"] for p in points), default=0)}


async def run_forever() -> None:
    """Record once a minute, on the minute; roll up once an hour. One loop
    per process that runs it (the worker with Redis, the panel without);
    several are harmless, the unique key keeps one row per minute."""
    await asyncio.sleep(10)
    last_compact = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            await record()
            if loop.time() - last_compact >= COMPACT_EVERY:
                last_compact = loop.time()
                await compact()
        except asyncio.CancelledError:
            raise
        except Exception as e:      # noqa: BLE001 -- next minute tries again
            log.warning("stats history: %s", e)
        now = db_now()
        await asyncio.sleep(EVERY - now.second - now.microsecond / 1e6 + 1)
