#!/usr/bin/env python3
"""Who is online, as the node counts it (agent/servers.py).

  * join and leave lines move the list at once, with when each joined;
  * a `list` reply settles it, in both shapes (1.13+ on one line, 1.12 with
    the names on the next);
  * a server list ping confirms the count, names those it samples, and asks
    `list` when it cannot name everyone -- at most every two minutes;
  * a join or leave throws away the last ping's count: the count follows
    the log at once, and the next look asks the server again. (The panel
    once showed two players' names beside "0 online" for 30 seconds.)

Offline: a fake running server and a fake ping.
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import agent.ping  # noqa: E402
from agent.servers import Server, Supervisor  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


UUID = "11111111-2222-3333-4444-555555555555"


class FakeRunning:
    def __init__(self):
        self.sent = []

    async def send(self, line):
        self.sent.append(line)

    async def stats(self):
        return {"cpu": 50.0, "mem": 1024 ** 3}


class Cfg:
    game_bind_ip = "127.0.0.1"


def supervisor():
    sup = Supervisor.__new__(Supervisor)
    sup.cfg = Cfg()
    srv = Server(UUID, {"uuid": UUID, "port": 25565, "memory_mb": 2048,
                        "limits": {"cpu_pct": 200}})
    srv.state = "running"
    srv.running = FakeRunning()
    sup.servers = {UUID: srv}
    sup.samples = {}
    return sup, srv


async def look(sup):
    """What a panel sees after the sampler's next pass."""
    await sup.sample()
    return await sup.stats(UUID)


def line(text):
    return f"[12:00:00] [Server thread/INFO]: {text}"


async def main():
    pings = []

    async def fake_ping(host, port, timeout=3.0):
        return pings.pop(0) if pings else None

    agent.ping.ping = fake_ping

    sup, srv = supervisor()
    sup._track_players(srv, line("Sakura joined the game"))
    sup._track_players(srv, line("Kitsune joined the game"))
    check("join lines put players online at once, with when they joined",
          srv.players == {"Sakura", "Kitsune"} and srv.sessions["Sakura"]["exact"])
    sup._track_players(srv, line("Kitsune left the game"))
    check("a leave line takes them off", srv.players == {"Sakura"}
          and "Kitsune" not in srv.sessions)

    sup._track_players(srv, line("There are 2 of a max of 20 players online: Sakura, Fox_1"))
    check("a modern `list` reply settles the list", srv.players == {"Sakura", "Fox_1"}
          and srv.player_source == "list")
    sup._track_players(srv, line("There are 1/20 players online:"))
    sup._track_players(srv, line("Ember"))
    check("a 1.12 `list` reply, names on the next line", srv.players == {"Ember"})

    # --- the ping --------------------------------------------------------------------------
    sup, srv = supervisor()
    pings.append({"online": 1, "max": 20, "sample": ["Sakura"], "version": "1.20.1"})
    st = await look(sup)
    check("a ping finds a player the log never showed, and names the version",
          st["online"] == 1 and st["players"] == ["Sakura"] and st["max"] == 20
          and st["version"] == "1.20.1" and st["player_source"] == "ping", st)
    check("CPU is a share of the cores the server may use, with the counts",
          st["cpu"] == 25.0 and st["cpu_cores"] == 0.5 and st["cpu_allowance"] == 2.0, st)
    check("memory names the limit it is a share of",
          st["mem_limit"] == 2048 * 1024 ** 2 and st["mem_percent"] == 50.0, st)

    sup._track_players(srv, line("Kitsune joined the game"))
    pings.append(None)          # the server does not answer the next ping
    st = await look(sup)
    check("a join right after a ping counts at once, not the ping's stale number",
          st["online"] == 2 and sorted(st["players"]) == ["Kitsune", "Sakura"], st)
    check("...keeping the slots and version the ping knew", st["max"] == 20
          and st["version"] == "1.20.1", st)
    srv.pinged_at = 0       # thirty seconds on
    pings.append({"online": 2, "max": 20, "sample": ["Kitsune", "Sakura"], "version": "1.20.1"})
    st = await look(sup)
    check("...and when the next ping agrees, the list is confirmed by the server",
          st["online"] == 2 and st["player_source"] == "ping", st)

    sup, srv = supervisor()
    pings.append({"online": 3, "max": 20, "sample": [], "version": "1.20.1"})
    await look(sup)
    check("a ping that names nobody (hidden sample) asks `list` to name them",
          srv.running.sent == ["list"], srv.running.sent)
    srv.pinged_at = 0
    pings.append({"online": 3, "max": 20, "sample": [], "version": "1.20.1"})
    await look(sup)
    check("...but not again within two minutes", srv.running.sent == ["list"],
          srv.running.sent)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
