#!/usr/bin/env python3
"""A panel on the in-memory engine with enough in it to look at every screen.

    python dev/tools/dev_panel.py [--port 8710]

For working on the front end: no node, no Docker, no network. The mock
engine's two seeded servers get panel records, three days of stats history
and a couple of world backups; a crashed server, a half-installed one and a
Paper lobby join them; there are schedules, a second account with a
per-server grant, and the public status page is on. The admin account is
`admin` with the test-only password in ADMIN_PASSWORD below.

State goes to a temp directory unless STATE_DIR is set, so it never touches
a real install.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import pathlib
import random
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("ENGINE", "mock")
if not os.environ.get("STATE_DIR"):
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bf-devpanel-"))
    os.environ["STATE_DIR"] = str(tmp / "state")
    os.environ.setdefault("DATA_DIR", str(tmp))
    os.environ.setdefault("CACHE_DIR", str(tmp / "cache"))
os.environ["DATABASE_URL"] = os.environ.get("DEV_DATABASE_URL", "")
os.environ.pop("REDIS_URL", None)

ADMIN_PASSWORD = "live scripts passphrase"      # test-only, never a real one
FRIEND_PASSWORD = "friend test passphrase"


async def _seed() -> None:
    from sqlalchemy import select
    from app import auth, db, engine, history, schedules, statuspage, access
    from app.db.models import Server
    from app.engine.mock import _stopped

    for _ in range(100):
        try:
            drv = engine.drivers().get("local") or next(iter(engine.drivers().values()))
            break
        except Exception:       # noqa: BLE001 -- the lifespan has not configured it yet
            await asyncio.sleep(0.2)
    else:
        return
    await auth.bootstrap()
    await auth.set_password("admin", ADMIN_PASSWORD)

    extra = [
        ("Vault Hunters", 25567, "forge", "1.18.2", "crashed"),
        ("Half Built", 25568, "fabric", "1.20.1", "incomplete"),
        ("Lobby", 25569, "purpur", "1.21.4", "running"),
    ]
    made = {}
    for name, port, loader, mc, st in extra:
        sid = await engine.create_server(name=name, port=port, memory_mb=6144,
                                         mc_version=mc, loader=loader)
        made[name] = sid
        await engine.update_server(sid, {
            "executable": f"{loader}-{mc}-100.jar",
            "execution_command": f"java -Xms2G -Xmx5G -jar {loader}-{mc}-100.jar nogui"})
        manifest = {"schema": 1, "complete": st != "incomplete", "loader": loader,
                    "minecraft": mc, "loader_version": "100", "mods": [],
                    "problems": [{"severity": "bad", "title": "Out of memory"}]
                    if st == "crashed" else []}
        await engine.write_manifest(sid, manifest)
        if st == "crashed":
            drv.stats[sid] = _stopped(max=20, crashed=True)
            drv.stats[sid]["last_exit"] = {"code": 137, "oom": True, "at": 0, "asked": False}
        elif st == "running":
            drv.stats[sid] = _stopped(running=True, online=7, max=50, cpu=22.0,
                                      mem=2_100_000_000, mem_percent=34.0,
                                      players=["Mochi_Kit", "tanuki_ren", "yuzu_bloom",
                                               "Notch", "Alex", "Steve", "Kitsune"])
            drv.stats[sid]["mem_limit"] = 6144 * 1024 * 1024

    ids = ["aaaa1111", "bbbb2222", *made.values()]
    names = {"aaaa1111": "Sakura SMP", "bbbb2222": "Paper Survival",
             **{v: k for k, v in made.items()}}
    ports = {sid: int((await engine.get_server(sid)).get("server_port") or 25565) for sid in ids}
    async with db.session() as s:
        have = set((await s.execute(select(Server.uuid))).scalars().all())
        for sid in ids:
            if sid not in have:
                s.add(Server(uuid=sid, name=names[sid], port=ports[sid], status="running"))

    # Simple Voice Chat on two servers: a mod on one, a plugin on the other,
    # each given its own UDP port.
    from app import voicechat
    jar = b"PK\x05\x06" + b"\x00" * 18
    await engine.upload_file("aaaa1111", "mods", "voicechat-neoforge-1.21.1-2.5.26.jar", jar)
    await engine.upload_file(made["Lobby"], "plugins", "voicechat-bukkit-2.5.26.jar", jar)
    for sid in ("aaaa1111", made["Lobby"]):
        await voicechat.ensure(sid)

    # Three days of history for the two seeded servers and the lobby.
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    rows = []
    rnd = random.Random(7)
    for sid, peak in (("aaaa1111", 9), (made["Lobby"], 24), ("bbbb2222", 4)):
        for i in range(72 * 60):
            at = now - dt.timedelta(minutes=72 * 60 - i)
            hour = (at.hour + 5) % 24
            evening = max(0.0, 1 - abs(hour - 20) / 7)
            up = not (sid == "bbbb2222" and (i // 300) % 3 == 2)
            online = round(peak * evening * rnd.uniform(0.6, 1.0)) if up else 0
            rows.append({"server_id": sid, "at": at, "res": 1, "samples": 1, "up": int(up),
                         "online": online, "max_players": 20 if up else None,
                         "cpu": round(8 + 50 * evening * rnd.uniform(0.5, 1.1), 1)
                         if up else None,
                         "mem": int((2.4 + 1.4 * evening + rnd.uniform(-0.2, 0.2)) * 1024 ** 3)
                         if up else None,
                         "mem_limit": 8 * 1024 ** 3})
    await history._insert(rows)
    await history.compact()

    from app import worldbackups
    await engine.upload_file("aaaa1111", "world", "level.dat", b"seed world")
    for reason in ("Nightly", "Before switching the pack version", "manual"):
        await worldbackups.take("aaaa1111", reason=reason)

    try:
        await schedules.preset("aaaa1111", "daily_restart", {"time": "05:00", "warn": [5, 1]})
        await schedules.preset("aaaa1111", "backup_every", {"hours": 6, "keep": 8})
        await schedules.create("aaaa1111", action="say", cron="0 */2 * * *",
                               payload={"name": "Vote reminder",
                                        "message": "Vote for the server on the shrine board!"})
    except Exception as e:      # noqa: BLE001 -- a dev nicety, not a requirement
        print("schedules not seeded:", e)

    try:
        await auth.create_user(username="mochi", password=FRIEND_PASSWORD, role="guest",
                               must_change=False, display="Mochi")
        await access.set_grant("mochi", made["Lobby"],
                               ["console.read", "console.command", "server.power"])
    except auth.AuthError:
        pass
    await statuspage.save({"enabled": True, "title": "Sakura Shrine",
                           "blurb": "The servers we play on. Come say hi.",
                           "servers": ["aaaa1111", made["Lobby"], "bbbb2222"],
                           "address": "play.example.test", "show_players": True})
    print(f"dev panel seeded: admin / {ADMIN_PASSWORD!r}", flush=True)


async def main(port: int) -> None:
    import uvicorn
    from app.main import app
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))
    seeding = asyncio.create_task(_seed())
    await server.serve()
    seeding.cancel()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8710)
    asyncio.run(main(ap.parse_args().port))
