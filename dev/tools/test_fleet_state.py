#!/usr/bin/env python3
"""Racks, and the record of when each server was last up.

The two things worth testing here are the ones with a wrong answer that
looks right:

  1. **An uptime may only be claimed for a witnessed down -> up edge.**
     Finding a server already running on the very first poll says nothing
     about when it started. Claiming one anyway produces a number the user
     can disprove in one glance at the console, which is worse than saying
     nothing.

  2. **An unreachable node must not look like an empty fleet.** The rack
     document filters assignments against the servers that currently
     exist. If "the node did not answer" arrived as an empty set, every
     assignment would vanish and the layout would read as wiped.

The state lives in the database now (racks, rack_members, server_activity)
and survives a restart; an old fleet-state.json -- including the torn,
all-NUL one a power cut once produced -- is carried across by the importer.

Offline, on SQLite or on TEST_DATABASE_URL (see dev/tools/_dbtest.py).
"""
import asyncio
import json
import sys

import _dbtest  # noqa: F401 -- sets STATE_DIR before app is imported

from app import config, db  # noqa: E402
from app import fleetgroups as fg  # noqa: E402
from app.db import importer  # noqa: E402

out = []


def check(name, cond, extra=""):
    out.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


async def fresh() -> str:
    url = await _dbtest.fresh()
    fg.reset_for_tests()
    await fg.load()
    return url


async def restarted(url: str) -> None:
    """A fresh process: nothing in memory, the database read back."""
    await _dbtest.restart(url)
    fg.reset_for_tests()
    await fg.load()


