#!/usr/bin/env python3
"""import_from_crafty.py, against the Crafty mock (dev/mock/crafty.py).

What this pins down:

  * a dry run says what it would do and changes nothing;
  * through Crafty's API: each server is created with its name, port and
    heap, the loader and version its manifest names, every file copied
    (worlds and config included), its command pointed at the node's own Java
    rather than Crafty's absolute JVM path, the EULA accepted, the manifest
    kept and stamped with where it came from -- and then booted;
  * run again, nothing is imported twice;
  * a port already taken here is reported for that server, and the others
    still import;
  * a server whose version cannot be told is reported, not guessed;
  * from Crafty's folder on disk: files byte-for-byte, with a planted link
    not followed and a world's session.lock left behind;
  * Crafty itself is only ever read.

Offline: the mock runs in-process; the engine is the in-memory one.
"""
import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev" / "tools"))

import _dbtest  # noqa: E402,F401  -- temp STATE_DIR before anything from app

os.environ["CRAFTY_URL"] = "http://crafty.test"
os.environ["CRAFTY_TOKEN"] = "test-token"

import httpx  # noqa: E402

import import_from_crafty as importer  # noqa: E402
from app import engine, loaders, specs  # noqa: E402
from app.engine.mock import MockEngine  # noqa: E402
from dev.mock import crafty as mock  # noqa: E402

results = []
WRITES = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""),
          flush=True)


class Recording(httpx.ASGITransport):
    """The mock, in-process, noting every request that is not a read."""

    async def handle_async_request(self, request):
        path = request.url.path
        read = request.method == "GET" or (request.method == "POST"
                                           and path.endswith("/files"))
        if not read:
            WRITES.append(f"{request.method} {path}")
        return await super().handle_async_request(request)


def args(**kw):
    base = dict(from_dir=None, only=None, no_boot=False, dry_run=False, auto_ports=False,
                connect_engine=False, report=None)
    return importer.parse([]).__class__(**{**base, **kw})


async def go(eng: MockEngine, **kw) -> dict:
    engine.configure("mock", drivers={"local": eng})
    return await importer.run(args(**kw), setup=False, transport=Recording(app=mock.app))


