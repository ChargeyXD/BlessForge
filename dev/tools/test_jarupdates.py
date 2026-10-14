#!/usr/bin/env python3
"""Server-jar updates (app/jarupdates.py) for Paper, Purpur and Folia.

    python dev/tools/test_jarupdates.py

Offline: a fresh database, the in-memory engine, and the loader index
replaced by a fixed answer. Checks that an update backs the world up first,
stops and restarts a running server, keeps every JVM flag the owner set,
removes the old jar, and that a failed backup changes nothing.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev" / "tools"))

import _dbtest  # noqa: E402 -- a temp STATE_DIR and database

from app import db, engine, jarupdates, loaders, schedules, templates, worldbackups  # noqa: E402
from app.db.models import Schedule, Server  # noqa: E402
from app.engine.mock import MockEngine  # noqa: E402

results: list[bool] = []
OLD = "paper-1.21.1-100.jar"
NEW = "paper-1.21.1-120.jar"
CMD = f"java -Xms2G -Xmx6G -XX:+UseG1GC -XX:MaxGCPauseMillis=130 -jar {OLD} nogui"
LATEST = {"build": "120"}


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {extra}" if extra else ""),
          flush=True)


async def fake_resolve(family, mc, build=None):
    if family not in ("paper", "purpur", "folia"):
        return {}
    b = LATEST["build"]
    return {"url": f"https://fill.example/{family}-{mc}-{b}.jar",
            "filename": f"{family}-{mc}-{b}.jar", "kind": "server",
            "loader_version": b, "source": "PaperMC"}


async def fake_java(_key, _mc):
    return 21


def pure():
    check("newer builds compare as numbers, an unknown build is worth updating",
          jarupdates.newer("120", "100") and not jarupdates.newer("99", "100")
          and jarupdates.newer("5", "") and not jarupdates.newer("", "1"))
    check("the build is read off the jar name",
          jarupdates.build_of(OLD) == "100" and jarupdates.build_of("server.jar") == "")
    cmd = jarupdates._command_for(CMD, OLD, NEW, "paper")
    check("the new command is the old one with the jar swapped, every flag kept",
          cmd == CMD.replace(OLD, NEW), cmd)
    cmd = jarupdates._command_for("java -Xms3G -Xmx9G -jar server.jar", "elsewhere.jar",
                                  NEW, "paper")
    check("...and when the command does not name the jar, the heap is still kept",
          "-Xmx9G" in cmd and NEW in cmd, cmd)


async def main():
    pure()
    await _dbtest.fresh()
    loaders.resolve = fake_resolve
    templates.java_major = fake_java
    mock = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": mock})

    sid = await engine.create_server(name="Lobby", port=25810, memory_mb=4096,
                                     mc_version="1.21.1", loader="paper")
    fab = await engine.create_server(name="Modded", port=25811, memory_mb=4096,
                                     mc_version="1.21.1", loader="fabric")
    async with db.session() as s:
        s.add(Server(uuid=sid, name="Lobby", port=25810))
        s.add(Server(uuid=fab, name="Modded", port=25811))
    await engine.write_manifest(sid, {"schema": 1, "complete": True, "loader": "paper",
                                      "minecraft": "1.21.1", "loader_version": "100"})
    await engine.write_manifest(fab, {"schema": 1, "complete": True, "loader": "fabric",
                                      "minecraft": "1.21.1", "loader_version": "0.16.9"})
    await engine.update_server(sid, {"executable": OLD, "execution_command": CMD})
    await engine.update_server(fab, {"executable": "fabric-server-launch.jar",
                                     "execution_command": "java -jar fabric-server-launch.jar"})
    await engine.upload_file(sid, ".", OLD, b"old jar")
    await engine.upload_file(sid, "world", "level.dat", b"the world")

    info = await jarupdates.check(sid)
    check("a Paper server on build 100 sees build 120 for its own Minecraft version",
          info["supported"] and info["current"] == "100" and info["latest"] == "120"
          and info["update_available"], info)
    other = await jarupdates.check(fab)
    check("a Fabric server is not offered a jar update (mods have their own)",
          not other["supported"] and other["reason"], other)
    try:
        await jarupdates.apply(fab)
        refused = False
    except jarupdates.JarUpdateError:
        refused = True
    check("...and applying one there is refused", refused)

    await engine.power(sid, "start")
    out = await jarupdates.apply(sid)
    srv = await engine.get_server(sid)
    files = await engine.list_dir(sid, ".")
    names = list(files)                     # name -> entry
    check("the update ran: 100 -> 120, and the server was started again",
          out["updated"] and out["from"] == "100" and out["to"] == "120" and out["restarted"]
          and (await engine.get_stats(sid))["running"], out)
    check("the startup command names the new jar and keeps every flag",
          srv["executable"] == NEW and srv["execution_command"] == CMD.replace(OLD, NEW),
          srv["execution_command"])
    check("the new jar came from the project, and the old one is gone",
          any(u.endswith(NEW) for u in mock.fetched) and NEW in str(names)
          and OLD not in str(names), names)
    manifest = await engine.read_manifest(sid)
    snaps = (await worldbackups.listing(sid))["snapshots"]
    check("the manifest records build 120, and the world was backed up first",
          manifest["loader_version"] == "120" and out["backup"]
          and any(s["reason"].startswith("Before updating paper 100") for s in snaps),
          [s["reason"] for s in snaps])
    again = await jarupdates.apply(sid)
    check("on the newest build, an update is a no-op", again == {
        "updated": False, "current": "120", "latest": "120"}, again)

    LATEST["build"] = "121"
    real = mock.world_backup_start

    async def failing(server_id, opts):
        raise RuntimeError("disk full")
    mock.world_backup_start = failing
    try:
        await jarupdates.apply(sid)
        stopped_it = False
    except worldbackups.BackupFailed:
        stopped_it = True
    mock.world_backup_start = real
    srv = await engine.get_server(sid)
    check("a failed backup stops the update with nothing changed, the server still up",
          stopped_it and srv["executable"] == NEW
          and (await engine.get_stats(sid))["running"], srv["executable"])

    row = await schedules.create(sid, action="update_jar", cron="0 5 * * *",
                                 payload={"name": "Nightly jar update", "tz": "UTC"})
    async with db.session() as s:
        task = await s.get(Schedule, row["id"])
    ran = await schedules.run_task(task)
    srv = await engine.get_server(sid)
    check("a scheduled update_jar task follows new builds",
          ran["ok"] and ran["result"].startswith("updated 120 -> 121")
          and srv["executable"] == "paper-1.21.1-121.jar", ran)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed ({_dbtest.label()})")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
