#!/usr/bin/env python3
"""World backups: the node's snapshot store, its lifecycle, and the panel.

    python dev/tools/test_worldbackup.py

Offline. The store is exercised on real folders in a temp directory
(agent/worldbackup.py); the agent's backup and restore flows on the process
runtime with a fake server; the panel side (app/worldbackups.py) on the
in-memory engine.
"""
from __future__ import annotations

import asyncio
import io
import os
import pathlib
import shutil
import sys
import tempfile
import time
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev" / "tools"))

import _dbtest  # noqa: E402,F401 -- a temp STATE_DIR and database

from agent import worldbackup  # noqa: E402
from agent.config import AgentConfig  # noqa: E402

results: list[bool] = []
TMP = pathlib.Path(tempfile.mkdtemp(prefix="bf-worldbackup-"))


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {extra}" if extra else ""),
          flush=True)


def write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def tree(root: pathlib.Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def store_tests():
    server = TMP / "servers" / "aaaaaaaa-0000-0000-0000-000000000001"
    write(server / "world" / "level.dat", b"level-1")
    write(server / "world" / "region" / "r.0.0.mca", os.urandom(300_000))
    write(server / "server.properties", b"motd=hi\n")
    write(server / "mods" / "create.jar", b"jar" * 1000)
    write(server / "logs" / "latest.log", b"noise")
    write(server / ".blessforge-tmp" / "jna.tmp", b"native")
    outside = TMP / "outside.txt"
    outside.write_bytes(b"SECRET outside the server")
    linked = False
    try:
        os.symlink(outside, server / "world" / "escape.txt")
        os.link(outside, server / "world" / "hardlink.txt")
        linked = True
    except (OSError, NotImplementedError):
        pass
    store = worldbackup.Store(TMP / "backups", server.name)

    first = store.take(server, reason="first")
    snap = store.manifest(first["id"])
    names = {e[0] for e in snap["files"]}
    check("a snapshot holds the server's files, not its logs or the JVM temp folder",
          {"world/level.dat", "world/region/r.0.0.mca", "server.properties",
           "mods/create.jar"} <= names and not any(n.startswith(("logs/", ".blessforge-tmp/"))
                                                    for n in names), sorted(names))
    if linked:
        check("a link out of the folder is never followed, and a hard-linked file is "
              "skipped and named", "world/escape.txt" not in names
              and "world/hardlink.txt" not in names and first["skipped"] >= 1
              and not any(b"SECRET" in (store.object_path(e[1]).read_bytes()) for e in snap["files"]),
              (first, sorted(names)))

    second = store.take(server, reason="nothing changed")
    check("a second snapshot of an unchanged world stores nothing new and reads "
          "nothing again", second["new_bytes"] == 0 and second["reused"] == second["files"],
          second)
    write(server / "world" / "level.dat", b"level-2-changed")
    time.sleep(0.01)
    third = store.take(server, reason="one file changed")
    check("...and after one change, only that file is stored",
          0 < third["new_bytes"] < 100 and third["reused"] == third["files"] - 1, third)

    # Damage the world, then restore the first snapshot exactly.
    (server / "world" / "region" / "r.0.0.mca").unlink()
    write(server / "world" / "griefer.txt", b"x")
    write(server / "logs" / "keep-me.log", b"logs survive a restore")
    before = tree(server)
    out = store.restore(server, first["id"])
    after = tree(server)
    check("a restore puts back what was deleted, removes what was added, and the "
          "changed file is its old self again",
          "world/region/r.0.0.mca" in after and "world/griefer.txt" not in after
          and after.get("world/level.dat") == b"level-1", sorted(set(before) ^ set(after)))
    check("...and leaves excluded folders (logs) alone",
          after.get("logs/keep-me.log") == b"logs survive a restore", out)
    if linked:
        check("...and never touches anything outside the folder",
              outside.read_bytes() == b"SECRET outside the server")

    blob = b"".join(store.zip_stream(first["id"]))
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        zipped = set(z.namelist())
        level = z.read("world/level.dat")
    check("a backup downloads as a zip, built as it is sent",
          zipped == names and level == b"level-1", len(zipped))

    size_before = store.store_bytes()
    gone = store.keep_only(1)
    check("keep 1: the two older backups go, and files only they used are freed",
          len(gone) == 2 and store.store_bytes() < size_before
          and [s["id"] for s in store.index()] == [third["id"]], (gone, store.index()))

    for bad in ("../../etc/passwd", "x/../y", first["id"]):
        try:
            store.manifest(bad)
            refused = False
        except worldbackup.BackupError as e:
            refused = e.status == 404
        check(f"a backup id like {bad!r} that is not a backup here is a 404", refused)

    packed = worldbackup.Store(TMP / "backups-z", server.name)
    snap = packed.take(server, compress=True)
    write(server / "server.properties", b"motd=changed\n")
    packed.restore(server, snap["id"])
    check("compressed storage restores byte for byte",
          (server / "server.properties").read_bytes() == b"motd=hi\n"
          and any(p.name.endswith(".z") for p in (packed.objects).rglob("*")), snap)


class SavingServer:
    """A running server that confirms `save-all flush` like Minecraft does."""

    def __init__(self, srv):
        self.srv = srv
        self.calls = []

    async def send(self, line):
        self.calls.append(line)
        if line == "save-all flush":
            self.srv.console.feed("[Server thread/INFO]: Saving the game (this may take "
                                  "a moment!)")
            self.srv.console.feed("[Server thread/INFO]: Saved the game")

    async def stop(self, timeout, command="stop"):
        self.calls.append(("stop", command))
        await self.on_exit(0, {})

    async def kill(self):
        pass

    async def stats(self):
        return {}


class Runtime:
    name = "docker"

    def __init__(self):
        self.started = []

    async def start(self, server, folder, on_output, on_exit):
        self.started.append(server["uuid"])
        return None

    async def remove(self, uuid):
        pass


async def supervisor_tests():
    from agent.servers import Supervisor

    class Events:
        def __init__(self):
            self.sent = []

        def emit(self, kind, server=None, **data):
            self.sent.append((kind, server, data))

    cfg = AgentConfig(node_id="n", token_id="t", token="t", panel_url="http://127.0.0.1:9",
                      panel_public_key="", servers_root=TMP / "sup" / "servers",
                      state_dir=TMP / "sup" / "state", runtime="process",
                      allow_unsafe_process_runtime=True)
    events = Events()
    sup = Supervisor(cfg, Runtime(), events)
    sid = "bbbbbbbb-0000-0000-0000-000000000002"
    sup.put(sid, {"name": "Live", "port": 30600, "execution_command": "java -jar s.jar"})
    folder = sup.folder(sid)
    write(folder / "world" / "level.dat", b"live-world")
    srv = sup.servers[sid]
    handle = SavingServer(srv)
    handle.on_exit = lambda code, info: sup._exited(srv, code, info)
    srv.running, srv.state = handle, "running"

    job = sup.start_backup(sid, {"reason": "while running", "keep": 5})
    for _ in range(200):
        if sup.backup_jobs[job]["status"] != "running":
            break
        await asyncio.sleep(0.05)
    rec = sup.backup_jobs[job]
    check("a running server is backed up live: save-off, save-all flush, wait for "
          "'Saved the game', copy, save-on",
          rec["status"] == "done" and handle.calls[:2] == ["save-off", "save-all flush"]
          and handle.calls[-1] == "save-on", (rec.get("error"), handle.calls))
    listed = await sup.backups(sid)
    check("...and it is listed with what the store costs",
          listed["snapshots"][0]["id"] == rec["result"]["id"] and listed["store_bytes"] > 0,
          listed)

    write(folder / "world" / "level.dat", b"broken-world")
    job = sup.start_restore(sid, rec["result"]["id"], {"start_after": True})
    for _ in range(200):
        if sup.backup_jobs[job]["status"] != "running":
            break
        await asyncio.sleep(0.05)
    r2 = sup.backup_jobs[job]
    check("a restore stops the running server, backs the world up as it is first, "
          "restores, and starts it again",
          r2["status"] == "done" and ("stop", "stop") in handle.calls
          and r2.get("safety_backup") and (folder / "world" / "level.dat").read_bytes()
          == b"live-world" and sid in sup.runtime.started, (r2.get("error"), handle.calls))
    safety = [s for s in (await sup.backups(sid))["snapshots"]
              if s["id"] == r2.get("safety_backup")]
    check("...and the world it replaced is kept as a backup of its own",
          safety and safety[0]["reason"].startswith("Before restoring"), safety)


async def panel_tests():
    from app import engine, schedules, worldbackups
    from app.db.models import Server
    from app.engine.mock import MockEngine
    from app import db

    mock = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": mock})
    await db.init()
    sid = await engine.create_server(name="Panel", port=25820, memory_mb=1024,
                                     mc_version="1.21.1", loader="paper")
    async with db.session() as s:
        s.add(Server(uuid=sid, name="Panel", port=25820))
    await engine.upload_file(sid, "world", "level.dat", b"panel-world")

    saved = await worldbackups.save_settings(sid, {"keep": 2, "exclude": "dynmap, bluemap/web"})
    check("backup settings: keep, extra exclusions; the rest default",
          saved["keep"] == 2 and saved["exclude"] == ["dynmap", "bluemap/web"]
          and saved["before_risky"] is True, saved)
    try:
        await worldbackups.save_settings(sid, {"exclude": ["../etc"]})
        refused = False
    except ValueError:
        refused = True
    check("...a folder outside the server is refused", refused)

    for n in range(3):
        made = await worldbackups.take(sid, reason=f"n{n}")
    listing = await worldbackups.listing(sid)
    check("taking a backup waits for it and returns its size; keep 2 holds",
          made["size_human"] and len(listing["snapshots"]) == 2, listing["snapshots"])

    await engine.upload_file(sid, "world", "level.dat", b"oops")
    out = await worldbackups.restore(sid, listing["snapshots"][0]["id"])
    check("a restore from the panel gives the world back, with a safety backup first",
          await engine.download_file(sid, "world/level.dat") == b"panel-world"
          and out.get("safety_backup"), out)

    row = await schedules.create(sid, action="backup", cron="@every 6h",
                                 payload={"name": "Six-hourly", "keep": 2, "tz": "UTC"})
    async with db.session() as s:
        from app.db.models import Schedule
        task = await s.get(Schedule, row["id"])
    ran = await schedules.run_task(task)
    check("a scheduled backup task backs the world up", ran["ok"]
          and ran["result"].startswith("backup "), ran)

    async def failing(server_id, opts):
        raise RuntimeError("disk full")
    real = mock.world_backup_start
    mock.world_backup_start = failing
    try:
        await worldbackups.before_risky(sid, "switching the pack version")
        blocked = False
    except worldbackups.BackupFailed as e:
        blocked = "nothing was changed" in str(e)
    await worldbackups.save_settings(sid, {"before_risky": False})
    skipped = await worldbackups.before_risky(sid, "switching the pack version") is None
    mock.world_backup_start = real
    check("before a risky change: a failed backup stops it; with the setting off, no "
          "backup is taken", blocked and skipped, (blocked, skipped))


async def main():
    store_tests()
    await supervisor_tests()
    await panel_tests()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    ok = asyncio.run(main())
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if ok else 1)
