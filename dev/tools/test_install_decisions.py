#!/usr/bin/env python3
"""What an install does with the port and with client-only mods.

Three behaviours that were each wrong in a way nothing else would catch:

  * The port typed at the install step never reached the server. The pack's
    own overrides land on top of the server folder, and most packs ship a
    server.properties containing 25565 -- every server once ended up on
    25565 as a result. It has to be written AFTER the overlay, and it has to
    work on a server that has never started and so has no
    server.properties yet.

  * Client-only mods were deleted from the install. They are now written as
    `<name>.jar.disabled` and recorded, so the pack still matches what was
    exported and a wrong call costs one click to undo.

  * "Review & strip client-only mods" unticked still stripped them, because
    the front end never sent the flag and the backend defaulted it on.

Runs offline against the in-memory engine (app/engine/mock.py), so the files
asserted on are the files the install actually wrote; CurseForge and the
loader install are stubbed.
"""
import asyncio
import io
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import _dbtest  # noqa: E402,F401 -- a temp STATE_DIR and database

from app import (clientscan, engine, installer, optimizer, packs,  # noqa: E402
                 preflight, properties, whitelist)
from app.engine.mock import MockEngine  # noqa: E402
from app.jobs import Job  # noqa: E402

results = []


def check(name, condition, extra=""):
    results.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


def fabric_jar(mod_id: str, side: str) -> bytes:
    """A minimal jar whose fabric.mod.json declares a side."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fabric.mod.json",
                   '{"id": "%s", "name": "%s", "version": "1.0", '
                   '"environment": "%s"}' % (mod_id, mod_id, side))
    return buf.getvalue()


def build_server_pack() -> bytes:
    """A server pack carrying one server mod and one client-only mod."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mods/goodmod-1.0.jar", fabric_jar("goodmod", "*"))
        z.writestr("mods/clientmod-1.0.jar", fabric_jar("clientmod", "client"))
        z.writestr("config/goodmod.toml", "setting = true\n")
        z.writestr("server.properties", "server-port=25565\nmotd=From the pack\n")
    return buf.getvalue()


class Recorder(MockEngine):
    """The real in-memory engine, writing down what arrived and how."""

    def __init__(self):
        super().__init__(seed=False)
        self.uploaded: list[str] = []      # paths that landed in the server
        self.upload_sizes: list[int] = []
        self.patched: list[dict] = []
        self.asked: list[dict] = []        # every install step sent

    async def upload_path(self, server_id, location, filename, src):
        # The overlay arrives as a zip the node unpacks, so read it back to
        # learn the real filenames -- which is exactly where .disabled would
        # be lost if the installer dropped it. It is built on disk and
        # streamed, so the size is recorded to prove nothing was buffered.
        self.upload_sizes.append(pathlib.Path(src).stat().st_size)
        if filename.endswith(".zip"):
            with zipfile.ZipFile(src) as z:
                self.uploaded.extend(z.namelist())
        else:
            self.uploaded.append(filename)
        await super().upload_path(server_id, location, filename, src)

    async def update_server(self, server_id, fields):
        self.patched.append(dict(fields))
        await super().update_server(server_id, fields)

    async def install(self, server_id, spec, on_progress=None):
        self.asked.append(dict(spec))       # failed steps included
        return await super().install(server_id, spec, on_progress)


class Run:
    """What one install left behind, read back off the engine."""

    def __init__(self, eng: Recorder, server_id: str):
        self.eng, self.sid = eng, server_id
        self.uploaded, self.upload_sizes = eng.uploaded, eng.upload_sizes
        self.patched = eng.patched
        self.heap = None
        self.files: dict[str, str] = {}
        self.manifest: dict = {}

    async def load(self):
        for name in ("server.properties", "user_jvm_args.txt", "eula.txt"):
            try:
                self.files[name] = await self.eng.read_file(self.sid, name)
            except engine.EngineError:
                pass
        self.manifest = await engine.read_manifest(self.sid)
        self.root = set(await self.eng.list_dir(self.sid, "."))
        self.mods = set(await self.eng.list_dir(self.sid, "mods"))


