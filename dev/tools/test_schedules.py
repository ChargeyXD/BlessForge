#!/usr/bin/env python3
"""Scheduled tasks (app/schedules.py): the clock, the table, and the runs.

    python dev/tools/test_schedules.py

Offline: a temporary SQLite database (or TEST_DATABASE_URL) and the
in-memory engine. Checks the cron maths, that a task fires once however many
loops race for it, that a chain runs its steps on their delays, and that a
run missed while BlessForge was down is skipped rather than fired late.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev" / "tools"))

import _dbtest  # noqa: E402,F401 -- a temp STATE_DIR and database

from app import db, engine, schedules  # noqa: E402
from app.db.models import Schedule, Server  # noqa: E402
from app.engine.mock import MockEngine  # noqa: E402

results: list[bool] = []
UTC = dt.timezone.utc


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {extra}" if extra else ""),
          flush=True)


def at(*a) -> dt.datetime:
    return dt.datetime(*a, tzinfo=UTC)


def cron_maths():
    nxt = schedules.next_after("0 5 * * *", at(2026, 10, 14, 4, 59), "UTC")
    check("0 5 * * * after 04:59 is 05:00 the same day", nxt == at(2026, 10, 14, 5, 0), nxt)
    nxt = schedules.next_after("0 5 * * *", at(2026, 10, 14, 5, 0), "UTC")
    check("...and strictly after: at 05:00 it is tomorrow's", nxt == at(2026, 10, 15, 5, 0),
          nxt)
    nxt = schedules.next_after("*/15 * * * *", at(2026, 10, 14, 4, 46), "UTC")
    check("*/15 steps through the hour", nxt == at(2026, 10, 14, 5, 0), nxt)
    nxt = schedules.next_after("30 2 * * mon-fri", at(2026, 10, 17, 0, 0), "UTC")  # a Saturday
    check("weekday names and ranges: Saturday's next weekday 02:30 is Monday",
          nxt == at(2026, 10, 19, 2, 30), nxt)
    nxt = schedules.next_after("0 0 1 * 1", at(2026, 10, 14, 0, 0), "UTC")
    check("both day fields set: either matches (the 1st, or a Monday), as cron does",
          nxt == at(2026, 10, 19, 0, 0), nxt)
    nxt = schedules.next_after("0 5 * * *", at(2026, 10, 14, 0, 0), "Asia/Kolkata")
    check("a task's own time zone: 05:00 in Kolkata is 23:30 UTC the day before",
          nxt == at(2026, 10, 14, 23, 30), nxt)
    nxt = schedules.next_after("@every 6h", at(2026, 10, 14, 12, 0), "UTC",
                               last_run=at(2026, 10, 14, 7, 0))
    check("@every keeps its phase from the last run (07:00 + 6 h = 13:00)",
          nxt == at(2026, 10, 14, 13, 0), nxt)
    check("@daily is midnight", schedules.next_after("@daily", at(2026, 10, 14, 3, 0), "UTC")
          == at(2026, 10, 15, 0, 0))
    bad = []
    for expr in ("61 * * * *", "* * *", "0 5 * * 8", "@every 0m", "five past", ""):
        try:
            schedules.parse(expr)
            bad.append(expr)
        except schedules.ScheduleError:
            pass
    check("nonsense is refused with a reason, never guessed", not bad, bad)
    check("upcoming() gives the next five times",
          len(schedules.upcoming("@hourly", "UTC")) == 5)


class Recorder(MockEngine):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.powers: list[tuple[str, str]] = []

    async def power(self, server_id, action):
        self.powers.append((server_id, action))
        await super().power(server_id, action)


async def make_server(mock) -> str:
    sid = await engine.create_server(name="Sched", port=25810, memory_mb=1024,
                                     mc_version="1.21.1", loader="paper")
    await engine.update_server(sid, {"execution_command": "java -jar paper.jar nogui"})
    async with db.session() as s:
        s.add(Server(uuid=sid, name="Sched", port=25810))
    return sid


async def table_and_runs():
    mock = Recorder(seed=False)
    engine.configure("mock", drivers={"local": mock})
    sid = await make_server(mock)

    row = await schedules.create(sid, action="say", cron="0 5 * * *",
                                 payload={"name": "Morning", "message": "Good morning",
                                          "tz": "UTC"})
    check("a task is saved with its next run", row["id"] and row["next_run"]
          and row["message"] == "Good morning", row)
    try:
        await schedules.create(sid, action="say", cron="0 5 * * *", payload={"message": ""})
        empty = False
    except schedules.ScheduleError:
        empty = True
    check("a say task with no message is refused", empty)
    upd = await schedules.update_task(sid, row["id"], {"cron": "15 6 * * *",
                                                       "payload": {"message": "Hi"}})
    check("a task can be changed; its next run moves with it",
          upd["cron"] == "15 6 * * *" and upd["message"] == "Hi"
          and upd["next_run"] != row["next_run"], upd)

    # --- it fires once, however many loops race for it --------------------------------
    await engine.power(sid, "start")
    past = dt.datetime.now(UTC) - dt.timedelta(seconds=30)
    async with db.session() as s:
        (await s.get(Schedule, row["id"])).next_run = past
    ran = await asyncio.gather(schedules.run_due(), schedules.run_due(), schedules.run_due())
    sent = [c for c in mock.commands if c[1] == "say Hi"]
    check("three loops at once: the task runs exactly once", len(sent) == 1
          and sum(len(r) for r in ran) == 1, (sent, ran))
    after = [t for t in await schedules.list_for(sid) if t["id"] == row["id"]][0]
    check("...records what happened, and is due again tomorrow",
          after["last_ok"] is True and "say Hi" in after["last_result"]
          and after["next_run"] > dt.datetime.now(UTC).timestamp(), after)

    # --- a run missed while BlessForge was down is skipped, not fired late -------------
    async with db.session() as s:
        (await s.get(Schedule, row["id"])).next_run = dt.datetime.now(UTC) - dt.timedelta(hours=3)
    mock.commands.clear()
    await schedules.run_due()
    after = [t for t in await schedules.list_for(sid) if t["id"] == row["id"]][0]
    check("a run three hours late is skipped and says why",
          not mock.commands and after["last_ok"] is False
          and after["last_result"].startswith("missed"), after.get("last_result"))

    # --- the daily-restart chain --------------------------------------------------------
    made = await schedules.preset(sid, "daily_restart", {"time": "05:00", "warn": [5, 1],
                                                         "tz": "UTC"})
    check("the daily-restart preset is three linked steps: 5-min warning, 1-min "
          "warning, restart",
          [m["action"] for m in made] == ["say", "say", "restart"]
          and made[0]["cron"] == "55 4 * * *" and made[1]["parent"] == made[0]["id"]
          and made[1]["delay"] == 240 and made[2]["parent"] == made[1]["id"]
          and made[2]["delay"] == 60, [(m["action"], m["cron"], m.get("delay")) for m in made])
    mock.commands.clear()
    mock.powers.clear()
    now = dt.datetime.now(UTC)
    async with db.session() as s:
        (await s.get(Schedule, made[0]["id"])).next_run = now - dt.timedelta(seconds=5)
    await schedules.run_due(now)
    step2 = [t for t in await schedules.list_for(sid) if t["id"] == made[1]["id"]][0]
    check("the first warning goes out, and the next step is due 4 minutes later",
          ("say Server restarts in 5 minutes." in [c[1] for c in mock.commands])
          and abs(step2["next_run"] - (now.timestamp() + 240)) < 2, step2["next_run"])
    await schedules.run_due(now + dt.timedelta(seconds=241))
    await schedules.run_due(now + dt.timedelta(seconds=302))
    check("...then the 1-minute warning, then the restart",
          "say Server restarts in 1 minute." in [c[1] for c in mock.commands]
          and (sid, "restart") in mock.powers, (mock.commands, mock.powers))
    left = {t["id"]: t["next_run"] for t in await schedules.list_for(sid)}
    check("chained steps wait for their parent again afterwards",
          left[made[1]["id"]] is None and left[made[2]["id"]] is None
          and left[made[0]["id"]] is not None, left)

    # --- what it will not do --------------------------------------------------------------
    await engine.power(sid, "stop")
    mock.powers.clear()
    out = await schedules.run_task(await _row(made[2]["id"]))
    check("a restart with the server stopped is skipped, not a start",
          out["ok"] and "skipped" in out["result"] and not mock.powers, out)
    once = await schedules.create(sid, action="start", cron="@hourly",
                                  payload={"one_time": True, "tz": "UTC"})
    async with db.session() as s:
        (await s.get(Schedule, once["id"])).next_run = dt.datetime.now(UTC) - dt.timedelta(seconds=5)
    await schedules.run_due()
    after = [t for t in await schedules.list_for(sid) if t["id"] == once["id"]][0]
    check("a one-time task runs once and switches itself off",
          after["enabled"] is False and after["next_run"] is None
          and (sid, "start") in mock.powers, after)

    gone = await schedules.remove(sid, made[0]["id"])
    check("deleting the first step deletes the chain after it",
          sorted(gone) == sorted(m["id"] for m in made), gone)


async def _row(schedule_id):
    async with db.session() as s:
        return await s.get(Schedule, schedule_id)


async def main():
    cron_maths()
    await db.init()
    await table_and_runs()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
