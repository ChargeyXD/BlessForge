#!/usr/bin/env python3
"""Stats history (app/history.py): recording, the roll-up, and the charts.

    python dev/tools/test_history.py
    TEST_DATABASE_URL=postgresql+asyncpg://... python dev/tools/test_history.py

Offline: a fresh database (SQLite, or TEST_DATABASE_URL) and the in-memory
engine. Checks that a minute is recorded once however often it is asked for,
that an unreachable node leaves a gap rather than zeros, that the roll-up
keeps uptime and the player peak exact and never splits a bucket, and that
a chart of any window stays a chart-sized number of points.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev" / "tools"))

import _dbtest  # noqa: E402 -- a temp STATE_DIR and database

from sqlalchemy import func, select  # noqa: E402

from app import db, engine, history  # noqa: E402
from app.db.models import Server, ServerStat  # noqa: E402
from app.engine.mock import MockEngine, _stopped  # noqa: E402

results: list[bool] = []
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 14, 12, 0, 30, tzinfo=UTC)
GB = 1024 ** 3


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {extra}" if extra else ""),
          flush=True)


async def rows(**where) -> list[ServerStat]:
    async with db.session() as s:
        q = select(ServerStat).order_by(ServerStat.at)
        for k, v in where.items():
            q = q.where(getattr(ServerStat, k) == v)
        return list((await s.execute(q)).scalars().all())


async def wipe() -> None:
    from sqlalchemy import delete
    async with db.session() as s:
        await s.execute(delete(ServerStat))


def synthetic(sid: str, start: dt.datetime, minutes: int) -> list[dict]:
    """One reading a minute: running for 50 minutes of every hour, players
    rising to a peak of minute-of-hour // 5."""
    out = []
    for i in range(minutes):
        at = start + dt.timedelta(minutes=i)
        up = at.minute < 50
        out.append({"server_id": sid, "at": at, "res": 1, "samples": 1, "up": int(up),
                    "online": at.minute // 5 if up else 0, "max_players": 20 if up else None,
                    "cpu": 30.0 if up else None, "mem": 2 * GB if up else None,
                    "mem_limit": 4 * GB})
    return out


async def recording(mock: MockEngine, a: str, b: str, ghost: str):
    mock.stats[a] = _stopped(running=True, online=3, max=20, cpu=41.5, mem=3 * GB)
    mock.stats[a]["mem_limit"] = 6 * GB
    mock.stats[b] = _stopped()
    n = await history.record(NOW)
    got = {r.server_id: r for r in await rows()}
    check("a reading of every reachable server, on the minute",
          n == 2 and set(got) == {a, b}
          and all(history._aware(r.at) == NOW.replace(second=0) for r in got.values()),
          (n, list(got)))
    check("...a running one with its players, CPU and memory",
          got[a].up == 1 and got[a].online == 3 and got[a].cpu == 41.5
          and got[a].mem == 3 * GB and got[a].mem_limit == 6 * GB, vars(got[a]))
    check("...a stopped one as down, with no CPU or memory to average in",
          got[b].up == 0 and got[b].cpu is None and got[b].mem is None, vars(got[b]))
    check("...and the server the engine does not know is a gap, not a zero",
          ghost not in got)

    again = await history.record(NOW + dt.timedelta(seconds=20))
    check("the same minute recorded again (another worker) is still one row",
          again == 2 and len(await rows()) == 2, len(await rows()))

    await asyncio.gather(*(history.record(NOW + dt.timedelta(minutes=1)) for _ in range(4)))
    check("...and four loops racing for the next minute write it once",
          len(await rows()) == 4, len(await rows()))

    mock.stats[a]["node_down"] = True
    await history.record(NOW + dt.timedelta(minutes=2))
    third = [r.server_id for r in await rows()
             if history._aware(r.at) == (NOW + dt.timedelta(minutes=2)).replace(second=0)]
    check("a node that is not answering is skipped, not recorded as its old reading",
          third == [b], third)
    mock.stats[a].pop("node_down")
    await wipe()


