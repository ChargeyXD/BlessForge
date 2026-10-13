#!/usr/bin/env python3
"""Server templates: seven loaders, each created, tuned and booted the same way.

What this pins down:

  * every template in templates/ loads and is well formed, and a broken
    one is refused with the file and the reason, not a KeyError later;
  * the create screen's list IS the templates -- nothing else to keep in
    step;
  * Java: what Mojang's version JSON says, rounded up to a runtime the nodes
    have (1.17 asks for 16 and runs on 17; 26.x runs on 25), and the offline
    table when Mojang cannot be reached;
  * each of the seven creates through the engine with the startup command
    its template describes, on the Java its version needs, with the EULA
    accepted, its mod/plugin folder made, the heap actually applied (a
    pre-1.17 Forge has no args file, so its heap goes on the command line),
    and a complete manifest;
  * the installer runs on the node with the template's argv and cleanup;
  * the boot test, through the engine, reads "Done" as a boot.

Offline: the in-memory engine, and the loaders' projects stubbed.
"""
import asyncio
import json
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import engine, loaders, provision, smoketest, templates  # noqa: E402
from app.engine.mock import MockEngine  # noqa: E402
from app.jobs import Job  # noqa: E402

results = []
SEVEN = {"vanilla", "paper", "purpur", "folia", "fabric", "forge", "neoforge"}


def check(name, condition, extra=""):
    results.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


def source(family: str, mc: str) -> dict:
    """What loaders.resolve would answer, without the network."""
    if family == "forge":
        build = "14.23.5.2860" if mc == "1.12.2" else "47.4.0"
        name = f"forge-{mc}-{build}-installer.jar"
        return {"url": f"https://maven.minecraftforge.net/x/{name}", "filename": name,
                "kind": "installer", "loader_version": build, "source": "MinecraftForge"}
    if family == "neoforge":
        return {"url": "https://maven.neoforged.net/x/neoforge-21.1.209-installer.jar",
                "filename": "neoforge-21.1.209-installer.jar", "kind": "installer",
                "loader_version": "21.1.209", "source": "NeoForged"}
    jar = {"vanilla": f"server-{mc}.jar", "paper": f"paper-{mc}-100.jar",
           "purpur": f"purpur-{mc}-2400.jar", "folia": f"folia-{mc}-10.jar",
           "fabric": f"fabric-server-mc.{mc}-loader.0.16.5-launcher.1.0.1.jar"}[family]
    return {"url": f"https://example.invalid/{jar}", "filename": jar, "kind": "server",
            "loader_version": "1", "source": "stub"}


MOJANG = {"versions": [{"id": v, "url": f"https://mojang.invalid/{v}.json"}
                       for v in ("1.12.2", "1.16.5", "1.17.1", "1.20.4", "1.20.5",
                                 "1.21.1", "26.1")]}
MOJANG_JAVA = {"1.12.2": 8, "1.16.5": 8, "1.17.1": 16, "1.20.4": 17, "1.20.5": 21,
               "1.21.1": 21, "26.1": 25}
online = {"up": False}


async def fake_json(url, timeout=30.0):
    if not online["up"]:
        raise RuntimeError("offline")
    if url == loaders.MOJANG_MANIFEST:
        return MOJANG
    v = url.rsplit("/", 1)[-1][:-5]
    return {"id": v, "javaVersion": {"majorVersion": MOJANG_JAVA[v]}}


