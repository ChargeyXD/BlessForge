#!/usr/bin/env python3
"""Simple Voice Chat (app/voicechat.py): found, configured, one UDP port each.

    python dev/tools/test_voicechat.py
    TEST_DATABASE_URL=postgresql+asyncpg://... python dev/tools/test_voicechat.py

Offline: a fresh database and the in-memory engine. Checks that the mod and
the plugin are both found (and a disabled jar is not), that three servers
with it get three different UDP ports that skip the game ports, that two
configs claiming the same port are sorted out, that a config rewrite keeps
every comment and key it does not own, and that the settings refuse
nonsense.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev" / "tools"))

import _dbtest  # noqa: E402 -- a temp STATE_DIR and database

import httpx  # noqa: E402

from app import auth, db, engine, voicechat  # noqa: E402
from app.db.models import Server  # noqa: E402
from app.engine.mock import MockEngine  # noqa: E402

results: list[bool] = []
JAR = b"PK\x05\x06" + b"\x00" * 18


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {extra}" if extra else ""),
          flush=True)


async def server(name: str, port: int, *, kind: str | None = "mod",
                 jar: str = "voicechat-fabric-1.21.1-2.5.26.jar") -> str:
    sid = await engine.create_server(name=name, port=port, memory_mb=2048,
                                     mc_version="1.21.1", loader="fabric")
    async with db.session() as s:
        s.add(Server(uuid=sid, name=name, port=port))
    if kind == "mod":
        await engine.upload_file(sid, "mods", jar, JAR)
    elif kind == "plugin":
        await engine.upload_file(sid, "plugins", jar, JAR)
    return sid


def pure():
    text = ("# Simple Voice Chat server config\n"
            "# The port number to use for the voice chat communication.\n"
            "port=24454\n"
            "max_voice_distance=48.0\n"
            "some_future_key=keep me\n")
    out = voicechat.render(text, {"port": "24456", "codec": "AUDIO"})
    check("a rewrite changes only what it owns, keeps comments and unknown keys",
          "# The port number to use" in out and "some_future_key=keep me" in out
          and "port=24456" in out and "port=24454" not in out
          and out.rstrip().endswith("codec=AUDIO"), out)
    check("reading the file skips comments",
          voicechat.parse(text) == {"port": "24454", "max_voice_distance": "48.0",
                                    "some_future_key": "keep me"})
    fresh = voicechat.render("", {"port": "24454"})
    check("a first write starts with a header saying who wrote it",
          fresh.startswith("# Simple Voice Chat server config") and "port=24454" in fresh)


async def main():
    pure()
    await _dbtest.fresh()
    mock = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": mock})

    a = await server("Alpha", 25565)
    b = await server("Beta", 24455)            # its GAME port is one voice would want
    c = await server("Gamma", 25567, kind="plugin", jar="voicechat-bukkit-2.5.26.jar")
    d = await server("Delta", 25568, kind=None)
    e = await server("Echo", 25569, kind=None)
    await engine.upload_file(e, "mods", "voicechat-fabric-1.21.1-2.5.26.jar.disabled", JAR)

    found = await voicechat.detect(a)
    check("the mod is found, with its version and config path",
          found["present"] and found["kind"] == "mod" and found["version"] == "2.5.26"
          and found["config_path"] == voicechat.MOD_CONFIG, found)
    found = await voicechat.detect(c)
    check("the plugin is found, with the plugin config path",
          found["present"] and found["kind"] == "plugin"
          and found["config_path"] == voicechat.PLUGIN_CONFIG, found)
    check("no jar, or a disabled one, is not installed",
          not (await voicechat.detect(d))["present"]
          and not (await voicechat.detect(e))["present"])
    check("ensure() on a server without it does nothing", await voicechat.ensure(d) is None)

    ra = await voicechat.ensure(a)
    rb = await voicechat.ensure(b)
    rc = await voicechat.ensure(c)
    ports = [ra["port"], rb["port"], rc["port"]]
    check("three servers, three UDP ports, from 24454 up",
          len(set(ports)) == 3 and min(ports) == 24454, ports)
    check("...and none on a game port (Beta plays on 24455)", 24455 not in ports, ports)
    text = await engine.read_file(a, voicechat.MOD_CONFIG)
    check("the config is written before the first start, with the port",
          f"port={ra['port']}" in text and "bind_address=" in text, text)
    ptext = await engine.read_file(c, voicechat.PLUGIN_CONFIG)
    check("the plugin's config goes under plugins/voicechat/",
          f"port={rc['port']}" in ptext)
    again = await voicechat.ensure(a)
    check("ensuring again keeps the port it has", again["port"] == ra["port"]
          and again["changed"] == [], again.get("changed"))

    # Two servers whose own configs both say 24454 (hand-edited, or from
    # before BlessForge looked after this).
    f = await server("Foxtrot", 25570)
    await engine.ensure_dir(f, "config/voicechat")
    await engine.write_file(f, voicechat.MOD_CONFIG,
                            "# mine\nport=24454\nmax_voice_distance=64.0\n")
    rf = await voicechat.ensure(f)
    ftext = await engine.read_file(f, voicechat.MOD_CONFIG)
    check("a config that claims another server's port is moved to a free one",
          rf["port"] not in ports and rf["moved_from"] == 24454
          and f"port={rf['port']}" in ftext and "max_voice_distance=64.0" in ftext
          and "# mine" in ftext, (rf["port"], ftext))

    st = await voicechat.status(f)
    check("status reads the file's settings, with defaults for the rest",
          st["values"]["max_voice_distance"] == 64.0 and st["values"]["codec"] == "VOIP"
          and st["values"]["enable_groups"] is True and not st["clash"], st["values"])

    out = await voicechat.configure(f, {"codec": "AUDIO", "enable_groups": False,
                                        "max_voice_distance": 32, "voice_host": "play.example.test"})
    ftext = await engine.read_file(f, voicechat.MOD_CONFIG)
    check("settings are written in the file's own form",
          "codec=AUDIO" in ftext and "enable_groups=false" in ftext
          and "max_voice_distance=32" in ftext and "voice_host=play.example.test" in ftext
          and out["port"] == rf["port"], ftext)

    async def refused(coro):
        try:
            await coro
            return None
        except voicechat.VoiceChatError as err:
            return err.status
    check("nonsense is refused: a codec, a distance, a host with spaces, an unknown key",
          await refused(voicechat.configure(f, {"codec": "MP3"})) == 400
          and await refused(voicechat.configure(f, {"max_voice_distance": 0})) == 400
          and await refused(voicechat.configure(f, {"voice_host": "a b"})) == 400
          and await refused(voicechat.configure(f, {"port": 1})) == 400)
    check("a port another server has, or a game port, is refused",
          await refused(voicechat.configure(f, port=ra["port"])) == 409
          and await refused(voicechat.configure(f, port=25565)) == 409)
    chosen = await voicechat.configure(f, port=24600)
    check("a free port by hand is taken as given", chosen["port"] == 24600
          and "port=24600" in await engine.read_file(f, voicechat.MOD_CONFIG))

    await engine.delete_paths(a, ["mods/voicechat-fabric-1.21.1-2.5.26.jar"])
    await voicechat.ensure(a)
    g = await server("Golf", 25571)
    rg = await voicechat.ensure(g)
    check("removing it frees its port for the next server", rg["port"] == ra["port"],
          (rg["port"], ra["port"]))

    # Through the panel, as the Voice section does it.
    from app.main import app
    await auth.bootstrap()
    await auth.set_password("admin", "admin long password")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bf.test") as client:
        await client.post("/api/auth/login", json={"username": "admin",
                                                   "password": "admin long password"})
        r1 = await client.get(f"/api/instances/{c}/voicechat")
        r2 = await client.post(f"/api/instances/{c}/voicechat",
                               json={"values": {"force_voice_chat": True}})
        r3 = await client.post(f"/api/instances/{c}/voicechat", json={"port": "auto"})
        r4 = await client.get(f"/api/instances/{d}/voicechat")
        check("the API reads and writes it; auto picks a free port; absent says so",
              r1.status_code == 200 and r1.json()["kind"] == "plugin"
              and r2.status_code == 200 and r2.json()["values"]["force_voice_chat"] is True
              and r3.status_code == 200 and r3.json()["port"] >= 24454
              and r4.json() == {"present": False},
              (r1.status_code, r2.status_code, r3.status_code, r4.json()))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed ({_dbtest.label()})")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