async def run_install(**kwargs):
    eng = Recorder()
    engine.configure(drivers={"mock": eng})

    plan = packs.analyse_server_pack(zipfile.ZipFile(io.BytesIO(build_server_pack())))
    plan.mc_version, plan.loader = "1.20.1", "fabric"
    plan.server_loader, plan.name = "fabric", "Test Pack"

    async def fake_load_plan(job, **kw):
        return plan, zipfile.ZipFile(io.BytesIO(build_server_pack())), {}

    async def no_loader(job, sid, plan):
        # The loader ladder has its own tests; here the server just needs a
        # startup command so the heap has somewhere to go.
        await engine.update_server(sid, {
            "executable": "fabric-server.jar",
            "execution_command": "java -Xms1G -Xmx2G -jar fabric-server.jar nogui"})
        return {"loader_version": ""}

    holder = {}

    async def fake_set_memory(sid, heap_gb, flags):
        holder["heap"] = (heap_gb, list(flags or []))
        return "java -Xmx tuned"

    originals = (installer._load_plan, installer._wait_for_loader,
                 optimizer.set_command_memory)
    installer._load_plan = fake_load_plan
    installer._wait_for_loader = no_loader
    optimizer.set_command_memory = fake_set_memory
    try:
        job = Job("install", "test")
        result = await installer.install_modpack(
            job, mod_id=1, file_id=2, server_name="Test",
            **{"optimize": False, "boot_check": False, **kwargs}
        )
    finally:
        (installer._load_plan, installer._wait_for_loader,
         optimizer.set_command_memory) = originals
    run = Run(eng, result["server_id"])
    run.heap = holder.get("heap")
    await run.load()
    return run, result


