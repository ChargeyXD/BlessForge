#!/usr/bin/env python3
"""Boot-and-repair: only what a boot proves, never on a guess, never twice.

The crash texts here are the real ones from the pack runs on a node
(2026-10-12): Prominence II's Fabric resolver, Beyond Depth and 2BROKE4LORE's
Forge "requires ... Currently, ... is not installed", BMC4's Oculus. The jars
are real zips with real metadata, so the provider/side checks read what a
loader would.

  * a missing library that sits disabled is re-enabled;
  * ...unless it is client-side, when the mod that needs it is disabled;
  * one that is not on disk is installed from CurseForge, then Modrinth;
  * one that cannot be had at all costs the dependent mod, as a last resort;
  * a client-side mod the crash names is disabled; a content mod it names is
    left alone and reported;
  * the wrong Java is one fault, fixed once;
  * no action is ever repeated, and the loop stops when there is nothing to do;
  * the repairs are recorded in the manifest.

Offline: the in-memory engine; the catalogue lookups are stubbed.
"""
import asyncio
import io
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import engine, repair, smoketest  # noqa: E402
from app.engine.mock import MockEngine  # noqa: E402
from app.jobs import Job  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


def fabric_jar(mod_id: str, environment: str = "*", depends: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({
            "schemaVersion": 1, "id": mod_id, "version": "1.0.0", "name": mod_id,
            "environment": environment, "depends": depends or {}}))
    return buf.getvalue()


def forge_jar(mod_id: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/mods.toml",
                   f'modLoader="javafml"\nloaderVersion="[47,)"\n[[mods]]\n'
                   f'modId="{mod_id}"\nversion="1.0.0"\ndisplayName="{mod_id}"\n')
    return buf.getvalue()


PROMINENCE = """[main/ERROR]: Incompatible mods found!
net.fabricmc.loader.impl.FormattedException: Some of your mods are incompatible with the game or each other!
More details:
\t - Mod 'Colorwheel' (colorwheel) 1.3.0+mc1.20.1 requires version 1.7.0 or later of iris, which is missing!
\t - Mod 'Forge Config Screens' (forgeconfigscreens) 8.0.2 requires any version of modmenu, which is missing!
"""
BEYOND = """-- Mod loading issue for: simplybows --
Details:
\tMod file: /server/mods/simplybows-forge-neoforge-0.1.4.jar
\tFailure message: Mod simplybows requires simplytooltips 0 or above
\t\tCurrently, simplytooltips is not installed
"""
BROKE = """Mod ars_elemental requires ars_nouveau 1.20.1-4.9.0 or above
Currently, ars_nouveau is not installed
"""
OCULUS = """-- MOD oculus --
Details:
\tMod File: /server/mods/oculus-mc1.20.1-1.8.0.jar
\tFailure message: Oculus has class loading errors
\t\torg.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError
\tException message: java.lang.RuntimeException: Attempted to load class net/minecraft/client/renderer/LevelRenderer for invalid dist DEDICATED_SERVER
"""
CONTENT = """-- MOD create --
Details:
\tMod File: /server/mods/create-1.20.1-0.5.1.jar
\tFailure message: Create has failed to load correctly
\tException message: java.lang.NullPointerException: registry entry is null
"""
JAVA = """Cobblemon (cobblemon) is missing a feature it requires to run
It requires javaVersion 21 or above and below 22, but 17 is available
"""


async def server_with(files: dict[str, bytes]) -> tuple[MockEngine, str]:
    mock = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": mock})
    sid = await engine.create_server(name="Repair test", port=25700, memory_mb=4096,
                                     mc_version="1.20.1", loader="fabric")
    for name, blob in files.items():
        await engine.upload_file(sid, "mods", name, blob)
    await engine.update_server(sid, {"execution_command": "java -jar server.jar nogui"})
    await engine.write_manifest(sid, {"schema": 1, "minecraft": "1.20.1", "loader": "fabric",
                                      "mods": [{"file": f"mods/{n}"} for n in files]})
    return mock, sid


async def decide_for(files: dict[str, bytes], text: str, tried=None, loader="fabric"):
    _, sid = await server_with(files)
    disk = repair.Disk(sid)
    await disk.refresh()
    return await repair.decide(sid, text, mc="1.20.1", loader=loader, disk=disk,
                               tried=tried or set())