async def main():
    loaders._json = fake_json
    loaders.resolve = lambda family, mc, build=None: _resolved(family, mc)

    # --- the files -------------------------------------------------------------
    tpls = templates.all_templates()
    check("all seven loaders have a template", set(tpls) == SEVEN, sorted(tpls))
    check("the create screen's list is the templates, in their order",
          [e["key"] for e in loaders.CATALOGUE] == list(tpls)
          and loaders.CATALOGUE[0]["key"] == "fabric", [e["key"] for e in loaders.CATALOGUE])
    for key, t in tpls.items():
        logo = ROOT / "app" / "static" / "img" / t["logo"].removeprefix("/assets/")
        check(f"{key}: its logo exists and its folder is the one the loader reads",
              logo.exists() and t["mod_directory"] == loaders.mod_directory(key),
              f"{logo.name} {t['mod_directory']}")
    check("Forge and NeoForge run an installer; the rest download one jar",
          {k for k, t in tpls.items() if t["install"]["fetch"] == "installer"}
          == {"forge", "neoforge"})

    with tempfile.TemporaryDirectory() as d:
        real_dir = templates.DIR
        for label, mutate in [
            ("a missing title", lambda t: t.pop("title")),
            ("an unknown install kind", lambda t: t["install"].__setitem__("fetch", "magic")),
            ("a command that is not java", lambda t: t["startup"].__setitem__(
                "command", "sh -c 'curl x | sh'")),
            ("a jar command without {jar}", lambda t: t["startup"].__setitem__(
                "command", "java -jar server.jar")),
            ("an installer without {installer}", lambda t: t["install"].__setitem__(
                "run", ["java", "-jar", "x.jar"])),
            ("a bad boot regex", lambda t: t["boot"].__setitem__("ready", "Done (")),
        ]:
            src = json.loads((real_dir / ("forge.json" if "installer" in label else
                                          "paper.json")).read_text(encoding="utf-8"))
            mutate(src)
            p = pathlib.Path(d) / f"{src['key']}.json"
            p.write_text(json.dumps(src), encoding="utf-8")
            templates.DIR = pathlib.Path(d)
            templates.all_templates.cache_clear()
            try:
                templates.all_templates()
                ok = False
            except templates.TemplateError as e:
                ok = p.name in str(e)
            check(f"a template with {label} is refused, naming the file", ok)
            p.unlink()
        templates.DIR = real_dir
        templates.all_templates.cache_clear()
    try:
        templates.get("bukkit")
        ok = False
    except ValueError as e:
        ok = "paper" in str(e)
    check("an unknown loader is refused with the list of known ones", ok)

    # --- Java ------------------------------------------------------------------
    check("a requirement rounds up to a runtime the nodes have",
          [templates.runtime_for(n) for n in (8, 11, 16, 17, 21, 22, 25, 30)]
          == [8, 17, 17, 17, 21, 25, 25, 25])
    online["up"] = True
    got = {mc: await templates.java_major("paper", mc) for mc in MOJANG_JAVA}
    check("with Mojang reachable, its javaVersion decides (16 runs on 17)",
          got == {"1.12.2": 8, "1.16.5": 8, "1.17.1": 17, "1.20.4": 17, "1.20.5": 21,
                  "1.21.1": 21, "26.1": 25}, got)
    online["up"] = False
    got = {mc: await templates.java_major("fabric", mc) for mc in MOJANG_JAVA}
    check("offline, the table gives the same answers",
          got == {"1.12.2": 8, "1.16.5": 8, "1.17.1": 17, "1.20.4": 17, "1.20.5": 21,
                  "1.21.1": 21, "26.1": 25}, got)

    # --- the startup commands -----------------------------------------------------
    check("a jar template's command names the jar and the JVM flags",
          templates.startup("paper", jar="paper.jar", jvm="-Xmx4G")
          == "java -Xmx4G -jar paper.jar nogui")
    check("a modern Forge/NeoForge command reads its flags from user_jvm_args.txt",
          templates.startup("neoforge", args_file="libraries/x/unix_args.txt")
          == "java @user_jvm_args.txt @libraries/x/unix_args.txt nogui")
    check("a pre-1.17 Forge falls back to running its jar",
          templates.startup("forge", jar="forge-1.12.2-14.23.5.2860.jar", jvm="-Xmx3G")
          == "java -Xmx3G -jar forge-1.12.2-14.23.5.2860.jar nogui")

    # --- seven creates, through the engine ------------------------------------------
    mock = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": mock})
    cases = [("vanilla", "1.21.1"), ("paper", "1.21.4"), ("purpur", "1.21.1"),
             ("folia", "1.20.6"), ("fabric", "26.1"), ("forge", "1.20.1"),
             ("forge", "1.12.2"), ("neoforge", "1.21.1")]
    port = 25600
    for family, mc in cases:
        port += 1
        job = Job("create_server", f"{family} {mc}")
        out = await provision.create_empty(job, name=f"T {family} {mc}", loader=family,
                                           mc_version=mc, port=port, mem_max=3)
        sid = out["server_id"]
        rec = await engine.get_server(sid)
        cmd = (rec.get("execution_command") or "").strip()
        want_java = templates.runtime_for(engine.required_java_major(mc))
        names = set(await engine.list_dir(sid, "."))
        manifest = await engine.read_manifest(sid)
        tpl = templates.get(family)
        if tpl["install"]["fetch"] == "installer" and mc != "1.12.2":
            shape = cmd.startswith("java @user_jvm_args.txt @libraries/") and cmd.endswith(
                "unix_args.txt nogui")
            jvm = await engine.read_file(sid, "user_jvm_args.txt")
            heap = re.search(r"-Xmx(3G|3072M)\b", jvm) is not None
        else:
            shape = re.fullmatch(r"java .*-jar \S+\.jar nogui", cmd) is not None
            heap = re.search(r"-Xmx(3G|3072M)\b", cmd) is not None
        check(f"{family} {mc}: the template's command, on Java {want_java}",
              shape and rec.get("java_major") == want_java, f"{cmd}  java={rec.get('java_major')}")
        parts = {"heap": heap, "eula": "eula.txt" in names,
                 "folder": tpl["mod_directory"] in names,
                 "complete": bool(manifest.get("complete")),
                 "loader": manifest.get("loader") == family}
        check(f"{family} {mc}: heap applied, EULA accepted, {tpl['mod_directory']}/ made, "
              "manifest complete", all(parts.values()),
              {k: v for k, v in parts.items() if not v})
        if tpl["install"]["fetch"] == "installer":
            spec = mock.installs[-1]
            check(f"{family} {mc}: the installer ran on the node with the template's argv",
                  spec["run"][:2] == ["java", "-jar"] and spec["run"][-1] == "--installServer"
                  and spec["java_major"] == want_java
                  and not any(n.endswith("-installer.jar") for n in names), spec["run"])
        boot = await smoketest.run(Job("smoke_test", "boot"), sid, timeout=30)
        check(f"{family} {mc}: the boot test through the engine reads it as booted",
              boot["outcome"] == "ready" and not (await engine.get_stats(sid)).get("running"),
              boot["outcome"])

    # --- "Optimize" off: no tuning flags, but the heap typed is still the heap -----------
    for family, mc in (("paper", "1.21.1"), ("neoforge", "1.21.1")):
        port += 1
        out = await provision.create_empty(Job("create_server", "plain"), name=f"Plain {family}",
                                           loader=family, mc_version=mc, port=port, mem_max=1,
                                           optimize=False)
        rec = await engine.get_server(out["server_id"])
        cmd = rec.get("execution_command") or ""
        jvm = (await engine.read_file(out["server_id"], "user_jvm_args.txt")
               if "@user_jvm_args.txt" in cmd else cmd)
        check(f"{family} with Optimize off: a 1 GB heap is written, not the -Xmx2G "
              "placeholder, and no tuning flags",
              re.search(r"-Xmx(1G|1024M)\b", jvm) and "-Xmx2G" not in jvm
              and "-XX:" not in jvm and rec.get("memory_mb") == 1024 + 768, (jvm[-160:],
                                                                             rec.get("memory_mb")))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


async def _resolved(family, mc):
    return source(family, mc)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