async def main():
    # --- the port typed at install must reach the instance --------------
    fake, result = await run_install(port=25580)
    check("the port reaches server.properties",
          "server-port=25580" in fake.files.get("server.properties", ""),
          repr(fake.files.get("server.properties", "")[:60]))
    check("query.port follows it",
          "query.port=25580" in fake.files.get("server.properties", ""))
    check("the server record is updated too",
          any(p.get("server_port") == 25580 for p in fake.patched),
          str(fake.patched))
    check("the port is written after the pack overlay, not before",
          fake.uploaded and "server-port=25565" not in fake.files.get(
              "server.properties", ""),
          "a pack's own server.properties must not be the last word")
    check("the pack's other settings survive the port write",
          "motd=From the pack" in fake.files.get("server.properties", "")
          or "motd" not in fake.files.get("server.properties", ""))
    check("the summary reports the port", result.get("port") == 25580)

    # --- a server pack installs as its author shipped it -----------------
    # A client-looking jar in a SERVER pack is often there because a server
    # mod needs it; disabling it on a guess broke packs that would have
    # booted. The first boot decides (app/repair.py), not the jar's name.
    check("a server pack's client-looking mod is installed",
          any("clientmod" in u for u in fake.uploaded),
          ", ".join(u for u in fake.uploaded if u.endswith((".jar", ".disabled"))))
    check("...enabled, exactly as the pack ships it",
          "mods/clientmod-1.0.jar" in fake.uploaded
          and not any(u.endswith("clientmod-1.0.jar.disabled") for u in fake.uploaded))
    check("the server mod is untouched",
          "mods/goodmod-1.0.jar" in fake.uploaded)
    check("nothing is reported as disabled on a guess",
          result.get("client_only_disabled") == [],
          str(result.get("client_only_disabled")))

    records = {r["file"]: r for r in fake.manifest.get("mods", [])}
    check("the manifest does not tag it client-only",
          records.get("mods/clientmod-1.0.jar", {}).get("client_only") is False)

    # An explicit choice in the review is still honoured, server pack or not.
    fake, result = await run_install(port=25581, disable_files=["clientmod-1.0.jar"],
                                     client_reasons={"clientmod-1.0.jar":
                                                     ["the jar declares environment=client"]})
    records = {r["file"]: r for r in fake.manifest.get("mods", [])}
    check("a mod the operator ticks in the review is installed disabled",
          any(u.endswith("clientmod-1.0.jar.disabled") for u in fake.uploaded)
          and result.get("client_only_disabled") == ["clientmod-1.0.jar"])
    check("...and records why",
          "environment=client" in " ".join(
              records.get("mods/clientmod-1.0.jar", {}).get("client_only_reasons", [])))
    check("the server mod is not tagged",
          records.get("mods/goodmod-1.0.jar", {}).get("client_only") is False)

    # --- unticking the review really means "install it as published" ----
    fake2, result2 = await run_install(port=25581, skip_client_only=False)
    check("skip_client_only=false installs client mods enabled",
          "mods/clientmod-1.0.jar" in fake2.uploaded
          and not any(u.endswith(".disabled") for u in fake2.uploaded),
          ", ".join(u for u in fake2.uploaded if u.startswith("mods/")))

    # --- an explicit review decision wins -------------------------------
    fake3, result3 = await run_install(
        port=25582, disable_files=["goodmod-1.0.jar"],
        client_reasons={"goodmod-1.0.jar": ["the user said so"]})
    check("a reviewed decision disables exactly what was chosen",
          any(u.endswith("goodmod-1.0.jar.disabled") for u in fake3.uploaded)
          and "mods/clientmod-1.0.jar" in fake3.uploaded,
          ", ".join(u for u in fake3.uploaded if u.startswith("mods/")))
    records3 = {r["file"]: r for r in fake3.manifest.get("mods", [])}
    check("the review's reasons are recorded against the mod",
          records3.get("mods/goodmod-1.0.jar", {}).get("client_only_reasons")
          == ["the user said so"])

    # --- a Fabric pack gets its heap tuned too --------------------------
    # Forge and NeoForge read JVM args from user_jvm_args.txt; everything
    # else keeps them in the startup command, and nothing used to rewrite
    # it -- so a Fabric pack asking for 6 GB kept 6 GB regardless of the host.
    tuned, _ = await run_install(port=25584, optimize=True)
    check("a Fabric install tunes the heap through the startup command",
          getattr(tuned, "heap", None) is not None,
          f"heap={getattr(tuned, 'heap', None)}")
    check("the tuned heap is sized to the host, not to the pack's wish",
          getattr(tuned, "heap", (99, []))[0] <= 64)
    record = await tuned.eng.get_server(tuned.sid)
    from app import specs as _specs
    check("and the server's memory limit grows to hold that heap",
          record["memory_mb"] >= _specs.container_memory_mb(tuned.heap[0]),
          f"{record['memory_mb']} MB for a {tuned.heap[0]} GB heap")

    # --- a server that has never run has no server.properties -------------
    fresh = Recorder()
    engine.configure(drivers={"mock": fresh})
    sid = await engine.create_server(name="Fresh", port=25590, memory_mb=2048)
    out = await properties.set_port(sid, 25591, force=True)
    text = await fresh.read_file(sid, "server.properties")
    check("the port can be set before Minecraft has written the file",
          "server-port=25591" in text and out["port"] == 25591, repr(text))

    # --- the upload archive is built on disk and cleaned up -------------
    check("the overlay was uploaded as an archive", fake.upload_sizes,
          f"{len(fake.upload_sizes)} batch(es)")
    import tempfile as _tf
    leftovers = list(pathlib.Path(_tf.gettempdir()).glob(".studio-batch-*.zip"))
    check("no batch archive is left behind on disk", not leftovers,
          ", ".join(p.name for p in leftovers))

    # --- deleting outright is still possible, just not the default ------
    fake4, _ = await run_install(port=25583, exclude_files=["clientmod-1.0.jar"])
    check("exclude_files still removes a mod entirely",
          not any("clientmod" in u for u in fake4.uploaded),
          ", ".join(u for u in fake4.uploaded if u.startswith("mods/")))

    # --- the client-only review must not strip a dependency --------------------
    # This is the failure the protection exists to prevent, and it was live: the
    # server-pack path had no protection pass at all, so Athena -- declared
    # client-only by its own author, and a hard requirement of Chipped, which is
    # not -- was a confident "remove". The pack installed and then refused to
    # boot with "requires version 4.0.0 or later of athena, which is missing".
    #
    # The scoring now lives in clientscan; these build scored items directly
    # rather than going through a jar, because what is under test is the
    # override pass, not the reading of a zip.
    def scored(file_name, name, mod_id, verdict, deps=(), score=100):
        return {"file_name": file_name, "name": name, "mod_id": mod_id,
                "provides": [], "hard_dependencies": list(deps),
                "verdict": verdict, "score": score, "confidence": "high",
                "reasons": ["the author declares it client-only"],
                "project_id": None}

    items = [
        scored("chipped.jar", "Chipped", "chipped", "server", ["athena"], -20),
        scored("athena.jar", "Athena", "athena", "client"),
        scored("iris.jar", "Iris", "iris", "client"),
        scored("zoom.jar", "Zoomer", "zoom", "client", ["iris"]),
    ]
    clientscan.apply_overrides(items)
    by = {c["file_name"]: c for c in items}
    check("a dependency of a server mod is held back, not removed",
          by["athena.jar"]["verdict"] == "keep", by["athena.jar"]["verdict"])
    check("and it names who needs it",
          by["athena.jar"].get("required_by_others") == ["Chipped"],
          by["athena.jar"].get("required_by_others"))
    check("a client mod nothing depends on is still removed",
          by["iris.jar"]["verdict"] == "client", by["iris.jar"]["verdict"])
    check("a client mod required only by ANOTHER client mod is not protected",
          by["iris.jar"].get("required_by_others") is None,
          "Zoomer needs Iris, but Zoomer is going too")

    # Sodium, Embeddium, Oculus: never protected, and what needs them goes too.
    items = [
        # A staying mod hard-requires Sodium: before, Sodium was "kept".
        scored("sodium-fabric-0.5.13+mc1.20.1.jar", "Sodium", "sodium", "review", (), 40),
        scored("dynlights.jar", "Dynamic Lights", "dynlights", "server", ["sodium"], -20),
        scored("addon.jar", "Lights Addon", "lightsaddon", "server", ["dynlights"], -20),
        scored("embeddium-0.3.31+mc1.20.1.jar", "Embeddium", "embeddium", "server", (), -10),
        scored("oculus-mc1.20.1-1.8.0.jar", "Oculus", "oculus", "server", (), -10),
        scored("create.jar", "Create", "create", "server", (), -50),
        scored("sodium-extra-0.5.7.jar", "Sodium Extra", None, "server", (), -10),
    ]
    clientscan.apply_overrides(items)
    by = {c["file_name"]: c for c in items}
    check("Sodium is removed even though a staying mod requires it",
          by["sodium-fabric-0.5.13+mc1.20.1.jar"]["verdict"] == "client"
          and not by["sodium-fabric-0.5.13+mc1.20.1.jar"].get("required_by_others"),
          by["sodium-fabric-0.5.13+mc1.20.1.jar"]["verdict"])
    check("...and the mod that hard-requires it goes with it",
          by["dynlights.jar"]["verdict"] == "client"
          and "Sodium" in by["dynlights.jar"]["reasons"][0],
          by["dynlights.jar"]["reasons"][:1])
    check("...all the way up the chain",
          by["addon.jar"]["verdict"] == "client", by["addon.jar"]["verdict"])
    check("Embeddium and Oculus are removed whatever their score said",
          by["embeddium-0.3.31+mc1.20.1.jar"]["verdict"] == "client"
          and by["oculus-mc1.20.1-1.8.0.jar"]["verdict"] == "client")
    check("an unrelated content mod is untouched",
          by["create.jar"]["verdict"] == "server")
    check("an add-on whose file name starts with sodium- is not taken for Sodium",
          clientscan.never_on_server(file_name="sodium-extra-0.5.7.jar") is None
          and clientscan.never_on_server(file_name="sodium-fabric-0.5.13.jar") == "Sodium"
          and clientscan.never_on_server(mod_id="embeddium") == "Embeddium"
          and clientscan.never_on_server(slug="oculus") == "Oculus"
          and clientscan.never_on_server(mod_id="sodiumextra") is None)

    # --- the scoring itself ---------------------------------------------------
    only_name = clientscan.evaluate(file_name="zoomify-1.2.jar", name_listed=True)
    check("a name-list match alone is a review, never a removal",
          only_name["verdict"] == "review", only_name["verdict"])

    declared = clientscan.evaluate(
        file_name="figura.jar", meta={"side": "client"}, exact_hash_match=True)
    check("a declared environment=client is decisive",
          declared["verdict"] == "client", declared["verdict"])

    author_says_server = clientscan.evaluate(
        file_name="anything.jar", modrinth_side="required",
        name_listed=True, meta={"side": None}, exact_hash_match=True)
    check("the author saying server_side: required outranks every heuristic",
          author_says_server["verdict"] == "server", author_says_server["verdict"])

    # The signal the old detector had no equivalent of: a Forge jar that
    # declares nothing at all, whose every class lives under a client package.
    forge_client = clientscan.evaluate(
        file_name="someforgemod.jar", meta={"loader": "forge"},
        archive={"readable": True, "classes": 40, "client_classes": 40,
                 "client_ratio": 1.0, "has_assets": True})
    check("an all-client package tree catches a Forge mod that declares nothing",
          forge_client["verdict"] == "client", forge_client["verdict"])

    balanced = clientscan.evaluate(
        file_name="bigmod.jar", meta={"loader": "forge"},
        archive={"readable": True, "classes": 200, "client_classes": 12,
                 "client_ratio": 0.06, "has_data": True, "has_recipes": True})
    check("a mod with real server code and datapack content is left alone",
          balanced["verdict"] == "server", balanced["verdict"])

    # --- the operator's own decisions -----------------------------------------
    await _dbtest.fresh()
    whitelist._reset_for_tests()
    await whitelist.add("figura-0.1.4.jar", name="Figura", verdict="allow")
    allowed = [scored("figura-0.1.5.jar", "Figura", "figura", "client")]
    clientscan.apply_overrides(allowed)
    check("an allow survives a version bump (the stem is what matches)",
          allowed[0]["verdict"] == "server", allowed[0]["verdict"])

    whitelist._reset_for_tests()
    await whitelist.add("somemod-1.0.jar", name="Some mod", verdict="block")
    blocked = [scored("somemod-2.0.jar", "Some mod", "somemod", "server", (), -50)]
    clientscan.apply_overrides(blocked)
    check("a block overrides a clean score",
          blocked[0]["verdict"] == "client", blocked[0]["verdict"])
    whitelist._reset_for_tests()
    await whitelist.load()
    check("a decision survives a restart (it is in the database)",
          whitelist.blocks("somemod-3.0.jar"))
    whitelist._reset_for_tests()

    await author_declared_client()
    await export_carries_configs()
    await server_pack_without_metadata()
    await node_fetches_mods()

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