async def main():
    async def offline(url, timeout=30.0):
        raise RuntimeError("offline test")
    loaders._json = offline

    eng = MockEngine(seed=False)
    summary = await go(eng, dry_run=True)
    check("a dry run lists both servers and creates nothing",
          [r["outcome"] for r in summary["results"]] == ["would import"] * 2
          and not eng.servers, summary["results"])

    summary = await go(eng)
    by_name = {r["name"]: r for r in summary["results"]}
    check("both servers import and boot", summary["ok"] == 2
          and all(r["outcome"] == "booted" for r in summary["results"]),
          [(r["name"], r["outcome"], r.get("why")) for r in summary["results"]])
    sakura, paper = by_name["Sakura SMP"], by_name["Paper Survival"]
    s_rec = await engine.get_server(sakura["server_id"])
    p_rec = await engine.get_server(paper["server_id"])
    check("names and ports carried over",
          (s_rec["server_name"], s_rec["server_port"], p_rec["server_name"],
           p_rec["server_port"]) == ("Sakura SMP", 25565, "Paper Survival", 25566))
    check("the heap is read from user_jvm_args.txt (6G) or the command (4G)",
          sakura["heap_gb"] == 6 and paper["heap_gb"] == 4
          and s_rec.get("memory_mb") == specs.container_memory_mb(6),
          (sakura["heap_gb"], paper["heap_gb"], s_rec.get("memory_mb")))
    check("the loader and version come from the manifest BlessForge left there",
          (sakura["loader"], sakura["minecraft"], paper["loader"], paper["minecraft"])
          == ("neoforge", "1.21.1", "paper", "1.21.4"))
    check("Crafty's absolute JVM path becomes the node's own java",
          s_rec["execution_command"].startswith("java @user_jvm_args.txt")
          and "/usr/lib/jvm" not in s_rec["execution_command"]
          and s_rec.get("java_major") == 21, s_rec["execution_command"])
    sid = sakura["server_id"]
    props = await engine.read_file(sid, "server.properties")
    check("text files are copied exactly",
          props == mock.TEXT["aaaa1111"]["server.properties"])
    copied = {e["path"] for e in await engine.walk(sid)}
    check("mods, config and the world are all copied",
          {"mods/jei-1.21.1-19.21.0.jar", "config/jei/jei-client.ini",
           "world/region/r.0.0.mca", "world/level.dat"} <= copied,
          sorted(copied)[:12])
    manifest = await engine.read_manifest(sid)
    check("the manifest is kept, and says where the server came from",
          len(manifest.get("mods") or []) == 3
          and manifest.get("imported_from", {}).get("crafty") == "aaaa1111")
    check("the EULA is accepted", (await engine.read_file(sid, "eula.txt")).startswith(
        "eula=true"))
    check("Crafty was only read", WRITES == [], WRITES)

    summary = await go(eng)
    check("run again, nothing is imported twice",
          [r["outcome"] for r in summary["results"]] == ["skipped", "skipped"]
          and len(eng.servers) == 2)

    eng2 = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": eng2})
    await engine.create_server(name="Already here", port=25566, memory_mb=2048)
    summary = await go(eng2, no_boot=True)
    outcomes = {r["name"]: (r["outcome"], r.get("why", "")) for r in summary["results"]}
    check("a port already taken here fails that server only, saying why",
          outcomes["Paper Survival"][0] == "failed" and "25566" in outcomes["Paper Survival"][1]
          and outcomes["Sakura SMP"][0] == "copied", outcomes)
    check("...and Crafty is not declared safe to remove", not summary["crafty_can_go"])

    eng3 = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": eng3})
    await engine.create_server(name="Already here", port=25566, memory_mb=2048)
    summary = await go(eng3, no_boot=True, auto_ports=True)
    imported = {r["name"]: r for r in summary["results"]}
    sid = imported["Paper Survival"].get("server_id")
    check("with --auto-ports that server is imported anyway, with Port: automatic",
          imported["Paper Survival"]["outcome"] == "copied" and sid
          and (await engine.get_server(sid))["limits"].get("port_auto"), imported)

    mock.SERVERS["cccc3333"] = {"server_id": "cccc3333", "server_name": "Mystery",
                                "server_port": 25570, "path": "/servers/mystery",
                                "executable": "start.jar",
                                "execution_command": "java -Xmx2G -jar start.jar nogui"}
    mock.FILES["cccc3333"] = {".": {}}
    try:
        summary = await go(MockEngine(seed=False), only="cccc3333")
    finally:
        mock.SERVERS.pop("cccc3333")
        mock.FILES.pop("cccc3333")
    r = summary["results"][0]
    check("a server whose Minecraft version cannot be told is reported, not guessed",
          r["outcome"] == "failed" and "version" in r["why"], r)

    # --- from Crafty's folder on disk -------------------------------------------------
    disk = _dbtest.TMP / "crafty-servers"
    sakura_dir = disk / "sakura"
    (sakura_dir / "world" / "region").mkdir(parents=True)
    (sakura_dir / "mods").mkdir()
    region = os.urandom(200_000)
    (sakura_dir / "world" / "region" / "r.0.0.mca").write_bytes(region)
    (sakura_dir / "world" / "session.lock").write_bytes(b"\xe2\x98\x83")
    (sakura_dir / "mods" / "jei.jar").write_bytes(b"PK\x03\x04jar")
    (sakura_dir / "server.properties").write_bytes(b"motd=from disk\n")
    outside = _dbtest.TMP / "not-crafty"
    outside.mkdir()
    (outside / "secret.txt").write_text("the host's own file")
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(sakura_dir / "escape"))
    else:
        os.symlink(outside, sakura_dir / "escape")
    eng3 = MockEngine(seed=False)
    summary = await go(eng3, from_dir=str(disk), only="aaaa1111")
    r = summary["results"][0]
    check("from disk: imported and booted", r["outcome"] == "booted", r.get("why"))
    sid = r["server_id"]
    got = await engine.download_file(sid, "world/region/r.0.0.mca")
    paths = {e["path"] for e in await engine.walk(sid)}
    check("from disk: a world file arrives byte for byte", got == region)
    check("from disk: a planted link is not followed",
          not any(p.startswith("escape") for p in paths), sorted(paths))
    check("from disk: the old process's session.lock is left behind",
          "world/session.lock" not in paths)

    report = _dbtest.TMP / "report.json"
    summary = await go(MockEngine(seed=False), report=str(report), no_boot=True)
    saved = json.loads(report.read_text(encoding="utf-8"))
    check("the report is written as JSON, and copy-only never clears Crafty to go",
          saved["servers"] == 2 and saved["crafty_can_go"] is False)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
