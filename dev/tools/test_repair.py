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
import time
import io
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import diagnostics, engine, repair, smoketest  # noqa: E402
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


# Beyond Depth, as it really died: the error names no mod; only Mixin's
# debug trail says whose mixin was being applied.
MIXIN_LATEST = """[12Oct2026 07:30:12.168] [main/ERROR] [net.minecraftforge.fml.loading.RuntimeDistCleaner/DISTXFORM]: Attempted to load class net/minecraft/client/player/LocalPlayer for invalid dist DEDICATED_SERVER
[12Oct2026 07:30:12.172] [main/INFO] [STDERR/]: [java.lang.Throwable:printStackTrace:-1]: Caused by: org.spongepowered.asm.mixin.transformer.throwables.MixinTransformerError: An unexpected critical error was encountered
[12Oct2026 07:30:12.174] [main/INFO] [STDERR/]: [java.lang.Throwable:printStackTrace:-1]: Caused by: org.spongepowered.asm.mixin.throwables.ClassMetadataNotFoundException: net.minecraft.client.player.LocalPlayer
"""
MIXIN_DEBUG = """[12Oct2026 07:30:12.160] [main/DEBUG] [mixin/]: Mixing MixinForgeBlock from openpartiesandclaims.forge.mixins.json into net.minecraft.world.level.block.Block
[12Oct2026 07:30:12.163] [main/DEBUG] [mixin/]: Mixing DynamicCrosshairBaseBlock from dynamiccrosshair.mixins.json into net.minecraft.world.level.block.Block
[12Oct2026 07:30:12.168] [main/ERROR] [net.minecraftforge.fml.loading.RuntimeDistCleaner/DISTXFORM]: Attempted to load class net/minecraft/client/player/LocalPlayer for invalid dist DEDICATED_SERVER
"""


# Beyond Depth's next death: GML (the Groovy language provider) loading a
# Groovy mod pulls a client class; no mod's own frame is in the trace.
GML_CRASH = """[12Oct2026 08:23:56.169] [main/ERROR] [net.minecraft.server.Main/FATAL]: Failed to start the minecraft server
java.lang.RuntimeException: Attempted to load class com/mojang/blaze3d/vertex/PoseStack for invalid dist DEDICATED_SERVER
\tat net.minecraftforge.fml.loading.RuntimeDistCleaner.processClassWithFlags(RuntimeDistCleaner.java:56) ~[fmlloader-1.20.1-47.4.22.jar%2369!/:1.0]
\tat cpw.mods.cl.ModuleClassLoader.loadClass(ModuleClassLoader.java:135) ~[securejarhandler-2.1.10.jar:?]
\tat org.codehaus.groovy.reflection.stdclasses.CachedSAMClass.getDeclaredMethods(CachedSAMClass.java:113) ~[groovy-4.0.13.jar%231324!/:4.0.13]
\tat org.groovymc.gml.internal.ModExtensionLoader$_setup_closure1.doCall(ModExtensionLoader.groovy:36) ~[gml-4.0.11-all.jar%23755!/:4.0.11]
\tat org.groovymc.gml.internal.GMLLangLoader.loadMod(GMLLangLoader.groovy:34) ~[gml-4.0.11-all.jar%23755!/:4.0.11]
\tat net.minecraftforge.fml.ModLoader.buildModContainerFromTOML(ModLoader.java:291) ~[fmlcore-1.20.1-47.4.22.jar%231015!/:?]
\tat net.minecraft.server.Main.main(Main.java:125) ~[server-1.20.1-20230612.114412-srg.jar%231014!/:?]
"""


# BMC4's fourth boot: a client mixin that never applied on the server.
CAST_CRASH = """[12Oct2026 08:33:10.119] [main/FATAL] [net.minecraftforge.fml.DeferredWorkQueue/LOADING]: Synchronous work queue completed exceptionally in 649.2 ms, see suppressed exceptions for details:
java.lang.RuntimeException: null
	Suppressed: java.lang.ClassCastException: class net.minecraft.world.effect.MobEffect cannot be cast to class net.lopymine.pe.utils.PEStatusEffect (net.minecraft.world.effect.MobEffect is in module minecraft@1.20.1 of loader 'TRANSFORMER' @38293d23; net.lopymine.pe.utils.PEStatusEffect is in module particle_effects@1.6.0+1.20.1+forge of loader 'TRANSFORMER' @38293d23)
		at net.lopymine.pe.utils.StatusEffectUtils.swapParticle(StatusEffectUtils.java:17) ~[ParticleEffects-1.6.0+1.20.1+forge.jar%23708!/:?]
		at net.minecraftforge.fml.DeferredWorkQueue.makeRunnable(DeferredWorkQueue.java:76) ~[fmlcore-1.20.1-47.4.20.jar%23818!/:?]
"""