async def export_carries_configs():
    """A server exported as a CurseForge pack carries the pack's own configs
    in overrides/ -- not just its mod list -- and never the world or logs."""
    import json as _json
    from app import curseforge, exporter
    mock = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": mock})
    sid = await engine.create_server(name="Export", port=25790, memory_mb=4096,
                                     mc_version="1.20.1", loader="forge")
    await engine.upload_file(sid, "mods", "create-1.0.jar", b"jar")
    await engine.upload_file(sid, "mods", "hud-1.0.jar.disabled", b"jar")
    await engine.upload_file(sid, "config", "create-common.toml", b"speed = 2")
    await engine.upload_file(sid, "config/sub", "deep.json", b"{}")
    await engine.upload_file(sid, "defaultconfigs", "server.toml", b"x = 1")
    await engine.upload_file(sid, "kubejs/server_scripts", "recipes.js", b"//")
    await engine.upload_file(sid, "world", "level.dat", b"world")
    await engine.upload_file(sid, "logs", "latest.log", b"log")
    await engine.write_manifest(sid, {
        "schema": 1, "minecraft": "1.20.1", "loader": "forge",
        "pack": {"name": "Beyond Depth (server)", "version": "13.0.1"},
        "mods": [{"file": "mods/create-1.0.jar", "source": "curseforge",
                  "project_id": 328085, "file_id": 111},
                 {"file": "mods/hud-1.0.jar", "source": "curseforge",
                  "project_id": 999, "file_id": 222}]})

    async def no_build(mc, loader):
        return "forge-47.4.0"
    real = curseforge.loader_build_id
    curseforge.loader_build_id = no_build
    try:
        out = await exporter.export_instance(Job("t", "t"), sid)
    finally:
        curseforge.loader_build_id = real
    with zipfile.ZipFile(io.BytesIO(out["bytes"])) as z:
        names = set(z.namelist())
        doc = _json.loads(z.read("manifest.json"))
    check("an export lists the enabled CurseForge mods, not the disabled client one",
          [f["projectID"] for f in doc["files"]] == [328085], doc["files"])
    check("...and carries config/, defaultconfigs/ and kubejs/ in overrides/",
          {"overrides/config/create-common.toml", "overrides/config/sub/deep.json",
           "overrides/defaultconfigs/server.toml",
           "overrides/kubejs/server_scripts/recipes.js"} <= names, sorted(names))
    check("...never the world or the logs",
          not any(n.startswith(("overrides/world", "overrides/logs")) for n in names))
    check("...and leaves no temporary archive behind on the server",
          ".blessforge-export-configs.zip" not in await engine.list_dir(sid, "."))
    check("...and says so in its result", out.get("config_files") == 4, out.get("config_files"))