async def main():
    print(f"(database: {_dbtest.label()})")

    # --- 1. when did it last run -------------------------------------------
    await fresh()
    first = await fg.observe([{"server_id": "s1", "name": "A", "running": True}], full=True)
    rec = first["servers"]["s1"]
    check("an already-running server on the first poll gets no claimed uptime",
          rec["last_started_precision"] == "unknown", rec["last_started_precision"])
    check("...but it is still recorded as running now",
          rec["running"] is True and "last_running_at" in rec)
    check("first_seen is set on the first sighting", "first_seen" in rec)

    await fg.observe([{"server_id": "s1", "running": False}], full=True)
    up = await fg.observe([{"server_id": "s1", "running": True}], full=True)
    check("a down -> up transition IS a witnessed edge",
          up["servers"]["s1"]["last_started_precision"] == "observed")
    started = up["servers"]["s1"]["last_started"]
    again = await fg.observe([{"server_id": "s1", "running": True}], full=True)
    check("staying up does not restart the clock",
          again["servers"]["s1"]["last_started"] == started)

    await fresh()
    await fg.observe([{"server_id": "s1", "running": False}], full=True)
    down = await fg.observe([{"server_id": "s1", "running": False}], full=True)
    check("a server never seen up has no last_running_at",
          "last_running_at" not in down["servers"]["s1"])

    await fresh()
    await fg.observe([{"server_id": "s1", "running": False},
                      {"server_id": "s2", "running": False}], full=True)
    partial = await fg.observe([{"server_id": "s1", "running": True}], full=False)
    check("a partial sweep does not forget the servers it did not mention",
          "s2" in partial["servers"])
    swept = await fg.observe([{"server_id": "s1", "running": True}], full=True)
    check("a full sweep forgets a server that is no longer there",
          "s2" not in swept["servers"])

    # --- 2. racks -----------------------------------------------------------
    url = await fresh()
    doc = await fg.save_group(None, "Survival", "torii", {"s1", "s2"})
    check("a new rack gets a slug id", doc["group"]["id"] == "survival", doc["group"]["id"])
    check("its motif is kept", doc["group"]["motif"] == "torii")
    same = await fg.save_group(None, "Survival", "sakura", {"s1"})
    check("a second rack with the same name does not collide",
          same["group"]["id"] == "survival-2", same["group"]["id"])
    junk = await fg.save_group(None, "Weird", "'; DROP TABLE --", {"s1"})
    check("an unknown motif falls back rather than reaching the DOM",
          junk["group"]["motif"] in fg.MOTIFS, junk["group"]["motif"])
    blank = None
    try:
        await fg.save_group(None, "   ", "sakura", set())
    except ValueError:
        blank = "refused"
    check("a rack with no name is refused", blank == "refused")

    doc = await fg.assign(["s1", "s2"], "survival", {"s1", "s2"})
    group = next(g for g in doc["groups"] if g["id"] == "survival")
    check("servers hang on the rack they were assigned to",
          group["server_ids"] == ["s1", "s2"], str(group["server_ids"]))
    doc = await fg.assign(["s1"], None, {"s1", "s2"})
    group = next(g for g in doc["groups"] if g["id"] == "survival")
    check("assigning to no rack puts the server back in the yard",
          group["server_ids"] == ["s2"], str(group["server_ids"]))
    missing = None
    try:
        await fg.assign(["s2"], "no-such-rack", {"s2"})
    except ValueError:
        missing = "refused"
    check("hanging on a rack that is gone is refused", missing == "refused")

    # --- 3. a fleet that cannot be read is not an empty fleet ---------------
    gone = fg.document(set())
    check("a server that no longer exists is dropped from the answer",
          gone["groups"][0]["server_ids"] == [], str(gone["groups"][0]["server_ids"]))
    unknown = fg.document(None)
    check("...but `the node did not answer` (None) leaves the layout alone",
          unknown["groups"][0]["server_ids"] == ["s2"],
          str(unknown["groups"][0]["server_ids"]))
    back = fg.document({"s2"})
    check("filtering an answer does not delete the stored assignment",
          back["groups"][0]["server_ids"] == ["s2"])

    # --- 4. deleting a rack ------------------------------------------------
    doc = await fg.delete_group("survival", {"s2"})
    check("a deleted rack is gone", not any(g["id"] == "survival" for g in doc["groups"]))
    check("and its members are back in the yard, not orphaned",
          "s2" not in doc["assign"], str(doc["assign"]))

    # --- 5. it survives a restart ------------------------------------------
    await fg.save_group(None, "Creative", "tsuki", {"s9"})
    await fg.assign(["s9"], "creative", {"s9"})
    await fg.observe([{"server_id": "s9", "running": True}], full=False)
    await restarted(url)
    reloaded = fg.document({"s9"})
    check("racks are still there after a restart",
          any(g["id"] == "creative" for g in reloaded["groups"]))
    check("and so are their assignments",
          reloaded["assign"].get("s9") == "creative", str(reloaded["assign"]))
    check("and the order they were hung in",
          [g["id"] for g in reloaded["groups"]][-1] == "creative")
    activity = await fg.observe([], full=False)
    check("and the activity record", "s9" in activity["servers"])

    # --- 6. an old state file comes across, sanitised ----------------------
    url = await fresh()
    path = config.state_path(fg.STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"groups":[{"id":"x","name":"OK","motif":"<script>"},'
                    '{"name":""},"nonsense"],"assign":{"s1":"x","s2":"gone"},'
                    '"activity":{"s1":{"first_seen":1}}}', encoding="utf-8")
    result = await importer.run()
    await fg.load()
    loaded = fg.document({"s1", "s2"})
    check("an old fleet-state.json is imported", result.get("imported") is True, result)
    check("a junk motif in the file is normalised on the way in",
          loaded["groups"][0]["motif"] in fg.MOTIFS, loaded["groups"][0]["motif"])
    check("unnamed and non-dict entries are dropped",
          len(loaded["groups"]) == 1, str(len(loaded["groups"])))
    check("an assignment to a rack that does not exist is not carried",
          loaded["assign"] == {"s1": "x"}, loaded["assign"])
    check("the file is renamed so it cannot be imported twice",
          not path.exists() and path.with_name(path.name + ".imported").exists())

    # --- 6b. a torn file is not data ----------------------------------------
    # Not hypothetical: a hard restart on 2026-09-11 left this exact file as
    # 559 NUL bytes. The length was recorded, the bytes never landed.
    url = await fresh()
    path.write_bytes(bytes(559))    # bytes(n) is n NULs, no escape
    await importer.run()
    await fg.load()
    check("a file of NUL bytes is treated as absent, not as a crash",
          fg.document(None)["groups"] == [])
    check("...and the app keeps working afterwards",
          (await fg.save_group(None, "After", "sakura", set()))["group"] is not None)

    # --- 7. a database that cannot be written degrades instead of failing ----
    url = await fresh()
    await fg.save_group(None, "Before", "sakura", set())
    await db.dispose()
    db.configure("sqlite+aiosqlite:///" + str(_dbtest.TMP / "no" / "such" / "x.db").replace("\\", "/"))
    real_init = db.init

    async def broken_init(*a, **k):
        raise RuntimeError("the database is down")

    db.init = broken_init
    try:
        degraded = await fg.save_group(None, "During An Outage", "sakura", set())
        check("a rack can still be made when the database cannot be written",
              degraded["group"] is not None)
        check("...and the answer says so, so the UI can warn once",
              degraded["persisted"] is False, str(degraded["persisted"]))
    finally:
        db.init = real_init
        await db.dispose()

    # The torn-file guard still protects what remains on disk: caches.
    torn = _dbtest.TMP / "cache-probe.json"
    torn.write_bytes(bytes(64))
    check("config.read_state still reads a torn cache file as absent",
          config.read_state(torn) is None)
    check("and config.write_state still writes one that reads back",
          config.write_state(torn, json.dumps({"ok": 1}))
          and json.loads(config.read_state(torn)) == {"ok": 1})

    passed = sum(out)
    print(f"\n{passed}/{len(out)} checks passed")
    return passed == len(out)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