def mixin_jar(mod_id: str, common: list, client: list) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/mods.toml",
                   'modLoader="javafml"\nloaderVersion="[47,)"\n[[mods]]\n'
                   f'modId="{mod_id}"\nversion="1.0.0"\n')
        z.writestr(f"{mod_id}.mixins.json", json.dumps(
            {"required": True, "package": f"x.{mod_id}.mixin", "mixins": common,
             "client": client}))
    return buf.getvalue()


def groovy_jar(mod_id: str, depends_on_gml: bool = True) -> bytes:
    buf = io.BytesIO()
    dep = (f'[[dependencies.{mod_id}]]\nmodId="gml"\nmandatory=true\n'
           f'versionRange="[4,)"\nside="BOTH"\n') if depends_on_gml else ""
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/mods.toml",
                   'modLoader="gml"\nloaderVersion="[4,)"\n[[mods]]\n'
                   f'modId="{mod_id}"\nversion="1.0.0"\n' + dep)
    return buf.getvalue()


# Beyond Depth, round 6: two content mods and two client mods failed alike.
DIST_BLOCKS = """-- MOD create --
Details:
\tMod File: /server/mods/create-1.20.1-6.0.8.jar
\tFailure message: Create (create) has failed to load correctly
\t\tjava.lang.RuntimeException: Attempted to load class com/mojang/blaze3d/vertex/PoseStack for invalid dist DEDICATED_SERVER
-- MOD sodiumextras --
Details:
\tMod File: /server/mods/sodiumextras-forge-1.0.7-1.20.1.jar
\tFailure message: Sodium Extras (sodiumextras) has failed to load correctly
\t\tjava.lang.RuntimeException: Attempted to load class net/minecraft/client/Options for invalid dist DEDICATED_SERVER
"""