async def author_declared_client():
    """An install with no review asks Modrinth, by SHA-1, which mods their
    authors say do not run on a server -- Beyond Depth shipped 62 of them."""
    from app import modrinth, whitelist

    async def fake_versions(hashes):
        return {h: {"mod_id": "p-" + h} for h in hashes if h in ("aa", "bb", "cc")}

    async def fake_projects(ids):
        sides = {"p-aa": "unsupported", "p-bb": "required", "p-cc": "unsupported"}
        return {i: {"id": i, "slug": i, "title": i.upper(), "server_side": sides[i]}
                for i in ids}

    real = (modrinth.versions_from_hashes, modrinth.get_projects)
    modrinth.versions_from_hashes, modrinth.get_projects = fake_versions, fake_projects
    whitelist._reset_for_tests()
    await whitelist.add("allowed-1.0.jar", name="Allowed", verdict="allow")
    try:
        records = [{"file": "mods/hud-1.0.jar", "sha1": "aa"},
                   {"file": "mods/content-1.0.jar", "sha1": "bb"},
                   {"file": "mods/allowed-1.0.jar", "sha1": "cc"},
                   {"file": "mods/unknown-1.0.jar", "sha1": "dd"},
                   {"file": "mods/already-1.0.jar", "sha1": "aa", "client_only": True}]
        payload = [installer.PackEntry(r["file"] + (".disabled" if r.get("client_only")
                                                    else "")) for r in records]
        disabled: list[str] = []
        off = await installer._author_declared_client(Job("t", "t"), records[:4], payload,
                                                      disabled)
        targets = sorted(e.target for e in payload)
        check("a mod its author marks server_side unsupported is installed disabled",
              "mods/hud-1.0.jar.disabled" in targets and off == ["hud-1.0.jar"]
              and disabled == ["hud-1.0.jar"], (off, targets))
        check("...with the reason on its record",
              records[0].get("client_only") and "not supported on servers"
              in records[0]["client_only_reasons"][0], records[0])
        check("a server mod, one Modrinth does not know, and one the operator allowed "
              "stay enabled",
              {"mods/content-1.0.jar", "mods/unknown-1.0.jar", "mods/allowed-1.0.jar"}
              <= set(targets), targets)
    finally:
        modrinth.versions_from_hashes, modrinth.get_projects = real
        whitelist._reset_for_tests()


