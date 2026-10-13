#!/usr/bin/env python3
"""The engine: one door to every server, and the loader ladder behind it.

What this pins down:

  * the router -- one node needs no lookup; several need one, and a server
    nobody has mapped is a 404, not a guess at the first node;
  * the mock driver is faithful rather than permissive: paths outside the
    folder are refused, a zip with one bad member is refused whole, a
    command to a stopped server is refused, a binary file does not read as
    text;
  * the loader ladder in app/provision.py, now that nothing installs a
    loader for us: the node fetches it; when it cannot, BlessForge fetches,
    checks and uploads it; when neither can, the failure says so and leaves
    the server in place. Forge/NeoForge get their installer run and their
    startup command read back from run.sh -- or, before 1.17, from the jar
    the installer leaves;
  * the exact loader build a pack names is the one asked for;
  * and the WP-02 promise: nothing under app/ mentions Crafty any more.

Offline. The engine is the in-memory mock; the loaders' projects are stubbed.
"""
import asyncio
import hashlib
import io
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import engine, loaders, provision  # noqa: E402
from app.engine.base import ServerEngine  # noqa: E402
from app.engine.mock import MockEngine  # noqa: E402
from app.jobs import Job  # noqa: E402

results = []


def check(name, condition, extra=""):
    results.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


async def refused(coro, status=None) -> bool:
    try:
        await coro
    except engine.EngineError as e:
        return status is None or e.status == status
    return False


