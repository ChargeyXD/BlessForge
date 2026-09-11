#!/usr/bin/env python3
"""Racks, and the record of when each server was last up.

The two things worth testing here are the ones with a wrong answer that
looks right:

  1. **An uptime may only be claimed for a witnessed down -> up edge.**
     Finding a server already running on the very first poll says nothing
     about when it started. Claiming one anyway produces a number the user
     can disprove in one glance at the console, which is worse than saying
     nothing.

  2. **An unreachable Crafty must not look like an empty fleet.** The rack
     document filters assignments against the servers that currently
     exist. If "Crafty did not answer" arrived as an empty set, every
     assignment would vanish and the layout would read as wiped.

Offline. No network, no Docker, no backend. Writes into a temp directory.

    python3 dev/tools/test_fleet_state.py
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Point state at a scratch directory BEFORE app.config is imported, so the
# real /data is never touched by a test run.
_TMP = tempfile.mkdtemp(prefix="bf-fleet-")
os.environ["STATE_DIR"] = _TMP
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("CACHE_DIR", str(pathlib.Path(_TMP) / "cache"))

from app import fleetgroups as fg  # noqa: E402

out = []


def check(name, cond, extra=""):
    out.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


def fresh():
    fg.reset_for_tests()
    for leftover in pathlib.Path(_TMP).glob("fleet-state*"):
        leftover.unlink()


# --- 1. when did it last run -------------------------------------------
fresh()
first = fg.observe([{"server_id": "s1", "name": "A", "running": True}], full=True)
rec = first["servers"]["s1"]
check("an already-running server on the first poll gets no claimed uptime",
      rec["last_started_precision"] == "unknown", rec["last_started_precision"])
check("...but it is still recorded as running now",
      rec["running"] is True and "last_running_at" in rec)
check("first_seen is set on the first sighting", "first_seen" in rec)

fg.observe([{"server_id": "s1", "running": False}], full=True)
up = fg.observe([{"server_id": "s1", "running": True}], full=True)
check("a down -> up transition IS a witnessed edge",
      up["servers"]["s1"]["last_started_precision"] == "observed")

started = up["servers"]["s1"]["last_started"]
again = fg.observe([{"server_id": "s1", "running": True}], full=True)
check("staying up does not restart the clock",
      again["servers"]["s1"]["last_started"] == started)

fresh()
fg.observe([{"server_id": "s1", "running": False}], full=True)
down = fg.observe([{"server_id": "s1", "running": False}], full=True)
check("a server never seen up has no last_running_at",
      "last_running_at" not in down["servers"]["s1"])

# A full sweep is authoritative; a partial one says nothing about absentees.
fresh()
fg.observe([{"server_id": "s1", "running": False},
            {"server_id": "s2", "running": False}], full=True)
partial = fg.observe([{"server_id": "s1", "running": True}], full=False)
check("a partial sweep does not forget the servers it did not mention",
      "s2" in partial["servers"])
swept = fg.observe([{"server_id": "s1", "running": True}], full=True)
check("a full sweep forgets a server that is no longer there",
      "s2" not in swept["servers"])

# --- 2. racks -----------------------------------------------------------
fresh()
doc = fg.save_group(None, "Survival", "torii", {"s1", "s2"})
check("a new rack gets a slug id", doc["group"]["id"] == "survival",
      doc["group"]["id"])
check("its motif is kept", doc["group"]["motif"] == "torii")

same = fg.save_group(None, "Survival", "sakura", {"s1"})
check("a second rack with the same name does not collide",
      same["group"]["id"] == "survival-2", same["group"]["id"])

junk = fg.save_group(None, "Weird", "'; DROP TABLE --", {"s1"})
check("an unknown motif falls back rather than reaching the DOM",
      junk["group"]["motif"] in fg.MOTIFS, junk["group"]["motif"])

blank = None
try:
    fg.save_group(None, "   ", "sakura", set())
except ValueError:
    blank = "refused"
check("a rack with no name is refused", blank == "refused")

doc = fg.assign(["s1", "s2"], "survival", {"s1", "s2"})
group = next(g for g in doc["groups"] if g["id"] == "survival")
check("servers hang on the rack they were assigned to",
      group["server_ids"] == ["s1", "s2"], str(group["server_ids"]))

doc = fg.assign(["s1"], None, {"s1", "s2"})
group = next(g for g in doc["groups"] if g["id"] == "survival")
check("assigning to no rack puts the server back in the yard",
      group["server_ids"] == ["s2"], str(group["server_ids"]))

missing = None
try:
    fg.assign(["s2"], "no-such-rack", {"s2"})
except ValueError:
    missing = "refused"
check("hanging on a rack that is gone is refused", missing == "refused")

# --- 3. a fleet that cannot be read is not an empty fleet ---------------
gone = fg.document(set())
check("a server that no longer exists is dropped from the answer",
      gone["groups"][0]["server_ids"] == [], str(gone["groups"][0]["server_ids"]))

unknown = fg.document(None)
check("...but `Crafty did not answer` (None) leaves the layout alone",
      unknown["groups"][0]["server_ids"] == ["s2"],
      str(unknown["groups"][0]["server_ids"]))

# The assignment must still be in STORAGE after being filtered out of an
# answer, or one unreachable poll would quietly destroy the layout.
back = fg.document({"s2"})
check("filtering an answer does not delete the stored assignment",
      back["groups"][0]["server_ids"] == ["s2"])

# --- 4. deleting a rack ------------------------------------------------
doc = fg.delete_group("survival", {"s2"})
check("a deleted rack is gone", not any(g["id"] == "survival"
                                        for g in doc["groups"]))
check("and its members are back in the yard, not orphaned",
      "s2" not in doc["assign"], str(doc["assign"]))

# --- 5. it survives a restart ------------------------------------------
fg.save_group(None, "Creative", "tsuki", {"s9"})
fg.assign(["s9"], "creative", {"s9"})
fg.reset_for_tests()                       # simulates a fresh process
reloaded = fg.document({"s9"})
check("racks are still there after a restart",
      any(g["id"] == "creative" for g in reloaded["groups"]))
check("and so are their assignments",
      reloaded["assign"].get("s9") == "creative", str(reloaded["assign"]))

# --- 6. a hand-edited state file cannot inject anything -----------------
fresh()
path = pathlib.Path(_TMP) / "fleet-state.json"
path.write_text('{"groups":[{"id":"x","name":"OK","motif":"<script>"},'
                '{"name":""},"nonsense"],"assign":{"s1":"x"},'
                '"activity":{"s1":{"first_seen":1}}}', encoding="utf-8")
fg.reset_for_tests()
loaded = fg.document({"s1"})
check("a junk motif in the file is normalised on load",
      loaded["groups"][0]["motif"] in fg.MOTIFS, loaded["groups"][0]["motif"])
check("unnamed and non-dict entries are dropped",
      len(loaded["groups"]) == 1, str(len(loaded["groups"])))

# --- 6b. a torn write is not data ---------------------------------------
# This is not hypothetical: a hard restart on 2026-09-11 left this exact
# file as 559 NUL bytes. The length was recorded, the bytes never landed.
# Before config.write_state the save was atomic but not durable, so the
# rename could reach the disk ahead of the contents.
fresh()
fg.save_group(None, "Before The Power Cut", "sakura", set())
torn = pathlib.Path(_TMP) / "fleet-state.json"
size = torn.stat().st_size
torn.write_bytes(bytes(size))   # bytes(n) is n NULs, no escape
fg.reset_for_tests()
recovered = fg.document(None)
check("a file of NUL bytes is treated as absent, not as a crash",
      recovered["groups"] == [], str(recovered["groups"]))
check("...and the app keeps working afterwards",
      fg.save_group(None, "After", "sakura", set())["group"] is not None)

# The fix, from the other side: what write_state puts down must survive
# being read back by a fresh process with no in-memory state.
fresh()
fg.save_group(None, "Durable", "matsu", set())
fg.reset_for_tests()
check("a normally-saved rack reads back after a cold start",
      any(g["name"] == "Durable" for g in fg.document(None)["groups"]))

# --- 7. an unwritable /data degrades instead of failing -----------------
fresh()
fg.save_group(None, "Before", "sakura", set())
real = fg._save


def boom():
    fg._persisted = False
    return False


fg._save = boom
try:
    degraded = fg.save_group(None, "During An Outage", "sakura", set())
    check("a rack can still be made when /data cannot be written",
          degraded["group"] is not None)
    check("...and the answer says so, so the UI can warn once",
          degraded["persisted"] is False, str(degraded["persisted"]))
finally:
    fg._save = real

passed = sum(out)
print(f"\n{passed}/{len(out)} checks passed")
sys.exit(0 if passed == len(out) else 1)
