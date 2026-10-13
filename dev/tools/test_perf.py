#!/usr/bin/env python3
"""The performance reading (app/perf.py): faults are faults, taste is taste.

  * JVM arguments are read from where the server really takes them -- the
    command, and user_jvm_args.txt when the command reads it;
  * a flag Java removed is a fault on that Java and only a warning on an
    older one; two collectors, a heap that does not fit its container, a
    minimum above the maximum, a debugger left on -- all faults;
  * tidy settings produce nothing at all;
  * the YAML editor changes one key in place, comments kept, and never adds
    one; same-named keys in different sections are told apart;
  * optimisation mods are recognised by their jars, disabled ones ignored;
  * applying a fix edits the setting where it lives;
  * the optimisation mods' own switches are offered only for mods that are
    there and keys their files really have, and JSON keeps its types;
  * spark's console report is read as spark really prints it.

Offline: the in-memory engine.
"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import engine, perf  # noqa: E402
from app.engine.mock import MockEngine  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


def levels(issues):
    return {(i["level"], i["title"].split(" ")[0]) for i in issues}


async def main():
    # --- where arguments come from -----------------------------------------------------
    a = perf.jvm_args("java @user_jvm_args.txt @libraries/x/unix_args.txt nogui",
                      "# managed\n-Xms4G\n-Xmx6G -XX:+UseG1GC\n")
    check("a Forge command's arguments come from user_jvm_args.txt",
          [(x["arg"], x["from"]) for x in a["args"]] == [
              ("-Xms4G", "user_jvm_args.txt"), ("-Xmx6G", "user_jvm_args.txt"),
              ("-XX:+UseG1GC", "user_jvm_args.txt")], a["args"])
    a = perf.jvm_args('"/opt/java/bin/java" -Xmx2G -jar paper.jar --nogui', None)
    check("a jar command's arguments stop at -jar",
          [x["arg"] for x in a["args"]] == ["-Xmx2G"], a["args"])

    # --- faults -------------------------------------------------------------------------
    def run(cmd, **kw):
        return perf.check_jvm(perf.jvm_args(cmd, None)["args"], **kw)

    issues = run("java -Xmx4G -XX:+UseConcMarkSweepGC -jar s.jar", java=17, container_mb=6000,
                 cpus=4, mc="1.20.1")
    check("CMS stops Java 17 starting: a fault", any(i["level"] == "bad" and "CMS" in
                                                      i["detail"] for i in issues))
    issues = run("java -Xmx4G -XX:+UseConcMarkSweepGC -jar s.jar", java=8, container_mb=6000,
                 cpus=4, mc="1.12.2")
    check("...but on Java 8 only a warning", any(i["level"] == "warn" and "CMS" in i["detail"]
                                                for i in issues)
          and not any(i["level"] == "bad" and "CMS" in i["detail"] for i in issues))
    issues = run("java -Xmx4G -XX:+UseG1GC -XX:+UseZGC -jar s.jar", java=21,
                 container_mb=6000, cpus=4)
    check("two collectors at once: a fault", any("Two garbage collectors" in i["title"]
                                                for i in issues))
    issues = run("java -Xmx6G -jar s.jar", java=21, container_mb=6200, cpus=4)
    check("a heap with no room left in its container: a fault, fixed by keeping the "
          "heap and resizing the container",
          any(i["level"] == "bad" and "does not fit" in i["title"] and
              i["fix"]["set_heap_mb"] == 6144 for i in issues))
    issues = run("java -Xms8G -Xmx4G -jar s.jar", java=21, container_mb=9000, cpus=4)
    check("a minimum heap above the maximum: a fault",
          any("minimum heap is bigger" in i["title"] for i in issues))
    issues = run("java -Xmx4G -agentlib:jdwp=transport=dt_socket,server=y -jar s.jar",
                 java=21, container_mb=6000, cpus=4)
    check("a debugger left on: a fault", any("debugger" in i["title"] for i in issues))
    issues = run("java -Xmx4G -XX:ParallelGCThreads=16 -jar s.jar", java=21,
                 container_mb=6000, cpus=4)
    check("more GC threads than CPUs: flagged, with a count that fits",
          any("ParallelGCThreads" in i["title"] and i["fix"]["replace_arg"][1].endswith("=3")
              for i in issues))
    issues = run("java -Xmx4G -XX:+UseG1GC -jar s.jar", java=17, container_mb=6000, cpus=4,
                 mc="1.16.5")
    check("an old Minecraft without the Log4Shell mitigation is flagged",
          any("Log4Shell" in i["title"] for i in issues))
    issues = run("java -Xms5G -Xmx5G -XX:+UseG1GC -XX:MaxGCPauseMillis=200 -jar s.jar",
                 java=21, container_mb=6400, cpus=8, mc="1.21.1")
    check("tidy settings produce nothing", issues == [], issues)

    # --- YAML --------------------------------------------------------------------------
    y = ("world-settings:\n  default:\n    merge-radius:\n      item: 2.5 # a comment\n"
         "    entity-activation-range:\n      animals: 32\n"
         "    entity-tracking-range:\n      animals: 48\n")
    check("a key is read by its path",
          perf.yaml_get(y, ("world-settings", "default", "entity-tracking-range", "animals"))
          == "48")
    out = perf.yaml_set(y, ("world-settings", "default", "entity-activation-range", "animals"),
                        "16")
    check("one key changes; its namesake in another section does not",
          "      animals: 16\n    entity-tracking-range:\n      animals: 48" in out)
    out = perf.yaml_set(y, ("world-settings", "default", "merge-radius", "item"), "3.5")
    check("the comment on the line survives", "item: 3.5 # a comment" in out, out)
    check("a key that is not there is never added",
          perf.yaml_set(y, ("world-settings", "default", "nope"), "1") is None)

    # --- optimisation mods ------------------------------------------------------------------
    found = perf.detect_mods(["lithium-fabric-mc1.20.1-0.11.2.jar", "ferritecore-6.0.1.jar",
                              "c2me-fabric-mc1.20.1.jar.disabled", "create-0.5.1.jar",
                              "Chunky-1.3.146.jar", "spark-1.10.53-fabric.jar"])
    check("optimisation mods are recognised by their jars, disabled ones ignored",
          sorted(m["key"] for m in found) == ["chunky", "ferritecore", "lithium", "spark"],
          [m["key"] for m in found])

    # --- applying, where each setting lives ---------------------------------------------------
    mock = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": mock})
    sid = await engine.create_server(name="Perf", port=25800, memory_mb=6000,
                                     mc_version="1.20.1", loader="forge")
    await engine.update_server(sid, {"execution_command":
                                     "java @user_jvm_args.txt @libraries/x/unix_args.txt nogui"})
    await engine.write_file(sid, "user_jvm_args.txt",
                            "-Xmx8G\n-XX:+UseG1GC\n-XX:+AggressiveOpts\n")
    await engine.write_manifest(sid, {"schema": 1, "minecraft": "1.20.1", "loader": "forge"})
    reading = await perf.analyse(sid)
    fixes = [i["fix"] for i in reading["jvm"]["issues"] if i.get("fix")]
    result = await perf.apply(sid, jvm_fixes=fixes, yaml_ids=[])
    text = await engine.read_file(sid, "user_jvm_args.txt")
    check("fixes are written into user_jvm_args.txt, where those flags live",
          "AggressiveOpts" not in text and "-Xmx8G" not in text and "-XX:+UseG1GC" in text,
          text)
    check("...and the command is left alone",
          (await engine.get_server(sid))["execution_command"]
          == "java @user_jvm_args.txt @libraries/x/unix_args.txt nogui")
    check("...reporting what changed", result["restart_needed"] and result["applied"],
          result)

    # --- the optimisation mods' switches ------------------------------------------------
    j = '{\n  "version": 2,\n  "continueOnRestart": false,\n  "updateInterval": 1\n}\n'
    check("a JSON key is read by its path", perf.json_get(j, ("continueOnRestart",)) == "false")
    out = perf.json_set(j, ("continueOnRestart",), "true")
    check("...set keeping its type (a boolean stays a boolean)",
          out is not None and '"continueOnRestart": true' in out
          and '"updateInterval": 1' in out, out)
    check("...and a JSON key that is not there is never added",
          perf.json_set(j, ("nope",), "1") is None)

    sc = ("features:\n  lobotomize-villagers:\n    enabled: false\n    tick-interval: 20\n"
          "dynamic:\n  enabled: false\n  target-mspt: 35\n")
    for d in ("config/servercore", "config/chunky", "mods"):
        await engine.ensure_dir(sid, d)
    await engine.write_file(sid, "config/servercore/config.yml", sc)
    await engine.write_file(sid, "config/chunky/config.json", j)
    offered = await perf.mod_tuning(sid, {"chunky"})
    check("switches are offered only for mods that are present",
          [x["file"] for x in offered] == ["config/chunky/config.json"], offered)
    offered = await perf.mod_tuning(sid, {"servercore", "chunky"})
    check("...with the mod's name and the keys its file has",
          {(x["mod"], x["key"]) for x in offered} == {
              ("ServerCore", "dynamic.enabled"),
              ("ServerCore", "features.lobotomize-villagers.enabled"),
              ("Chunky", "continueOnRestart")}, offered)
    await engine.write_file(sid, "mods/servercore-fabric-1.5.jar", "x")
    await engine.write_file(sid, "mods/Chunky-1.3.146.jar", "x")
    reading = await perf.analyse(sid)
    ids = [x["id"] for x in reading["mod_settings"] if x["key"] in
           ("dynamic.enabled", "continueOnRestart")]
    result = await perf.apply(sid, jvm_fixes=[], yaml_ids=ids)
    sc_now = await engine.read_file(sid, "config/servercore/config.yml")
    ch_now = await engine.read_file(sid, "config/chunky/config.json")
    check("applying writes each switch in its own file and nothing else",
          "dynamic:\n  enabled: true" in sc_now
          and "lobotomize-villagers:\n    enabled: false" in sc_now
          and '"continueOnRestart": true' in ch_now and len(result["applied"]) == 2,
          (sc_now, ch_now, result))

    # --- spark's numbers, as spark 1.10 prints them -------------------------------------
    from app import spark
    real = [
        "[13:19:08] [spark-worker-pool-1-thread-1/INFO]: [⚡] TPS from last 5s, 10s, 1m, 5m, 15m:",
        "[13:19:08] [spark-worker-pool-1-thread-1/INFO]: [⚡]  *20.0, 19.2, *20.0, *20.0, *20.0",
        "[13:19:08] [spark-worker-pool-1-thread-1/INFO]: [⚡] Tick durations (min/med/95%ile/max ms) from last 10s, 1m:",
        "[13:19:08] [spark-worker-pool-1-thread-1/INFO]: [⚡]  0.6/0.8/4.2/8.2;  0.6/2.4/31.0/215.0",
        "[13:19:08] [spark-worker-pool-1-thread-1/INFO]: [⚡] CPU usage from last 10s, 1m, 15m:",
        "[13:19:08] [spark-worker-pool-1-thread-1/INFO]: [⚡]  7%, 27%, 27%  (system)",
        "[13:19:08] [spark-worker-pool-1-thread-1/INFO]: [⚡]  1%, 18%, 18%  (process)",
        "[13:19:09] [spark-worker-pool-1-thread-2/INFO]: Memory usage:",
        "[13:19:09] [spark-worker-pool-1-thread-2/INFO]:     1.2 GB / 4.0 GB   (30%)",
    ]
    n = spark.parse_health(real)
    check("spark's TPS, tick times, CPU and heap are all read",
          n.get("tps", {}).get("10s") == 19.2 and n.get("mspt", {}).get("p95") == 4.2
          and n.get("mspt_1m", {}).get("max") == 215.0 and n.get("cpu_process") == 1.0
          and n.get("memory_mb") == {"used": 1229, "max": 4096, "percent": 30.0}, n)

    # --- the node, not the panel, is what a heap is sized against ------------------------
    from app import specs as _specs

    class NodeMock(MockEngine):
        async def _call(self, method, path, timeout=10, **kw):
            return {"node": "n1", "ram_bytes": 8 * 1024 ** 3,
                    "ram_available_bytes": 5 * 1024 ** 3, "cpu_threads": 4, "load_1m": 2.0}

    engine.configure("mock", drivers={"n1": NodeMock(seed=False)})
    host = await _specs.host_for()
    check("heap sizing reads the node's own memory and cores",
          host["total_ram_gb"] == 8.0 and host["available_ram_gb"] == 5.0
          and host["cpu_count"] == 4 and host["load_per_core"] == 0.5
          and host["source"] == "node n1", host)

    # --- the heap is the user's, and the container follows it (brief item 1) ------------
    import os as _os
    from app import heap as heaps
    from app import optimizer
    _os.environ["HOST_RAM_GB"] = "16"
    try:
        mock = MockEngine(seed=False)
        engine.configure("mock", drivers={"local": mock})
        sid = await engine.create_server(name="Tune", port=25801, memory_mb=4096,
                                         mc_version="1.20.1", loader="fabric")
        await engine.update_server(sid, {"execution_command":
                                         "java -Xms3G -Xmx3G -jar fabric.jar nogui"})
        await engine.write_manifest(sid, {"schema": 1, "minecraft": "1.20.1",
                                          "loader": "fabric"})
        await engine.ensure_dir(sid, "mods")
        for i in range(120):
            await engine.write_file(sid, f"mods/m{i}.jar", "x")
        res = await optimizer.apply(sid, {"heap_gb": 10, "flags": []})
        server = await engine.get_server(sid)
        want = _specs.container_memory_mb(10, 120)
        check("changing the heap in Tune resizes the container around it",
              "-Xmx10240M" in server["execution_command"] and server["memory_mb"] == want
              and res["memory"]["memory_mb"] == want, (server["memory_mb"], want, res))
        check("...and records that a person chose it",
              heaps.set_by_user(server) and server["limits"]["heap_mb"] == 10240)
        res = await optimizer.apply(sid, {"heap_gb": 14, "flags": []})
        server = await engine.get_server(sid)
        check("a heap near the machine's total is set as asked, with a plain warning",
              "-Xmx14336M" in server["execution_command"] and res["warnings"]
              and "This machine has 16 GB" in res["warnings"][0], res.get("warnings"))
        refused = False
        try:
            await optimizer.apply(sid, {"heap_gb": 17, "flags": []})
        except RuntimeError as e:
            refused = "this machine has 16 GB" in str(e)
        check("only a heap bigger than the whole machine is refused", refused)
        refused = False
        try:
            await heaps.set_heap(sid, 256, by_user=True)
        except ValueError:
            refused = True
        check("...or one under 512 MB", refused)
        res = await perf.apply(sid, jvm_fixes=[{"set_heap_mb": 6144}], yaml_ids=[])
        server = await engine.get_server(sid)
        check("a heap set from the Performance fixes resizes the container too",
              "-Xmx6144M" in server["execution_command"]
              and server["memory_mb"] == _specs.container_memory_mb(6, 120)
              and any("container resized" in a for a in res["applied"]), res)
        out = await heaps.set_heap(sid, 8192, by_user=False)
        server = await engine.get_server(sid)
        check("set_heap writes -Xmx and the container together, and who chose it",
              "-Xmx8192M" in server["execution_command"]
              and server["memory_mb"] == out["memory_mb"] == _specs.container_memory_mb(8, 120)
              and not heaps.set_by_user(server), out)
        plan = await optimizer.build_plan(sid)
        check("the Tune plan carries what the Memory control needs",
              (plan["container"]["memory_mb"] == server["memory_mb"]
                               and plan["container"]["overhead"]["mod_count"] == 120),
              plan.get("container"))
    finally:
        _os.environ.pop("HOST_RAM_GB", None)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