async def main():
    found = {}

    async def fake_find(mod_id, *, mc, loader):
        return found.get(mod_id)

    repair.find_library = fake_find

    # --- Fabric: libraries that are client-side ------------------------------------
    actions, _ = await decide_for({
        "colorwheel-1.3.0.jar": fabric_jar("colorwheel", depends={"iris": ">=1.7.0"}),
        "iris-1.7.0.jar.disabled": fabric_jar("iris", "client"),
        "forgeconfigscreens-8.0.2.jar": fabric_jar("forgeconfigscreens"),
        "modmenu-7.2.2.jar.disabled": fabric_jar("modmenu", "client"),
        "lithium-0.11.jar": fabric_jar("lithium"),
    }, PROMINENCE)
    got = {(a["do"], a.get("file")) for a in actions}
    check("a mod needing a client-side library is disabled, not the library enabled",
          got == {("disable", "colorwheel-1.3.0.jar"),
                  ("disable", "forgeconfigscreens-8.0.2.jar")}, got)
    check("...both found in one boot (Fabric lists them together)", len(actions) == 2)

    # --- a disabled server-side library comes back ------------------------------------
    actions, _ = await decide_for({
        "simplybows-forge-neoforge-0.1.4.jar": forge_jar("simplybows"),
        "simplytooltips-1.1.jar.disabled": forge_jar("simplytooltips"),
    }, BEYOND, loader="forge")
    check("a disabled library that is not client-side is re-enabled",
          [(a["do"], a.get("file")) for a in actions]
          == [("enable", "simplytooltips-1.1.jar.disabled")],
          [(a["do"], a.get("file")) for a in actions])

    # --- a library not on disk is installed ------------------------------------------
    found["simplytooltips"] = {"do": "install", "source": "curseforge", "project_id": 777,
                               "file_id": 888, "name": "Simply Tooltips"}
    actions, _ = await decide_for({
        "simplybows-forge-neoforge-0.1.4.jar": forge_jar("simplybows")}, BEYOND, loader="forge")
    check("a library that is not on disk is installed from the catalogue",
          actions and actions[0]["do"] == "install" and actions[0]["project_id"] == 777
          and "simplybows needs simplytooltips" in actions[0]["why"], actions)

    # --- ...and one that cannot be had costs the dependent, loudly ---------------------
    actions, _ = await decide_for({
        "ars_elemental-1.20.1-0.6.7.9.jar": forge_jar("ars_elemental")}, BROKE, loader="forge")
    check("a library nobody may download costs the mod that needs it, as a last resort",
          actions and actions[0]["do"] == "disable"
          and actions[0]["file"] == "ars_elemental-1.20.1-0.6.7.9.jar"
          and actions[0].get("last_resort"), actions)
    check("...and says how to bring it back", actions and "Upload" in actions[0]["why"])

    # --- client-side crash culprit ---------------------------------------------------------
    actions, _ = await decide_for({
        "oculus-mc1.20.1-1.8.0.jar": forge_jar("oculus"),
        "create-1.20.1-0.5.1.jar": forge_jar("create")}, OCULUS, loader="forge")
    check("a client-side mod the crash names is disabled",
          [(a["do"], a.get("file")) for a in actions]
          == [("disable", "oculus-mc1.20.1-1.8.0.jar")], actions)

    actions, unresolved = await decide_for({
        "create-1.20.1-0.5.1.jar": forge_jar("create")}, CONTENT, loader="forge")
    check("a content mod the crash names is NOT disabled on a guess", actions == [], actions)
    check("...it is reported instead", any("create" in u for u in unresolved), unresolved)

    # --- Java ---------------------------------------------------------------------------
    actions, _ = await decide_for({"x-1.0.jar": forge_jar("x")}, JAVA, loader="forge")
    check("the wrong Java is one fault, fixed by setting Java 21",
          len(actions) == 1 and actions[0]["do"] == "set_java" and actions[0]["major"] == 21,
          actions)

    # --- nothing is tried twice ------------------------------------------------------------
    actions, _ = await decide_for({
        "oculus-mc1.20.1-1.8.0.jar": forge_jar("oculus")}, OCULUS,
        tried={("disable", "oculus-mc1.20.1-1.8.0.jar")}, loader="forge")
    check("an action already tried is not planned again", actions == [], actions)

    # --- found on BMC4 in round three -------------------------------------------------------
    probe = ("[12Oct2026 06:20:01.000] [modloading-worker-0/WARN] [some.Mod/]: probe\n"
             "java.lang.NoClassDefFoundError: net/minecraft/client/ParticleStatus\n"
             "\tat thedarkcolour.kotlinforforge.K.x(K.java:1) ~[kotlinforforge-4.12.0-all.jar%2390!/:?]\n"
             "\tat com.example.coolmod.Cool.init(Cool.java:2) ~[coolmod-1.0.jar%2391!/:?]\n")
    files = {"kotlinforforge-4.12.0-all.jar": forge_jar("kotlinforforge"),
             "coolmod-1.0.jar": forge_jar("coolmod")}
    actions, _ = await decide_for(files, probe, loader="forge")
    check("a client class a mod probed for and survived (logged at WARN) blames nobody",
          actions == [], actions)
    actions, _ = await decide_for(files, probe.replace("/WARN]", "/FATAL]"), loader="forge")
    check("a fatal one blames the mod under the Kotlin library, never the library",
          [(a["do"], a.get("file")) for a in actions] == [("disable", "coolmod-1.0.jar")],
          actions)
    actions, _ = await decide_for({
        "fzzy_config-1.0.jar": forge_jar("fzzy_config"),
        "kotlinforforge-4.12.0-all.jar.disabled": forge_jar("kotlinforforge")},
        "Mod fzzy_config requires kotlinforforge 4.0 or above\n"
        "Currently, kotlinforforge is not installed\n",
        tried={("disable", "kotlinforforge-4.12.0-all.jar")}, loader="forge")
    check("a jar this session disabled is not re-enabled (no flip-flop)",
          not any(a["do"] == "enable" for a in actions), actions)

    # --- the loop -------------------------------------------------------------------------------
    mock, sid = await server_with({
        "simplybows-forge-neoforge-0.1.4.jar": forge_jar("simplybows"),
        "simplytooltips-1.1.jar.disabled": forge_jar("simplytooltips"),
    })
    await engine.write_manifest(sid, {"schema": 1, "minecraft": "1.20.1", "loader": "forge",
                                      "mods": [{"file": "mods/simplytooltips-1.1.jar"}]})
    boots = {"n": 0}
    real_run = smoketest.run

    async def fake_run(job, server_id, *, timeout=300, stop_when_done=True):
        boots["n"] += 1
        names = set(await engine.list_dir(server_id, "mods"))
        ok = "simplytooltips-1.1.jar" in names
        await engine.ensure_dir(server_id, "logs")
        await engine.write_file(server_id, "logs/latest.log",
                                'Done (12.3s)! For help, type "help"\n' if ok else BEYOND)
        return {"outcome": "ready" if ok else "failed", "seconds": 12 if ok else 30,
                "findings": [], "failure_line": None if ok else "Mod simplybows requires"}

    smoketest.run = fake_run
    try:
        result = await repair.boot_and_repair(Job("t", "t"), sid)
    finally:
        smoketest.run = real_run
    check("the loop boots, repairs, and boots again until it is up",
          result["outcome"] == "ready" and result["rounds"] == 2 and boots["n"] == 2, result)
    check("...reporting the one repair it made",
          [(r["do"], r["file"]) for r in result["repairs"]]
          == [("enable", "simplytooltips-1.1.jar.disabled")], result["repairs"])
    manifest = await engine.read_manifest(sid)
    check("...and records it in the manifest, for the Mods and Diagnose tabs",
          manifest.get("repairs") and manifest["repairs"][0]["do"] == "enable")

    # A boot that keeps failing with nothing provable stops at once, not after 8 tries.
    mock, sid = await server_with({"create-1.20.1-0.5.1.jar": forge_jar("create")})

    async def always_fails(job, server_id, *, timeout=300, stop_when_done=True):
        boots["n"] += 1
        await engine.ensure_dir(server_id, "logs")
        await engine.write_file(server_id, "logs/latest.log", CONTENT)
        return {"outcome": "failed", "seconds": 20, "findings": []}

    boots["n"] = 0
    smoketest.run = always_fails
    try:
        result = await repair.boot_and_repair(Job("t", "t"), sid)
    finally:
        smoketest.run = real_run
    check("a failure with nothing provable to fix stops after one boot, saying why",
          result["outcome"] == "failed" and boots["n"] == 1 and "create" in (result["why"] or ""),
          result)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