def zip_of(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


SOURCES = {
    "fabric": {"url": "https://meta.fabricmc.net/x/server/jar",
               "filename": "fabric-server-mc.1.21.1-loader.0.16.5-launcher.1.0.1.jar",
               "kind": "server", "loader_version": "0.16.5", "source": "FabricMC"},
    "neoforge": {"url": "https://maven.neoforged.net/x/neoforge-21.1.209-installer.jar",
                 "filename": "neoforge-21.1.209-installer.jar",
                 "kind": "installer", "loader_version": "21.1.209",
                 "source": "NeoForged"},
    "forge-legacy": {"url": "https://maven.minecraftforge.net/x/forge-1.16.5-36.2.39-installer.jar",
                     "filename": "forge-1.16.5-36.2.39-installer.jar",
                     "kind": "installer", "loader_version": "36.2.39",
                     "source": "MinecraftForge"},
}


async def main():
    # Offline: the templates ask Mojang which Java a version needs, and fall
    # back to the table when it does not answer -- which is what this is.
    async def offline(url, timeout=30.0):
        raise RuntimeError("offline test")
    loaders._json = offline

    # --- the router ---------------------------------------------------------
    a, b = MockEngine(seed=False, node="a"), MockEngine(seed=False, node="b")
    check("the mock implements the whole ServerEngine protocol",
          isinstance(a, ServerEngine))
    engine.configure(drivers={"a": a})
    sid = await engine.create_server(name="One", port=25570, memory_mb=2048)
    check("with one node, any server routes to it", engine.for_server("whatever") is a)

    engine.configure(drivers={"a": a, "b": b})
    sid_b = await engine.create_server(name="Two", port=25571, memory_mb=2048, node="b")
    check("a created server is mapped to the node it was created on",
          engine.for_server(sid_b) is b)
    check("with several nodes an unmapped server is a 404, not a guess",
          await refused(engine.get_server("nobody-knows"), 404))
    listed = await engine.list_servers()
    check("listing every node maps what it finds",
          {s["server_id"] for s in listed} == {sid, sid_b}
          and engine.for_server(sid) is a)

    class Down(MockEngine):
        async def list_servers(self):
            raise engine.EngineError("node b is not answering", 503)

    engine.configure(drivers={"a": a, "b": Down(seed=False, node="b")})
    survivors = await engine.list_servers()
    check("one node not answering does not blank the fleet",
          [s["server_id"] for s in survivors] == [sid])

    # --- the mock is faithful, not permissive --------------------------------
    m = MockEngine(seed=False)
    engine.configure(drivers={"mock": m})
    s1 = await engine.create_server(name="Files", port=25572, memory_mb=2048,
                                    mc_version="1.21.1")
    check("a new server records the Java its version needs",
          (await engine.get_server(s1))["java_major"] == 21)
    dup = await engine.create_server(name="Dup", port=25572, memory_mb=1024)
    check("two servers may share a port (one runs at a time)",
          (await engine.get_server(dup))["server_port"] == 25572)
    await engine.update_server(s1, {"auto_start": True})
    check("...but only one of them may start by itself",
          await refused(engine.update_server(dup, {"auto_start": True}), 409))
    from app import properties
    moved = await properties.set_port(dup, 25572)
    check("setting a port another server uses warns rather than refuses",
          any("Also used by Files (only one can run at a time)" in w
              for w in moved["warnings"]), moved["warnings"])
    await engine.update_server(s1, {"auto_start": False})
    await engine.delete_server(dup)
    for bad in ("../etc/passwd", "/etc/passwd", "a/../../b", "x\x00y", "C:/Windows"):
        check(f"a path outside the folder is refused: {bad!r}",
              await refused(engine.read_file(s1, bad), 400))

    await engine.ensure_dir(s1, "config/deep/er")
    await engine.write_file(s1, "config/deep/er/a.toml", "x = 1\n")
    check("ensure_dir is mkdir -p and a written file reads back",
          await engine.read_file(s1, "config/deep/er/a.toml") == "x = 1\n")
    await engine.create_entry(s1, "config", "deep", directory=True)
    check("creating a folder that exists is not an error (mkdir -p)", True)
    await engine.upload_file(s1, "mods", "thing.jar", b"\x00\xffbinary")
    check("a binary file does not read as text",
          await refused(engine.read_file(s1, "mods/thing.jar"), 415))
    await engine.rename_path(s1, "mods/thing.jar", "thing.jar.disabled")
    await engine.move_path(s1, "mods/thing.jar.disabled", "config")
    check("rename stays in the folder, move changes it",
          "thing.jar.disabled" in await engine.list_dir(s1, "config")
          and "thing.jar.disabled" not in await engine.list_dir(s1, "mods"))
    listing = await engine.list_dir(s1, "config")
    check("sizes are bytes and times are epoch seconds",
          listing["thing.jar.disabled"]["size"] == 8
          and listing["thing.jar.disabled"]["modified"] > 1_700_000_000)

    await engine.upload_file(s1, "packs", "evil.zip", zip_of({
        "fine.txt": "ok", "../../escape.txt": "pwned"}))
    check("a zip with one member outside its folder is refused whole",
          await refused(engine.extract(s1, "packs/evil.zip"), 400))
    check("...and nothing from it was written",
          "fine.txt" not in await engine.list_dir(s1, "packs"))
    await engine.upload_file(s1, "packs", "good.zip", zip_of({
        "mods/a.jar": "a", "config/b.toml": "b"}))
    await engine.extract(s1, "packs/good.zip")
    check("a clean zip unpacks next to itself, and is finished on return",
          "a.jar" in await engine.list_dir(s1, "packs/mods"))

    check("a command to a stopped server is refused",
          await refused(engine.send_command(s1, "say hi"), 409))
    check("a server with no startup command will not start",
          await refused(engine.power(s1, "start"), 409))
    check("an unknown power action is refused",
          await refused(engine.power(s1, "explode"), 400))

    # --- the loader ladder ---------------------------------------------------
    asked: list = []
    fetched: list = []

    async def fake_resolve(family, mc, build=None):
        asked.append((family, mc, build))
        key = "forge-legacy" if family == "forge" else family
        src = dict(SOURCES.get(key, {}))
        if build and build != src.get("loader_version"):
            return {}
        return src

    payloads = {}

    async def fake_fetch(url, attempts=4):
        fetched.append(url)
        if url in payloads:
            return payloads[url]
        raise RuntimeError("the project's server is down")

    saved = (loaders.resolve, loaders.fetch)
    loaders.resolve, loaders.fetch = fake_resolve, fake_fetch
    try:
        # 1. Fabric, the node fetches it.
        m = MockEngine(seed=False)
        engine.configure(drivers={"mock": m})
        fab = await engine.create_server(name="Fab", port=25580, memory_mb=3072)
        job = Job("t", "fabric")
        r = await provision.install_loader(job, fab, family="fabric", mc_version="1.21.1")
        rec = await engine.get_server(fab)
        jar = SOURCES["fabric"]["filename"]
        check("fabric: the node fetched the jar itself",
              r["source"] == "node" and m.fetched == [SOURCES["fabric"]["url"]]
              and not fetched)
        check("fabric: the startup command runs the jar that is on disk",
              rec["execution_command"] == f"java -Xms1G -Xmx2G -jar {jar} nogui"
              and rec["executable"] == jar and jar in await engine.list_dir(fab, "."))
        check("fabric: and it runs on the Java 1.21.1 needs", rec["java_major"] == 21)

        # 2. The node cannot reach the project: BlessForge relays it.
        good = b"PK fabric jar bytes"
        payloads[SOURCES["fabric"]["url"]] = good
        SOURCES["fabric"]["sha1"] = hashlib.sha1(good).hexdigest()
        m.fail_fetch = "name resolution failed"
        relay = await engine.create_server(name="Relay", port=25581, memory_mb=3072)
        job = Job("t", "relay")
        r = await provision.install_loader(job, relay, family="fabric", mc_version="1.21.1")
        check("relay: when the node cannot download, BlessForge fetches and uploads",
              r["source"] == "relay" and r["repairs"] == ["relay"]
              and await engine.download_file(relay, jar) == good)
        check("relay: and the log says which rung it took",
              any("could not download" in e["message"] for e in job.log))

        # 3. A corrupt relay download is refused, not installed.
        payloads[SOURCES["fabric"]["url"]] = b"truncated"
        m.fail_fetch = "timeout"
        bad = await engine.create_server(name="Bad", port=25582, memory_mb=3072)
        try:
            await provision.install_loader(Job("t", "x"), bad, family="fabric",
                                           mc_version="1.21.1")
            corrupt = None
        except RuntimeError as e:
            corrupt = str(e)
        check("relay: a jar whose checksum does not match is refused",
              corrupt and "corrupt" in corrupt, corrupt)
        check("...and is not left on the server", jar not in await engine.list_dir(bad, "."))
        del SOURCES["fabric"]["sha1"]

        # 4. Neither can reach it: say so, leave the server.
        payloads.pop(SOURCES["fabric"]["url"], None)
        m.fail_fetch = "no route"
        gone = await engine.create_server(name="Gone", port=25583, memory_mb=3072)
        try:
            await provision.install_loader(Job("t", "x"), gone, family="fabric",
                                           mc_version="1.21.1")
            msg = None
        except RuntimeError as e:
            msg = str(e)
        check("exhausted: the failure names both rungs and offers Finish setup",
              msg and "node could not download" in msg and "Finish setup" in msg, msg)
        check("exhausted: the server is left in place to retry into",
              gone in {s["server_id"] for s in await engine.list_servers()})

        # 5. NeoForge: installer runs, command read back from run.sh.
        neo = await engine.create_server(name="Neo", port=25584, memory_mb=8192)
        asked.clear()
        r = await provision.install_loader(Job("t", "neo"), neo, family="neoforge",
                                           mc_version="1.21.1",
                                           loader_version="21.1.209")
        rec = await engine.get_server(neo)
        root = set(await engine.list_dir(neo, "."))
        check("neoforge: the exact build the pack names is the one asked for",
              ("neoforge", "1.21.1", "21.1.209") in asked)
        check("neoforge: the startup command is the args-file line from run.sh",
              rec["execution_command"] == "java @user_jvm_args.txt "
              "@libraries/net/neoforged/neoforge/21.1.209/unix_args.txt nogui",
              rec["execution_command"])
        check("neoforge: the executable is the server jar under libraries/",
              rec["executable"] == "libraries/net/neoforged/neoforge/21.1.209/"
              "neoforge-21.1.209-server.jar")
        check("neoforge: the installer jar is cleaned up afterwards",
              SOURCES["neoforge"]["filename"] not in root and "run.sh" in root)
        check("neoforge: the installer ran with java -jar ... --installServer",
              any(i.get("run") == ["java", "-jar", SOURCES["neoforge"]["filename"],
                                   "--installServer"] for i in m.installs))

        # 6. An exact build that is gone falls back to the newest, and says so.
        neo2 = await engine.create_server(name="Neo2", port=25585, memory_mb=8192)
        job = Job("t", "neo2")
        await provision.install_loader(job, neo2, family="neoforge",
                                       mc_version="1.21.1", loader_version="21.1.1")
        check("a pack's build that is no longer published falls back, with a warning",
              any("not published" in e["message"] for e in job.log))

        # 7. Forge before 1.17 leaves a jar, not a script.
        old = await engine.create_server(name="Old", port=25586, memory_mb=4096)
        await provision.install_loader(Job("t", "old"), old, family="forge",
                                       mc_version="1.16.5")
        rec = await engine.get_server(old)
        check("legacy forge: the command runs the jar the installer left",
              rec["execution_command"].endswith("-jar forge-1.16.5-36.2.39.jar nogui"),
              rec["execution_command"])
        check("legacy forge: and runs on Java 8", rec["java_major"] == 8)

        # 8. An installer that fails leaves the server and says why.
        m.fail_run = "Could not download library net.minecraft:server"
        broken = await engine.create_server(name="Broken", port=25587, memory_mb=8192)
        try:
            await provision.install_loader(Job("t", "x"), broken, family="neoforge",
                                           mc_version="1.21.1")
            why = None
        except RuntimeError as e:
            why = str(e)
        check("a failed installer is reported, and the server kept",
              why and "installer did not finish" in why
              and broken in {s["server_id"] for s in await engine.list_servers()}, why)

        # 9. A whole empty server, start to finish.
        r = await provision.create_empty(
            Job("t", "empty"), name="Empty Paper", loader="fabric",
            mc_version="1.21.1", port=25588, mem_max=4, motd="hello", optimize=False)
        sid = r["server_id"]
        props = await engine.read_file(sid, "server.properties")
        manifest = await engine.read_manifest(sid)
        rec = await engine.get_server(sid)
        check("create_empty: EULA accepted", "eula=true" in await engine.read_file(
            sid, "eula.txt"))
        check("create_empty: the port and MOTD reach server.properties",
              "server-port=25588" in props and "motd=hello" in props, props)
        check("create_empty: the mod folder exists", "mods" in await engine.list_dir(sid, "."))
        check("create_empty: the manifest is marked complete",
              manifest.get("complete") is True and manifest.get("loader") == "fabric")
        check("create_empty: the container holds the heap plus the JVM's overhead",
              rec["memory_mb"] >= 4096 + 768, rec["memory_mb"])

        # 10. Finish setup on a half-made server.
        m.fail_fetch = None
        half = await engine.create_server(name="Half", port=25589, memory_mb=8192)
        await engine.write_manifest(half, {"loader": "neoforge", "minecraft": "1.21.1",
                                           "loader_version": "21.1.209",
                                           "complete": False})
        r = await provision.reinstall_loader(Job("t", "finish"), half)
        check("Finish setup installs the recorded build and marks it complete",
              r["ok"] and (await engine.read_manifest(half)).get("complete") is True)
    finally:
        loaders.resolve, loaders.fetch = saved

    # --- no Crafty left ---------------------------------------------------------
    hits = []
    for path in (ROOT / "app").rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search("crafty", line, re.I):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{n}")
    check("nothing under app/ mentions Crafty", not hits, ", ".join(hits[:5]))
    check("app/crafty.py is gone", not (ROOT / "app" / "crafty.py").exists())

    # --- the heap a person types is the heap the server gets (brief item 1) ---------------
    import os
    from app import heap as heaps
    from app import specs
    os.environ["HOST_RAM_GB"] = "16"
    try:
        m = MockEngine(seed=False)
        engine.configure(drivers={"mock": m})
        big = await engine.create_server(name="Big", port=25590,
                                         memory_mb=specs.container_memory_mb(10),
                                         mc_version="1.20.1", loader="forge")
        await engine.update_server(big, {"execution_command":
                                         "java @user_jvm_args.txt @libraries/x/unix_args.txt nogui"})
        await engine.write_file(big, "user_jvm_args.txt", specs.render_jvm_args([], 10))
        host = await specs.host_for(big)
        sized = await heaps.resize(big, 10240, by_user=True, mods=420, host=host)
        rec = await engine.get_server(big)
        args = await engine.read_file(big, "user_jvm_args.txt")
        check("a 10 GB heap typed for a 420-mod pack on a 16 GB node is the heap it gets",
              "-Xmx10G" in args or "-Xmx10240M" in args, args)
        check("...in a container sized for that heap and those mods",
              rec["memory_mb"] == specs.container_memory_mb(10, 420) == sized["memory_mb"]
              and rec["memory_mb"] > 10240 + 1024 and heaps.set_by_user(rec), rec)
        check("...with no warning: it fits this machine", sized["warnings"] == [], sized)
        source = (ROOT / "app" / "installer.py").read_text(encoding="utf-8")
        check("the install uses the heap typed or the pack's, never the host's free figure",
              'heap = mem_max or memory["heap_gb"]' not in source
              and "heap = ram_max" in source)
    finally:
        os.environ.pop("HOST_RAM_GB", None)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