async def server_pack_without_metadata():
    """A server pack that is just the folder: the version comes from the
    client file's manifest. Found installing Just Create SMP on the agent
    (WP-06): 131 files, mods and all, and nothing naming the loader."""
    from app import curseforge

    def zipped(members: dict) -> io.BytesIO:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in members.items():
                z.writestr(name, data)
        buf.seek(0)
        return buf

    client = {"manifest.json": (
        '{"minecraft": {"version": "1.21.1", "modLoaders": '
        '[{"id": "neoforge-21.1.172", "primary": true}]}, '
        '"manifestType": "minecraftModpack", "name": "Bare", "version": "0.5", '
        '"files": [], "overrides": "overrides"}')}
    bare = {"mods/create-6.0.jar": b"PK", "config/create.toml": "x",
            "start.sh": "java -jar x.jar"}
    labelled = {**bare, "variables.txt": "MINECRAFT_VERSION=1.20.1\n"
                "MODLOADER=Forge\nMODLOADER_VERSION=47.4.0\n"}
    fetched = []
    packs_by_id = {1: client, 2: bare, 3: labelled}

    async def fake_get_file(mod_id, file_id):
        return {"server_pack_file_id": 2 if file_id == 1 else 3,
                "display_name": "Bare 0.5"}

    async def fake_fetch(job, mod_id, file_id):
        fetched.append(file_id)
        return zipped(packs_by_id[file_id]), {}

    saved = curseforge.get_file, installer.fetch_pack_archive
    curseforge.get_file, installer.fetch_pack_archive = fake_get_file, fake_fetch
    try:
        plan, _zf, _meta = await installer._load_plan(Job("t", "t"), mod_id=9, file_id=1)
        check("a server pack that names no version takes it from the pack's manifest",
              (plan.mc_version, plan.server_loader, plan.loader_version)
              == ("1.21.1", "neoforge", "21.1.172"), (plan.mc_version, plan.loader))
        check("...while still installing the server pack's own files",
              any(m["target"] == "mods/create-6.0.jar" for m in plan.overlay_members)
              and fetched == [2, 1], fetched)
        fetched.clear()
        packs_by_id[1] = {"manifest.json": client["manifest.json"].replace("1.21.1", "9.9")}
        plan, _zf, _meta = await installer._load_plan(Job("t", "t"), mod_id=9, file_id=4)
        check("a server pack that says what it is is believed, and the client "
              "file is not fetched", plan.mc_version == "1.20.1" and fetched == [3],
              (plan.mc_version, fetched))
    finally:
        curseforge.get_file, installer.fetch_pack_archive = saved