def content_jar(mod_id: str, needs: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        dep = (f'[[dependencies.{mod_id}]]\nmodId="{needs}"\nmandatory=true\n'
               'versionRange="[0,)"\nside="BOTH"\n') if needs else ""
        z.writestr("META-INF/mods.toml",
                   'modLoader="javafml"\nloaderVersion="[47,)"\n[[mods]]\n'
                   f'modId="{mod_id}"\nversion="1.0.0"\n' + dep)
        z.writestr(f"data/{mod_id}/recipes/thing.json", "{}")
    return buf.getvalue()


def jar_with(mod_id: str, *entries: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/mods.toml",
                   f'modLoader="javafml"\nloaderVersion="[47,)"\n[[mods]]\n'
                   f'modId="{mod_id}"\nversion="1.0.0"\ndisplayName="{mod_id}"\n')
        for e in entries:
            z.writestr(e, "{}")
    return buf.getvalue()


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

    # --- a client mod's mixin, named only by Mixin's debug trail --------------------------------
    async def mixin_case(files):
        _, sid = await server_with(files)
        await engine.ensure_dir(sid, "logs")
        await engine.write_file(sid, "logs/debug.log", MIXIN_DEBUG)
        trail = await diagnostics.mixin_context(sid, MIXIN_LATEST)
        disk = repair.Disk(sid)
        await disk.refresh()
        acts, why = await repair.decide(sid, MIXIN_LATEST + "\n" + trail, mc="1.20.1",
                                        loader="forge", disk=disk, tried=set())
        return trail, acts, why

    trail, actions, _ = await mixin_case({
        "dynamiccrosshair-7.8+1.20.1-forge.jar": jar_with("dynamiccrosshair",
                                                          "dynamiccrosshair.mixins.json"),
        "openpartiesandclaims-forge-1.20.1.jar": jar_with("openpartiesandclaims")})
    check("a mixin crash that names no mod is traced through debug.log to its config",
          "dynamiccrosshair.mixins.json" in trail and "LocalPlayer" in trail, trail)
    check("...and the client mod owning that config is disabled",
          [(a["do"], a.get("file")) for a in actions]
          == [("disable", "dynamiccrosshair-7.8+1.20.1-forge.jar")], actions)
    trail, actions, why = await mixin_case({
        "dynamiccrosshair-7.8+1.20.1-forge.jar": jar_with("dynamiccrosshair")})
    check("a jar matched by name that does not hold the config is left alone",
          actions == [] and any("dynamiccrosshair.mixins.json" in w for w in why),
          (actions, why))

    # --- a crash inside a language provider ------------------------------------------------------
    culprits = diagnostics.attribute_crash(GML_CRASH, ["gml-4.0.11-all.jar",
                                                       "eatinganimation-1.20.1-5.1.0.jar"])
    check("Groovy and its mod loader are never blamed for a mod they were loading",
          culprits == [], culprits)
    actions, _ = await decide_for({
        "gml-4.0.11-all.jar": forge_jar("gml"),
        "eatinganimation-1.20.1-5.1.0.jar": groovy_jar("eatinganimation"),
        "oculus-mc1.20.1-1.8.0.jar": forge_jar("oculus"),
        "create-1.20.1-0.5.1.jar": forge_jar("create")}, GML_CRASH, loader="forge")
    check("the client-side mod GML was loading is disabled -- not GML, not other client mods",
          [(a["do"], a.get("file")) for a in actions]
          == [("disable", "eatinganimation-1.20.1-5.1.0.jar")], actions)
    actions, why = await decide_for({
        "gml-4.0.11-all.jar": forge_jar("gml"),
        "somegroovycontent-1.0.jar": groovy_jar("somegroovycontent")}, GML_CRASH,
        loader="forge")
    check("...and with no client-side Groovy mod, nothing is disabled on a guess",
          actions == [] and any("gml-4.0.11-all.jar" in w for w in why), (actions, why))
    actions, _ = await decide_for({
        "gml-4.0.11-all.jar": forge_jar("gml"),
        "eatinganimation-1.20.1-5.1.0.jar.disabled": groovy_jar("eatinganimation"),
        "create-1.20.1-0.5.1.jar": forge_jar("create")}, GML_CRASH, loader="forge")
    check("with its client mod already off and nothing else loading through it, the "
          "crashing provider itself is disabled",
          [(a["do"], a.get("file")) for a in actions]
          == [("disable", "gml-4.0.11-all.jar")], actions)

    # --- a client mixin that never applied ---------------------------------------------------------
    actions, _ = await decide_for({
        "ParticleEffects-1.6.0+1.20.1+forge.jar": mixin_jar(
            "particle_effects", [], ["StatusEffectMixin", "ParticleMixin"]),
        "create-1.20.1-0.5.1.jar": forge_jar("create")}, CAST_CRASH, loader="forge")
    check("a game class cast to a mod's own interface: its client-only mixins are why, "
          "and it is disabled",
          [(a["do"], a.get("file")) for a in actions]
          == [("disable", "ParticleEffects-1.6.0+1.20.1+forge.jar")], actions)
    actions, why = await decide_for({
        "ParticleEffects-1.6.0+1.20.1+forge.jar": mixin_jar(
            "particle_effects", ["ServerSideMixin"], ["ParticleMixin"])},
        CAST_CRASH, loader="forge")
    check("...but a mod with mixins that do apply on a server is reported, not disabled",
          actions == [], (actions, why))

    # --- killed for memory -------------------------------------------------------------------------
    import os as _os
    import re as _re
    from app import specs as _specs

    class OOMUnlessRoom(MockEngine):
        """Prominence II on the test server: OOM-killed with a 5 GB heap in a
        6 GB container. Boots once the container has room for the JVM around
        its heap and its 300 mods."""
        async def power(self, server_id, action):
            await super().power(server_id, action)
            if action != "start":
                return
            rec = self.servers[server_id]
            heap = int(_re.search(r"-Xmx(\d+)M", rec.get("execution_command") or "").group(1))
            if rec["memory_mb"] < heap + _specs.container_overhead_mb(heap, 300):
                self._put(server_id, "logs", "latest.log",
                          b"[main/INFO]: Reloading YIGD graveyard data (server)\n")
                self.stats[server_id].update(running=False, crashed=True,
                                             last_exit={"code": 137, "oom": True})

    async def big_pack(by_user: bool):
        oom = OOMUnlessRoom(seed=False)
        engine.configure("mock", drivers={"local": oom})
        sid = await engine.create_server(name="Big pack", port=25702, memory_mb=6144,
                                         mc_version="1.20.1", loader="fabric",
                                         limits={"heap_set_by_user": by_user})
        await engine.update_server(sid, {"execution_command":
                                         "java -Xms5120M -Xmx5120M -XX:+UseG1GC -jar s.jar nogui"})
        await engine.write_manifest(sid, {"schema": 1, "minecraft": "1.20.1",
                                          "loader": "fabric"})
        for i in range(300):
            await engine.upload_file(sid, "mods", f"mod{i:03d}-1.0.jar", fabric_jar(f"mod{i:03d}"))
        result = await repair.boot_and_repair(Job("t", "t"), sid, timeout=60)
        return sid, result, await engine.get_server(sid)

    _os.environ["HOST_RAM_GB"] = "16"
    try:
        sid, result, server = await big_pack(by_user=True)
        check("an OOM kill grows the container around the same heap, and the next boot "
              "comes up", result["outcome"] == "ready"
              and "-Xmx5120M" in server["execution_command"]
              and [r["do"] for r in result["repairs"]] == ["grow_container"]
              and server["memory_mb"] == 5120 + 2048, (result, server["memory_mb"]))
        check("...one step: a quarter more room, at least 1 GB, blaming no mod",
              "6144 MB memory limit" in result["repairs"][0]["why"]
              and "heap stays 5120 MB" in result["repairs"][0]["why"], result["repairs"])

        _os.environ["HOST_RAM_GB"] = "6"
        sid, result, server = await big_pack(by_user=True)
        check("on a machine with nothing more to give, a heap a person set is left alone",
              result["outcome"] == "failed" and "-Xmx5120M" in server["execution_command"]
              and server["memory_mb"] == 6144 and not result["repairs"], result)
        check("...and it says so", "set by a person" in (result.get("why") or "")
              and "no more to give" in (result.get("why") or ""), result.get("why"))

        sid, result, server = await big_pack(by_user=False)
        check("only a heap BlessForge chose itself is lowered to fit, and the next boot "
              "comes up", result["outcome"] == "ready"
              and "-Xmx4608M" in server["execution_command"]
              and [r["do"] for r in result["repairs"]] == ["fit_heap"], (result, server))
        check("...the repair saying BlessForge chose it",
              "BlessForge chose the 5120 MB heap itself" in result["repairs"][0]["why"],
              result["repairs"])
    finally:
        _os.environ.pop("HOST_RAM_GB", None)

    # --- a boot that waits for a slot on its node has not exited ---------------------------
    class QueuedFirst(MockEngine):
        """Two other servers are booting on the node: this one waits its turn."""
        polls = 0

        async def power(self, server_id, action):
            await super().power(server_id, action)
            if action == "start":
                self.held = dict(self.stats[server_id])
                self.stats[server_id].update(running=False, state="queued")
                self._put(server_id, "logs", "latest.log", b"")     # nothing yet

        async def get_stats(self, server_id):
            QueuedFirst.polls += 1
            if QueuedFirst.polls == 3 and getattr(self, "held", None):
                self.stats[server_id].update(self.held, state="running")
                self._put(server_id, "logs", "latest.log",
                          b'[Server thread/INFO]: Done (1.0s)! For help, type "help"\n')
            return await super().get_stats(server_id)

    queued = QueuedFirst(seed=False)
    engine.configure("mock", drivers={"local": queued})
    sid = await engine.create_server(name="Queued", port=25706, memory_mb=4096,
                                     mc_version="1.20.1", loader="fabric")
    await engine.update_server(sid, {"execution_command": "java -jar s.jar nogui"})
    from app import smoketest
    verdict = await smoketest.run(Job("t", "t"), sid, timeout=60)
    check("a boot that waits in its node's queue first is not called exited",
          verdict["outcome"] == "ready", verdict.get("outcome"))

    # --- a client class in a content mod's failure is not proof --------------------------------
    actions, why = await decide_for({
        "create-1.20.1-6.0.8.jar": content_jar("create"),
        "copycats-3.0.10.jar": content_jar("copycats", needs="create"),
        "sodiumextras-forge-1.0.7-1.20.1.jar": forge_jar("sodiumextras")},
        DIST_BLOCKS, loader="forge")
    check("a content mod others need is never disabled because its failure names a "
          "client class; the client mod beside it is",
          [(a["do"], a.get("file")) for a in actions]
          == [("disable", "sodiumextras-forge-1.0.7-1.20.1.jar")]
          and any("needed by copycats" in w for w in why), (actions, why))

    # --- a boot that dies without a recognised failure line ------------------------------------
    class DiesOnBoot(MockEngine):
        async def power(self, server_id, action):
            await super().power(server_id, action)
            if action == "start":
                self._put(server_id, "logs", "latest.log", MIXIN_LATEST.encode())
                self.stats[server_id].update(running=False, crashed=True)

    dies = DiesOnBoot(seed=False)
    engine.configure("mock", drivers={"local": dies})
    sid = await engine.create_server(name="Dies", port=25701, memory_mb=4096,
                                     mc_version="1.20.1", loader="forge")
    await engine.update_server(sid, {"execution_command": "java -jar server.jar nogui"})
    t0 = time.time()
    result = await smoketest.run(Job("t", "t"), sid, timeout=120)
    check("a server that exits is a failed boot at the next look, not after the timeout",
          result["outcome"] == "failed" and time.time() - t0 < 20, result["outcome"])
    check("...quoting the error it died on",
          "ClassMetadataNotFoundException" in (result["failure_line"] or ""),
          result["failure_line"])

    # --- slow is not dead; silent after an error is hung -------------------------------------------
    class Writes(MockEngine):
        """A boot that logs a fatal-looking line and then either keeps
        working (2BROKE4LORE: "Preparing spawn area") or goes quiet (BMC4)."""
        script: list = []

        async def power(self, server_id, action):
            await super().power(server_id, action)
            if action != "start":
                return
            self.stats[server_id].update(running=True, crashed=False)

            async def feed():
                lines = []
                for item in self.script:
                    await asyncio.sleep(item[0])
                    lines.append(item[1])
                    self._put(server_id, "logs", "latest.log",
                              ("\n".join(lines) + "\n").encode())
            asyncio.get_running_loop().create_task(feed())

    old_quiet, old_hung = smoketest.FATAL_QUIET_S, smoketest.HUNG_AFTER_ERROR_S
    smoketest.FATAL_QUIET_S, smoketest.HUNG_AFTER_ERROR_S = 8, 8
    try:
        slow = Writes(seed=False)
        slow.script = [(0, "[main/INFO]: Starting"),
                       (1, "[main/FATAL] [mixin/]: something it survives")] + [
            (3, f"[Worker-Main-5/INFO]: Preparing spawn area: {p}%") for p in range(0, 100, 20)
        ] + [(2, '[Server thread/INFO]: Done (61.2s)! For help, type "help"')]
        engine.configure("mock", drivers={"local": slow})
        sid = await engine.create_server(name="Slow", port=25703, memory_mb=4096,
                                         mc_version="1.20.1", loader="forge")
        await engine.update_server(sid, {"execution_command": "java -jar s.jar nogui"})
        result = await smoketest.run(Job("t", "t"), sid, timeout=120)
        check("a boot still logging after a fatal-looking line is not killed for it",
              result["outcome"] == "ready", result["outcome"])

        hung = Writes(seed=False)
        hung.script = [(0, "[main/INFO]: Starting"),
                       (1, "[main/ERROR] [net.minecraft.resources.RegistryDataLoader/]: "
                           "Registry loading errors:"),
                       (0, "Caused by: java.lang.IllegalStateException: Failed to load "
                           "registries due to above errors")]
        engine.configure("mock", drivers={"local": hung})
        sid = await engine.create_server(name="Hung", port=25704, memory_mb=4096,
                                         mc_version="1.20.1", loader="forge")
        await engine.update_server(sid, {"execution_command": "java -jar s.jar nogui"})
        t0 = time.time()
        result = await smoketest.run(Job("t", "t"), sid, timeout=120)
        check("a boot that goes quiet after an exception is called hung, not waited out",
              result["outcome"] == "failed" and time.time() - t0 < 40
              and "Failed to load registries" in (result["failure_line"] or ""),
              (result["outcome"], result["failure_line"]))
    finally:
        smoketest.FATAL_QUIET_S, smoketest.HUNG_AFTER_ERROR_S = old_quiet, old_hung

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