async def rollup(a: str, b: str):
    start = (NOW - dt.timedelta(hours=72)).replace(minute=0, second=0)
    raw = synthetic(a, start, 72 * 60) + synthetic(b, start, 72 * 60)
    await history._insert(raw)
    before_up = sum(r["up"] for r in raw if r["server_id"] == a)
    out = await history.compact(NOW)
    cutoff = history._bucket(NOW - history.KEEP_RAW, history.ROLLED)
    old_raw = [r for r in await rows(res=1) if history._aware(r.at) < cutoff]
    rolled = await rows(res=history.ROLLED, server_id=a)
    check("one-minute rows past 48 hours are rolled into 15-minute rows",
          out["rolled"] > 0 and not old_raw and rolled, out)
    check("...each a whole bucket: 15 readings, on a quarter-hour",
          all(r.samples == 15 and history._aware(r.at).minute % 15 == 0 for r in rolled),
          [(r.at, r.samples) for r in rolled if r.samples != 15][:3])
    after_up = sum(r.up for r in await rows(server_id=a))
    check("...uptime stays exact: running minutes are counted, not rounded",
          after_up == before_up, (before_up, after_up))
    q45 = [r for r in rolled if history._aware(r.at).minute == 45]
    check("...the player peak is kept (not averaged away), CPU averaged over running",
          q45 and q45[0].online == 9 and q45[0].up == 5 and q45[0].cpu == 30.0,
          q45 and vars(q45[0]))
    again = await history.compact(NOW)
    check("rolling up twice changes nothing", again == {"rolled": 0, "deleted": 0}, again)

    # A small batch: a pass must stop on a bucket edge, never split one.
    await wipe()
    await history._insert(synthetic(a, start, 72 * 60))
    real = history._BATCH
    history._BATCH = 37
    try:
        await history.compact(NOW)
    finally:
        history._BATCH = real
    rolled = await rows(res=history.ROLLED)
    check("rolled up in small passes, every bucket still holds all 15 readings",
          rolled and all(r.samples == 15 for r in rolled),
          [(r.at, r.samples) for r in rolled if r.samples != 15][:3])

    await history._insert([{"server_id": a, "at": NOW - dt.timedelta(days=31), "res": 15,
                            "samples": 15, "up": 15, "online": 1, "max_players": 20,
                            "cpu": 1.0, "mem": GB, "mem_limit": GB}])
    out = await history.compact(NOW)
    check("anything older than 30 days is deleted", out["deleted"] == 1, out)


async def charts(a: str, b: str):
    day = await history.for_server(a, 24, now=NOW)
    pts = day["points"]
    check("a day's chart: 5-minute points, never more than a chart's worth",
          day["step"] == 5 and 0 < len(pts) <= history.MAX_POINTS, (day["step"], len(pts)))
    s = day["summary"]
    check("...uptime from the minutes it was running (50 of every 60)",
          s["uptime"] is not None and abs(s["uptime"] - 83.3) < 1.0, s)
    check("...the peak players, average CPU and memory of the window",
          s["peak_online"] == 9 and s["avg_cpu"] == 30.0 and s["avg_mem"] == 2 * GB, s)
    month = await history.for_server(a, 24 * 30, now=NOW)
    check("a month's chart reads the rolled rows too, still chart-sized",
          month["step"] >= history.ROLLED and 0 < len(month["points"]) <= history.MAX_POINTS
          and month["summary"]["samples"] > day["summary"]["samples"],
          (month["step"], len(month["points"]), month["summary"]["samples"]))
    silly = await history.for_server(a, "a lot", now=NOW)
    huge = await history.for_server(a, 10 ** 6, now=NOW)
    check("a nonsense window is a day; a huge one is 30 days",
          silly["hours"] == 24 and huge["hours"] == history.MAX_HOURS)
    both = await history.fleet([a, b], 24, now=NOW)
    one = await history.fleet([a], 24, now=NOW)
    check("the fleet chart sums players across the servers asked for",
          both["peak_online"] == 2 * one["peak_online"] == 18
          and max(p["running"] for p in both["points"]) == 2, (both["peak_online"],
                                                               one["peak_online"]))
    none = await history.fleet([], 24, now=NOW)
    check("...and a scoped user with no servers gets an empty chart",
          none["points"] == [] and none["peak_online"] == 0)


async def deleting(a: str):
    async with db.session() as s:
        n = (await s.execute(select(func.count()).select_from(ServerStat)
                             .where(ServerStat.server_id == a))).scalar()
        await s.delete(await s.get(Server, a))
    async with db.session() as s:
        left = (await s.execute(select(func.count()).select_from(ServerStat)
                                .where(ServerStat.server_id == a))).scalar()
    check("deleting a server deletes its history with it", n and left == 0, (n, left))


async def main():
    await _dbtest.fresh()
    mock = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": mock})
    a = await engine.create_server(name="Alpha", port=25801, memory_mb=4096)
    b = await engine.create_server(name="Beta", port=25802, memory_mb=4096)
    ghost = "ghost-not-on-any-node"
    async with db.session() as s:
        for sid, name in ((a, "Alpha"), (b, "Beta"), (ghost, "Ghost")):
            s.add(Server(uuid=sid, name=name, port=25800))
    await recording(mock, a, b, ghost)
    await rollup(a, b)
    await wipe()
    start = (NOW - dt.timedelta(hours=72)).replace(minute=0, second=0)
    await history._insert(synthetic(a, start, 72 * 60) + synthetic(b, start, 72 * 60))
    await history.compact(NOW)
    await charts(a, b)
    await deleting(a)
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed ({_dbtest.label()})")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