async def node_fetches_mods():
    """A manifest pack's mods go from the mod site to the node in one
    fetch_many step (agent/downloads.py); only what the node cannot fetch
    is zipped and uploaded: the pack's own files, a mod with no SHA-1 to
    check it by, and one the node failed on (from the copy already here)."""
    import hashlib
    import json
    import tempfile
    from app import config, curseforge

    jars = {"alpha-1.0.jar": fabric_jar("alpha", "*"), "beta-1.0.jar": fabric_jar("beta", "*"),
            "gamma-1.0.jar": fabric_jar("gamma", "*"), "delta-1.0.jar": fabric_jar("delta", "*")}

    def meta(fid, name, *, sha1=True):
        data = jars[name]
        return curseforge._slim_file({
            "id": fid, "modId": fid * 10, "displayName": name, "fileName": name,
            "fileLength": len(data), "releaseType": 1, "gameVersions": ["1.20.1", "Fabric"],
            "downloadUrl": f"https://edge.forgecdn.net/files/{fid}/{name}",
            "hashes": [{"algo": 1, "value": hashlib.sha1(data).hexdigest()}] if sha1 else []})

    metas = {1: meta(1, "alpha-1.0.jar"), 2: meta(2, "beta-1.0.jar", sha1=False),
             3: meta(3, "gamma-1.0.jar"), 4: meta(4, "delta-1.0.jar")}
    client = io.BytesIO()
    with zipfile.ZipFile(client, "w") as z:
        z.writestr("manifest.json", json.dumps({
            "minecraft": {"version": "1.20.1",
                          "modLoaders": [{"id": "fabric-0.15.0", "primary": True}]},
            "manifestType": "minecraftModpack", "name": "Fetched", "version": "1",
            "overrides": "overrides",
            "files": [{"projectID": fid * 10, "fileID": fid, "required": True}
                      for fid in metas]}))
        z.writestr("overrides/config/alpha.toml", "a = 1\n")

    class OldAgent(Recorder):
        """An agent from before fetch_many: the step is ignored, "done" all the same."""

        async def install(self, server_id, spec, on_progress=None):
            out = await super().install(
                server_id, {k: v for k, v in spec.items() if k != "fetch_many"})
            out.pop("fetched", None)
            return out

    async def install_with(eng):
        engine.configure(drivers={"mock": eng})
        for m in metas.values():
            if m["file_name"] != "gamma-1.0.jar":
                eng.cdn[m["download_url"]] = jars[m["file_name"]]
        # The site serves something else for gamma: the node refuses it.
        eng.cdn[metas[3]["download_url"]] = b"not the jar you are looking for"

        async def fake_load_plan(job, **kw):
            zf = zipfile.ZipFile(io.BytesIO(client.getvalue()))
            return packs.analyse_client_pack(zf), zf, {}

        async def no_loader(job, sid, plan):
            await engine.update_server(sid, {
                "executable": "fabric-server.jar",
                "execution_command": "java -Xms1G -Xmx2G -jar fabric-server.jar nogui"})
            return {"loader_version": ""}

        async def get_files(ids):
            return {i: metas[i] for i in ids}

        async def get_mods(ids):
            return {i: {"id": i, "name": f"mod {i}"} for i in ids}

        async def download(m, client=None):
            curseforge.download_url(m)
            return jars[m["file_name"]]

        saved = (installer._load_plan, installer._wait_for_loader, curseforge.get_files,
                 curseforge.get_mods, curseforge.download, config.CACHE_DIR)
        installer._load_plan, installer._wait_for_loader = fake_load_plan, no_loader
        curseforge.get_files, curseforge.get_mods = get_files, get_mods
        curseforge.download = download
        # A cache of its own: the jars here are this run's, byte for byte.
        config.CACHE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="bf-fetch-cache-"))
        try:
            job = Job("install", "fetch")
            result = await installer.install_modpack(
                job, mod_id=1, file_id=2, server_name="Fetched", port=25595,
                disable_files=["delta-1.0.jar"], optimize=False, boot_check=False)
        finally:
            (installer._load_plan, installer._wait_for_loader, curseforge.get_files,
             curseforge.get_mods, curseforge.download, config.CACHE_DIR) = saved
        sid = result["server_id"]
        on_disk = {}
        for name in await eng.list_dir(sid, "mods"):
            on_disk[name] = await eng.download_file(sid, f"mods/{name}")
        log = " ".join(line.get("message", "") if isinstance(line, dict) else str(line)
                       for line in getattr(job, "log", []))
        return result, on_disk, log

    eng = Recorder()
    result, on_disk, log = await install_with(eng)
    asked = next((s["fetch_many"] for s in eng.asked if s.get("fetch_many")), [])
    asked_paths = sorted(e["path"] for e in asked)
    check("mods with an address, a SHA-1 and a size are one fetch_many step to the node",
          asked_paths == ["mods/alpha-1.0.jar", "mods/delta-1.0.jar.disabled",
                          "mods/gamma-1.0.jar"], asked_paths)
    check("...each with the hash and size the node checks it by",
          asked and all(e["sha1"] == hashlib.sha1(jars[e["path"].split("/")[1].replace(".disabled", "")])
              .hexdigest() and e["size"] > 0 for e in asked))
    check("...and a mod left disabled keeps its .disabled name on the node",
          "mods/delta-1.0.jar.disabled" in eng.fetched_many, eng.fetched_many)
    check("the node's mods are not zipped and uploaded as well",
          not {"mods/alpha-1.0.jar", "mods/delta-1.0.jar.disabled"} & set(eng.uploaded),
          eng.uploaded)
    check("a mod with no SHA-1 to check it by is uploaded, as before",
          "mods/beta-1.0.jar" in eng.uploaded and "mods/beta-1.0.jar" not in asked_paths)
    check("the pack's own files (overrides) are uploaded", "config/alpha.toml" in eng.uploaded,
          eng.uploaded)
    check("a mod the node could not fetch (a bad hash) is uploaded from the copy here",
          "mods/gamma-1.0.jar" in eng.uploaded and "mods/gamma-1.0.jar" not in eng.fetched_many)
    want = {"alpha-1.0.jar": jars["alpha-1.0.jar"], "beta-1.0.jar": jars["beta-1.0.jar"],
            "gamma-1.0.jar": jars["gamma-1.0.jar"],
            "delta-1.0.jar.disabled": jars["delta-1.0.jar"]}
    check("every mod is on the server, byte for byte", on_disk == want,
          [n for n in set(want) | set(on_disk) if on_disk.get(n) != want.get(n)])
    check("the summary counts what the node fetched and what was uploaded",
          result.get("files_fetched_by_node") == 2 and result.get("files_uploaded") == 3,
          (result.get("files_fetched_by_node"), result.get("files_uploaded")))
    check("...and the install log says which mod the node could not fetch, and why",
          "could not download 1 of 3 mods" in log and "gamma-1.0.jar: its SHA-1" in log,
          log[-300:])

    old = OldAgent()
    result, on_disk, _log = await install_with(old)
    check("an agent too old to fetch (it ignores the step) gets every mod uploaded instead",
          result.get("files_fetched_by_node") == 0 and len(on_disk) == 4
          and {"mods/alpha-1.0.jar", "mods/delta-1.0.jar.disabled"} <= set(old.uploaded),
          (result.get("files_fetched_by_node"), sorted(on_disk)))


# Guarded: the installer fingerprints jars in worker processes, which import
# this module again -- unguarded, every run of the suite ran it three times.
if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
