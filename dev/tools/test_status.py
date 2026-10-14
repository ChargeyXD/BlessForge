#!/usr/bin/env python3
"""The public status page (app/statuspage.py) and pushed fleet changes
(app/fleetbus.py).

    python dev/tools/test_status.py

Offline: a fresh database and the in-memory engine. Checks that the page is
off until switched on, shows only the servers chosen, hides player names
unless asked, is cached for everyone, and that the event stream carries a
server's changes only to browsers that may see that server.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev" / "tools"))

import _dbtest  # noqa: E402 -- a temp STATE_DIR and database

import httpx  # noqa: E402

from app import auth, db, engine, fleetbus, statuspage  # noqa: E402
from app.db.models import Server  # noqa: E402
from app.engine.mock import MockEngine, _stopped  # noqa: E402

results: list[bool] = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {extra}" if extra else ""),
          flush=True)


async def refused(coro) -> bool:
    try:
        await coro
        return False
    except statuspage.StatusPageError:
        return True


def fresh_view():
    statuspage._cache["at"] = 0.0


async def status_page(mock: MockEngine, a: str, b: str, c: str):
    check("the page is off until an admin turns it on", await statuspage.public_view() is None)
    check("an address with a port or a scheme is refused (each server adds its own port)",
          await refused(statuspage.save({"address": "play.example.com:25565"}))
          and await refused(statuspage.save({"address": "https://x"})))
    check("...and so is a server that does not exist",
          await refused(statuspage.save({"servers": [a, "nope"]})))
    saved = await statuspage.save({"enabled": True, "title": "Sakura SMP",
                                   "servers": [b, a], "address": "Play.Example.com"})
    check("settings saved: on, the chosen servers in the chosen order",
          saved["enabled"] and saved["servers"] == [b, a]
          and saved["address"] == "play.example.com", saved)

    mock.stats[a] = _stopped(running=True, online=2, max=20,
                             players=["Alex", "Steve"], started=None)
    mock.stats[a]["state"] = "running"
    view = await statuspage.public_view()
    names = [s["name"] for s in view["servers"]]
    alpha = view["servers"][1]
    check("only the chosen servers are shown, in order (not the private one)",
          names == ["Beta", "Alpha"], names)
    check("a running server: online, its players counted, the join address with its port",
          alpha["state"] == "online" and alpha["players"] == 2 and alpha["max_players"] == 20
          and alpha["address"] == "play.example.com:25801" and view["players"] == 2, alpha)
    check("player names stay hidden unless the admin shows them", "names" not in alpha)
    beta = view["servers"][0]
    check("a stopped server is offline with nobody on, on port 25565 no port is added",
          beta["state"] == "offline" and beta["players"] == 0
          and beta["address"] == "play.example.com", beta)

    calls = {"n": 0}
    real = mock.get_stats

    async def counting(server_id):
        calls["n"] += 1
        return await real(server_id)
    mock.get_stats = counting
    for _ in range(20):
        await statuspage.public_view()
    mock.get_stats = real
    check("twenty visitors inside the cache window cost no reads of the servers",
          calls["n"] == 0, calls)

    await statuspage.save({"show_players": True})
    view = await statuspage.public_view()
    check("...and with names on, they are listed", view["servers"][1].get("names") == [
        "Alex", "Steve"], view["servers"][1])

    mock.stats[a]["state"] = "starting"
    fresh_view()
    view = await statuspage.public_view()
    check("a server still booting shows as starting",
          view["servers"][1]["state"] == "starting", view["servers"][1]["state"])

    from app.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bf.test") as client:
        r = await client.get("/api/status")
        check("anyone can read it, signed in or not", r.status_code == 200
              and r.json()["title"] == "Sakura SMP", r.status_code)
        r2 = await client.get("/api/admin/status-page")
        check("...but its settings need an admin", r2.status_code == 401, r2.status_code)
        await statuspage.save({"enabled": False})
        r3 = await client.get("/api/status")
        check("switched off, it is a 404 again", r3.status_code == 404, r3.status_code)


async def frames(gen, n: int, timeout: float = 1.0) -> list[dict]:
    out = []
    for _ in range(n):
        try:
            chunk = await asyncio.wait_for(gen.__anext__(), timeout)
        except asyncio.TimeoutError:
            break
        text = chunk.decode()
        if text.startswith("event:"):
            out.append(json.loads(text.split("data: ", 1)[1]))
    return out


async def pushed(a: str, b: str):
    mine = fleetbus.stream(lambda sid: sid == a)
    await mine.__anext__()                      # retry:
    hello = await frames(mine, 1)
    check("a stream opens with hello", hello and hello[0]["type"] == "hello")
    fleetbus.publish({"type": "status", "server": b, "state": "running"})
    fleetbus.publish({"type": "crash", "server": a, "code": 1})
    fleetbus.publish({"type": "stats", "servers": {a: {"cpu": 12.0}, b: {"cpu": 99.0}}})
    got = await frames(mine, 2)
    check("a browser hears about its own servers and nothing about the others",
          [g["type"] for g in got] == ["crash", "stats"]
          and list(got[1]["servers"]) == [a], got)
    for i in range(fleetbus.QUEUE + 10):
        fleetbus.publish({"type": "status", "server": a, "state": f"s{i}"})
    got = await frames(mine, 1)
    check("a browser that falls behind is told it lagged, not fed a gap",
          got and got[0]["type"] == "lagged", got[:1])
    await mine.aclose()
    check("a closed stream stops listening", fleetbus.subscribers() == 0,
          fleetbus.subscribers())

    from app.main import app
    await auth.bootstrap()
    await auth.set_password("admin", "admin long password")
    listen = fleetbus.stream(lambda sid: True)
    await listen.__anext__()
    await frames(listen, 1)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bf.test") as client:
        await client.post("/api/auth/login", json={"username": "admin",
                                                   "password": "admin long password"})
        r = await client.post(f"/api/instances/{b}/action/start_server?confirm=true")
    got = await frames(listen, 1)
    check("starting a server from the panel is pushed to open fleet pages at once",
          r.status_code == 200 and got and got[0]["type"] == "power"
          and got[0]["server"] == b, (r.status_code, got))
    await listen.aclose()


async def main():
    await _dbtest.fresh()
    mock = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": mock})
    a = await engine.create_server(name="Alpha", port=25801, memory_mb=1024)
    b = await engine.create_server(name="Beta", port=25565, memory_mb=1024)
    c = await engine.create_server(name="Private", port=25803, memory_mb=1024)
    await engine.update_server(b, {"executable": "server.jar",
                                   "execution_command": "java -jar server.jar"})
    async with db.session() as s:
        for sid, name, port in ((a, "Alpha", 25801), (b, "Beta", 25565), (c, "Private", 25803)):
            s.add(Server(uuid=sid, name=name, port=port))
    await status_page(mock, a, b, c)
    await pushed(a, b)
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed ({_dbtest.label()})")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
