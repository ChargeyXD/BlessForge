"""Is this server set up to run at its best? A reading of what is actually there.

The Tune tab proposes a configuration; this reads the one in place and says
what is holding it back. Four sources, all read from the server itself:

  * the JVM: every argument the server really starts with -- from the
    startup command AND user_jvm_args.txt, which a Forge/NeoForge command
    reads -- checked against this server's Java, its container's memory and
    its CPU share. A flag Java removed stops the JVM before it writes a log
    line; two collectors at once is refused; a heap bigger than the
    container is an OOM kill waiting to happen. Those are faults, not taste;
  * the pack's optimisation mods (FerriteCore, Lithium, ModernFix, Krypton,
    C2ME, ServerCore, AllTheLeaks, Memory Leak Fix, Packet Fixer, AI
    Improvements, Chunky, ThreadTweak, spark): which are present, what that
    changes about the right settings, and the actions they enable;
  * the Paper family's own configs (paper-world-defaults.yml, spigot.yml):
    the well-known safe settings, offered only where the key already exists
    in the file, and marked when they change gameplay;
  * spark's latest health snapshot, when there is one: measured TPS and tick
    times are worth more than any rule of thumb.

Vanilla and the plugin loaders get the same JVM and properties reading; they
are not modpacks, and nothing here pretends they are.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any

from app import engine, loaders, specs

# --- the JVM -------------------------------------------------------------------------

# Flags that newer Java removed outright: the JVM refuses to start with
# "Unrecognized VM option" and the server dies before its first log line.
# (flag head, first Java that rejects it, what replaced it)
_REMOVED = [
    ("UseConcMarkSweepGC", 14, "CMS was removed in Java 14; G1 replaced it"),
    ("CMSClassUnloadingEnabled", 14, "a CMS setting; CMS was removed in Java 14"),
    ("CMSParallelRemarkEnabled", 14, "a CMS setting; CMS was removed in Java 14"),
    ("UseCMSInitiatingOccupancyOnly", 14, "a CMS setting; CMS was removed in Java 14"),
    ("CMSInitiatingOccupancyFraction", 14, "a CMS setting; CMS was removed in Java 14"),
    ("CMSIncrementalMode", 9, "a CMS setting removed in Java 9"),
    ("UseParNewGC", 10, "ParNew was removed in Java 10"),
    ("AggressiveOpts", 12, "removed in Java 12; its optimisations are on by default"),
    ("PrintGCTimeStamps", 9, "replaced by -Xlog:gc in Java 9"),
    ("PrintGCDateStamps", 9, "replaced by -Xlog:gc in Java 9"),
    ("UseGCLogFileRotation", 9, "replaced by -Xlog:gc in Java 9"),
    ("UseLargePagesInMetaspace", 16, "removed in Java 16"),
    ("UseFastAccessorMethods", 9, "removed in Java 9"),
    ("Xincgc", 9, "incremental CMS, removed in Java 9"),
]
# Accepted but doing nothing -- harmless, misleading.
_IGNORED = [
    ("PermSize", 8, "PermGen no longer exists since Java 8"),
    ("MaxPermSize", 8, "PermGen no longer exists since Java 8"),
    ("UseBiasedLocking", 15, "biased locking is gone since Java 15"),
    ("UseCompressedStrings", 7, "never did anything on modern Java"),
    ("OptimizeStringConcat", 9, "always on"),
]
_COLLECTORS = {"UseG1GC": "G1", "UseZGC": "ZGC", "UseShenandoahGC": "Shenandoah",
               "UseParallelGC": "Parallel", "UseSerialGC": "Serial",
               "UseConcMarkSweepGC": "CMS", "UseParallelOldGC": "Parallel"}


def _size_mb(value: str) -> int | None:
    m = re.fullmatch(r"(\d+)([kKmMgGtT]?)", value or "")
    if not m:
        return None
    n = int(m.group(1))
    return {"": n // 1048576, "k": n // 1024, "m": n, "g": n * 1024, "t": n * 1048576}[
        m.group(2).lower()]


def jvm_args(command: str, args_file: str | None) -> dict:
    """Every JVM argument the server starts with, and where each came from.

    Arguments before -jar / the main class are the JVM's; `@file` arguments
    are expanded from that file the way the java launcher does it. Anything
    after is the game's.
    """
    try:
        argv = shlex.split(command or "")
    except ValueError:
        argv = (command or "").split()
    out: list[dict] = []
    if argv and re.search(r"(?:^|/)java(?:\.exe)?$", argv[0]):
        argv = argv[1:]
    for tok in argv:
        if tok in ("-jar", "nogui", "--nogui") or tok.endswith(".jar") or (
                not tok.startswith(("-", "@"))):
            break
        if tok.startswith("@"):
            if tok.endswith("user_jvm_args.txt") and args_file is not None:
                for line in args_file.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        for part in line.split():
                            out.append({"arg": part, "from": "user_jvm_args.txt"})
            continue                     # @libraries/.../unix_args.txt: the loader's
        out.append({"arg": tok, "from": "command"})
    return {"args": out}


def check_jvm(args: list[dict], *, java: int | None, container_mb: int | None,
              cpus: float | None, mc: str = "") -> list[dict]:
    """Faults and drags in the JVM arguments. Each finding names the argument."""
    issues: list[dict] = []
    flags = [a["arg"] for a in args]

    def head(flag: str) -> str:
        return re.sub(r"^-XX:[+-]?", "", flag).split("=", 1)[0]

    def add(level, title, detail, arg=None, fix=None):
        issues.append({"level": level, "title": title, "detail": detail,
                       "arg": arg, "fix": fix})

    java = java or 0
    heads = {head(f): f for f in flags if f.startswith("-XX:")}
    for name, since, why in _REMOVED:
        flag = heads.get(name) or next((f for f in flags if f == f"-{name}"), None)
        if not flag:
            continue
        if java >= since:
            add("bad", f"{flag} stops Java {java} from starting",
                f"{why}. Java {java} refuses unknown options and exits before the "
                "server writes a single line of log -- which looks like a server "
                "that silently does nothing.", flag, {"remove_arg": flag})
        else:
            add("warn", f"{flag} is obsolete", f"{why}. It works on Java {java} but "
                "will stop the server starting on a newer one.", flag,
                {"remove_arg": flag})
    for name, since, why in _IGNORED:
        flag = heads.get(name)
        if flag and java >= since:
            add("info", f"{flag} does nothing here", why + ".", flag, {"remove_arg": flag})

    chosen = sorted({_COLLECTORS[h] for h, f in heads.items()
                     if h in _COLLECTORS and f.startswith("-XX:+")})
    if len(chosen) > 1:
        add("bad", f"Two garbage collectors at once ({', '.join(chosen)})",
            "The JVM refuses to start: 'Multiple garbage collectors selected'. Keep one "
            "-- G1 for most servers.", ", ".join(chosen))
    if "Parallel" in chosen:
        add("warn", "The Parallel collector is chosen",
            "It stops the whole server for every collection; on a game server that is "
            "a stutter every few seconds. G1 keeps pauses short.",
            heads.get("UseParallelGC"), {"replace_arg": [heads.get("UseParallelGC"),
                                                         "-XX:+UseG1GC"]})
    if "ZGC" in chosen and java and java < 15:
        add("bad", "ZGC on Java " + str(java), "ZGC is experimental before Java 15 and "
            "needs -XX:+UnlockExperimentalVMOptions; prefer G1 here.", heads.get("UseZGC"))
    if "ZGenerational" in heads and java >= 24:
        add("info", "-XX:+ZGenerational does nothing on Java 24+",
            "Generational ZGC is the only ZGC from Java 24.", heads["ZGenerational"],
            {"remove_arg": heads["ZGenerational"]})

    xmx = [(_size_mb(f[4:]), f, a["from"]) for a in args for f in [a["arg"]]
           if f.startswith("-Xmx")]
    xms = [(_size_mb(f[4:]), f) for f in flags if f.startswith("-Xms")]
    if len({v for v, _, _ in xmx}) > 1:
        add("warn", "The heap is set twice, to different sizes",
            f"{', '.join(f'{f} ({src})' for _, f, src in xmx)}. The last one wins, so "
            "the number on the Tune tab may not be the one the server uses.",
            xmx[-1][1])
    heap = xmx[-1][0] if xmx else None
    if heap and xms and xms[-1][0] and xms[-1][0] > heap:
        add("bad", "The minimum heap is bigger than the maximum",
            f"{xms[-1][1]} > {xmx[-1][1]}: the JVM refuses to start.", xms[-1][1])
    if heap and container_mb:
        headroom = container_mb - heap
        if headroom < 512:
            add("bad", f"The heap ({heap} MB) does not fit the container ({container_mb} MB)",
                "Java needs memory beyond the heap -- threads, metaspace, the JIT, "
                "native buffers. Without room for it the container is killed for running "
                "out of memory, usually under load, with no Java error at all.",
                xmx[-1][1], {"set_heap_mb": max(1024, specs.heap_for_container_mb(container_mb))})
        elif heap < container_mb * 0.45 and container_mb >= 3072:
            add("info", f"The heap uses only {heap} MB of a {container_mb} MB container",
                "Most of the container's memory is not available to the game. If the "
                "server is tight on memory, the heap can grow.", xmx[-1][1],
                {"set_heap_mb": specs.heap_for_container_mb(container_mb)})
    if heap and heap >= 32 * 1024:
        add("warn", "A heap of 32 GB or more loses compressed pointers",
            "Every object reference doubles in size; 31 GB holds more than 32 does.",
            xmx[-1][1], {"set_heap_mb": 31 * 1024})
    if not xmx:
        add("warn", "No maximum heap is set",
            "Java then picks a quarter of the container's memory -- far less than a "
            "modded server wants.", None, {"set_heap_mb": specs.heap_for_container_mb(
                container_mb) if container_mb else 4096})
    if any(f.startswith(("-Xmn", "-XX:NewSize", "-XX:MaxNewSize")) for f in flags) and (
            "G1" in chosen or not chosen):
        flag = next(f for f in flags if f.startswith(("-Xmn", "-XX:NewSize", "-XX:MaxNewSize")))
        add("warn", f"{flag} fixes the young generation size",
            "G1 sizes it itself to meet its pause target; fixing it defeats that.",
            flag, {"remove_arg": flag})
    pause = next((f for f in flags if f.startswith("-XX:MaxGCPauseMillis=")), None)
    if pause:
        ms = int(re.sub(r"\D", "", pause.split("=", 1)[1]) or 0)
        if ms and (ms < 50 or ms > 500):
            add("warn", f"{pause} is an unusual pause target",
                "Below 50 ms G1 collects constantly and gets less done; above 500 ms a "
                "pause is a visible freeze. 200 is the usual value.", pause,
                {"replace_arg": [pause, "-XX:MaxGCPauseMillis=200"]})
    for prefix in ("-XX:ParallelGCThreads=", "-XX:ConcGCThreads="):
        flag = next((f for f in flags if f.startswith(prefix)), None)
        if flag and cpus:
            n = int(re.sub(r"\D", "", flag.split("=", 1)[1]) or 0)
            if n > max(1, round(cpus)):
                add("warn", f"{flag} asks for more threads than the server's {cpus:g} CPU(s)",
                    "The extra threads only compete with the server's own.", flag,
                    {"replace_arg": [flag, f"{prefix}{max(1, round(cpus) - 1)}"]})
    if "-XX:-UseCompressedOops" in flags and heap and heap < 32 * 1024:
        add("warn", "Compressed pointers are turned off",
            "Below 32 GB of heap they save a great deal of memory for nothing.",
            "-XX:-UseCompressedOops", {"remove_arg": "-XX:-UseCompressedOops"})
    debug = next((f for f in flags if f.startswith(("-agentlib:jdwp", "-Xrunjdwp", "-Xdebug"))),
                 None)
    if debug:
        add("bad", "A Java debugger is left on",
            f"{debug} slows the server and, if its port is reachable, lets anyone take "
            "control of the JVM. Remove it.", debug, {"remove_arg": debug})
    seen: dict[str, str] = {}
    for f in flags:
        if f.startswith("-XX:") and "=" in f:
            key = f.split("=", 1)[0]
            if key in seen and seen[key] != f:
                add("info", f"{key} is set twice", f"{seen[key]} and {f}; the last wins.", f)
            seen[key] = f
    if not chosen and heap and heap >= 2048:
        add("warn", "No garbage collector is chosen, and no GC tuning",
            "The JVM's defaults suit a desktop app, not a game server that allocates "
            "constantly. The Tune tab's recommended flags (Aikar's) are the fix.")
    if mc and _before(mc, (1, 18, 1)) and not any("formatMsgNoLookups" in f or
                                                    "log4j.configurationFile" in f
                                                    for f in flags):
        add("warn", "No Log4Shell mitigation",
            f"Minecraft {mc} ships a log4j vulnerable to CVE-2021-44228 unless patched; "
            "-Dlog4j2.formatMsgNoLookups=true closes it.", None,
            {"add_arg": "-Dlog4j2.formatMsgNoLookups=true"})
    return issues


def _before(mc: str, want: tuple) -> bool:
    try:
        parts = tuple(int(p) for p in mc.split(".")[:3])
    except ValueError:
        return False
    if parts and parts[0] >= 26:
        return False
    return parts < want


# --- optimisation mods ------------------------------------------------------------------

# key: (patterns that identify the jar, display name, what it means here)
OPTIMIZATION_MODS: dict[str, dict[str, Any]] = {
    "ferritecore": {"match": [r"^ferritecore"], "name": "FerriteCore",
                    "what": "Cuts the memory blockstates and models use -- often 20-30% "
                            "on a big pack. The heap can be smaller than the pack's own "
                            "recommendation."},
    "lithium": {"match": [r"^lithium", r"^radium", r"^canary"], "name": "Lithium",
                "what": "Faster game logic (AI, physics, chunk ticking) with vanilla "
                        "behaviour. Nothing to configure; it is on by default."},
    "modernfix": {"match": [r"^modernfix"], "name": "ModernFix",
                  "what": "Faster startup and lower memory for big packs."},
    "krypton": {"match": [r"^krypton"], "name": "Krypton",
                "what": "A faster network stack. Keep network-compression-threshold at its "
                        "default; Krypton makes compression cheap."},
    "c2me": {"match": [r"^c2me"], "name": "C2ME",
             "what": "Generates and loads chunks on many threads. Pre-generating the world "
                     "goes much faster with it, and it uses every core the server gets."},
    "alltheleaks": {"match": [r"^alltheleaks", r"^atl[-_]"], "name": "AllTheLeaks",
                    "what": "Fixes memory leaks in other mods. Long uptimes without the "
                            "heap creeping up."},
    "memoryleakfix": {"match": [r"^memoryleakfix", r"^memory[-_]?leak[-_]?fix"],
                      "name": "Memory Leak Fix",
                      "what": "Fixes memory leaks in vanilla and common mods."},
    "packetfixer": {"match": [r"^packetfixer", r"^packet[-_]?fixer"], "name": "Packet Fixer",
                    "what": "Raises the network packet size limits big packs hit (\"packet "
                            "too large\" kicks)."},
    "servercore": {"match": [r"^servercore"], "name": "ServerCore",
                   "what": "Server-side optimisations; in its dynamic mode it moves view "
                           "and simulation distance and mob caps with tick times."},
    "aiimprovements": {"match": [r"^ai[-_]?improvements", r"^aiimprovements"],
                       "name": "AI Improvements",
                       "what": "Cheaper mob AI goals; fewer milliseconds per tick with many "
                               "mobs."},
    "chunky": {"match": [r"^chunky"], "name": "Chunky",
               "what": "Pre-generates the world. Generating terrain while players explore "
                       "is the biggest lag source on a new server; pre-generation removes "
                       "it."},
    "threadtweak": {"match": [r"^threadtweak"], "name": "ThreadTweak",
                    "what": "Re-balances the JVM's worker threads around the server thread."},
    "spark": {"match": [r"^spark[-_]"], "name": "spark",
              "what": "The profiler. Diagnose > Profiler records health and profiles."},
}


def detect_mods(files: list[str]) -> list[dict]:
    """Which optimisation mods are present (enabled), by their jar names."""
    found = []
    for key, info in OPTIMIZATION_MODS.items():
        for f in files:
            name = f.lower()
            if name.endswith(".disabled"):
                continue
            if any(re.match(p, name) for p in info["match"]):
                found.append({"key": key, "name": info["name"], "file": f,
                              "what": info["what"]})
                break
    return found


# --- the Paper family's own configs ------------------------------------------------------


def yaml_get(text: str, path: tuple[str, ...]) -> str | None:
    """The scalar at a key path in a YAML file, read by indentation. Enough for
    Paper/Spigot's plain block style; returns None for anything else."""
    stack: list[tuple[int, str]] = []
    for line in text.splitlines():
        m = re.match(r"^(\s*)([\w\-.]+):\s*(.*?)\s*(#.*)?$", line)
        if not m or line.lstrip().startswith("#"):
            continue
        indent, key, value = len(m.group(1)), m.group(2), m.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        if tuple(k for _, k in stack) == path:
            return value.strip("'\"") if value else None
    return None


def yaml_set(text: str, path: tuple[str, ...], value: str) -> str | None:
    """Set an existing scalar at a key path, keeping comments and layout.
    None if the key is not there -- nothing is ever added."""
    lines = text.splitlines(keepends=True)
    stack: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)([\w\-.]+):(\s*)(.*?)(\s*#.*)?(\r?\n)?$", line)
        if not m or line.lstrip().startswith("#"):
            continue
        indent, key = len(m.group(1)), m.group(2)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        if tuple(k for _, k in stack) == path and m.group(4):
            lines[i] = (f"{m.group(1)}{key}:{m.group(3) or ' '}{value}"
                        f"{m.group(5) or ''}{m.group(6) or ''}")
            return "".join(lines)
    return None


# (file, key path, recommended value, why, changes gameplay)
PAPER_TUNING = [
    ("config/paper-world-defaults.yml", ("environment", "optimize-explosions"), "true",
     "Computes explosions faster; the result looks the same.", False),
    ("config/paper-world-defaults.yml", ("chunks", "max-auto-save-chunks-per-tick"), "8",
     "Spreads autosave over more ticks, so a save is not a lag spike.", False),
    ("config/paper-world-defaults.yml", ("chunks", "prevent-moving-into-unloaded-chunks"),
     "true", "Players cannot outrun chunk loading on an elytra or a boat and force "
     "synchronous loads.", False),
    ("config/paper-world-defaults.yml", ("collisions", "max-entity-collisions"), "2",
     "Caps how many entities one entity checks collisions with; mob farms stop "
     "dragging the server.", True),
    ("config/paper-world-defaults.yml", ("misc", "redstone-implementation"),
     "ALTERNATE_CURRENT", "A much faster redstone engine. Almost every contraption "
     "behaves the same; a few timing-sensitive ones do not.", True),
    ("spigot.yml", ("world-settings", "default", "entity-activation-range", "animals"), "16",
     "Animals further than this from a player tick less often.", True),
    ("spigot.yml", ("world-settings", "default", "entity-activation-range", "monsters"),
     "24", "The same for monsters.", True),
    ("spigot.yml", ("world-settings", "default", "entity-activation-range", "misc"), "8",
     "The same for items, minecarts and the like.", True),
    ("spigot.yml", ("world-settings", "default", "merge-radius", "item"), "3.5",
     "Dropped items merge from further apart: fewer entities.", False),
]


# The optimisation mods' own switches that are off by default and worth
# having on a small server -- (mod key, file, key path, value, why, changes
# gameplay). Every key here was read out of the file the mod itself writes;
# one that is not in this server's file (another version of the mod) is
# simply not offered.
MOD_TUNING = [
    ("servercore", "config/servercore/config.yml", ("dynamic", "enabled"), "true",
     "ServerCore lowers view and simulation distance and mob caps a step at a time "
     "while ticks run over 35 ms, and raises them again when the server catches up. "
     "On a small server it holds 20 TPS through a busy evening.", True),
    ("servercore", "config/servercore/config.yml",
     ("features", "lobotomize-villagers", "enabled"), "true",
     "Villagers locked in a trading cell think once a second instead of every tick; "
     "a trading hall stops costing milliseconds. They still trade and restock.", True),
    ("servercore", "config/servercore/config.yml", ("activation-range", "enabled"), "true",
     "Mobs far from every player tick less often, as on Paper.", True),
    ("servercore", "config/servercore/optimizations.yml", ("fast-biome-lookups",), "true",
     "Much cheaper mob spawning; a mob may spawn a few blocks across a biome border.",
     True),
    ("chunky", "config/chunky/config.json", ("continueOnRestart",), "true",
     "A pre-generation cut short by a restart carries on by itself.", False),
    ("chunky", "plugins/Chunky/config.yml", ("continue-on-restart",), "true",
     "A pre-generation cut short by a restart carries on by itself.", False),
]


def json_get(text: str, path: tuple[str, ...]) -> str | None:
    """The scalar at a key path in a JSON file, as text ("true", "8")."""
    try:
        node: Any = json.loads(text)
        for k in path:
            node = node[k]
    except (ValueError, KeyError, TypeError):
        return None
    if isinstance(node, (dict, list)):
        return None
    return json.dumps(node).strip('"')


def json_set(text: str, path: tuple[str, ...], value: str) -> str | None:
    """Set an existing scalar at a key path; None if it is not there. The
    value keeps the type the file had (a boolean stays a boolean)."""
    try:
        data = json.loads(text)
        node = data
        for k in path[:-1]:
            node = node[k]
        old = node[path[-1]]
    except (ValueError, KeyError, TypeError):
        return None
    if isinstance(old, bool):
        new: Any = value.lower() == "true"
    elif isinstance(old, int):
        new = int(value)
    elif isinstance(old, float):
        new = float(value)
    else:
        new = value
    node[path[-1]] = new
    return json.dumps(data, indent=2) + "\n"


async def _tuning(server_id: str, table, kind: str) -> list[dict]:
    out = []
    cache: dict[str, str | None] = {}
    for path, keys, value, why, gameplay in table:
        if path not in cache:
            try:
                cache[path] = await engine.read_file(server_id, path)
            except Exception:       # noqa: BLE001 -- not generated until the first boot
                cache[path] = None
        text = cache[path]
        if text is None:
            continue
        fmt = "json" if path.endswith(".json") else "yaml"
        current = (json_get if fmt == "json" else yaml_get)(text, keys)
        if current is None:
            continue
        out.append({"id": f"{fmt}:{path}:{'.'.join(keys)}", "file": path,
                    "key": ".".join(keys), "current": current, "value": value,
                    "why": why, "gameplay": gameplay, "kind": kind,
                    "applied": current.lower() == value.lower()})
    return out


async def paper_tuning(server_id: str) -> list[dict]:
    """The curated settings, for keys that exist in this server's files."""
    return await _tuning(server_id, PAPER_TUNING, "paper")


async def mod_tuning(server_id: str, present: set[str]) -> list[dict]:
    """The optimisation mods' own switches, for mods this server has."""
    table = [row[1:] for row in MOD_TUNING if row[0] in present]
    out = await _tuning(server_id, table, "mod")
    names = {row[1]: OPTIMIZATION_MODS[row[0]]["name"] for row in MOD_TUNING}
    for item in out:
        item["mod"] = names.get(item["file"], "")
    return out


# --- the whole reading ---------------------------------------------------------------


async def analyse(server_id: str) -> dict:
    manifest = await engine.read_manifest(server_id)
    server = await engine.get_server(server_id)
    family = loaders.family_of(manifest.get("loader") or server.get("loader") or "")
    mc = manifest.get("minecraft") or ""
    kind = ("vanilla" if family in ("", "vanilla") else
            "plugins" if family in loaders.PLUGIN_FAMILIES else "mods")
    command = server.get("execution_command") or ""
    args_file = None
    if "@user_jvm_args.txt" in command:
        try:
            args_file = await engine.read_file(server_id, "user_jvm_args.txt")
        except engine.EngineError:
            args_file = ""
    parsed = jvm_args(command, args_file)
    container_mb = server.get("memory_mb")
    limits = server.get("limits") or {}
    cpus = (limits.get("cpu_pct") / 100) if limits.get("cpu_pct") else None
    java = server.get("java_major") or engine.java_of(server).get("major")
    issues = check_jvm(parsed["args"], java=java, container_mb=container_mb, cpus=cpus, mc=mc)

    files: list[str] = []
    folder = "plugins" if kind == "plugins" else "mods"
    if kind != "vanilla":
        try:
            files = [n for n in await engine.list_dir(server_id, folder)
                     if n.lower().endswith((".jar", ".jar.disabled"))]
        except engine.EngineError:
            files = []
    present = detect_mods(files) if kind == "mods" else []
    if kind == "plugins":
        present = [m for m in detect_mods(files) if m["key"] in ("chunky", "spark")]
    keys = {m["key"] for m in present}

    actions = []
    if "chunky" in keys:
        actions.append({"id": "pregen", "title": "Pre-generate the world",
                        "detail": "Chunky generates the terrain out to a radius now, so "
                                  "exploring later costs nothing."
                                  + (" C2ME spreads it over every core." if "c2me" in keys
                                     else "")})
    else:
        actions.append({"id": "install_chunky", "title": "Install Chunky to pre-generate",
                        "detail": "Generating terrain while players explore is the biggest "
                                  "lag source on a new server. Chunky does it ahead of time; "
                                  "it is server-side only, so players need nothing."})
    if kind != "vanilla" and "spark" not in keys and not (
            kind == "plugins" and _plugin_bundles_spark(family, mc)):
        actions.append({"id": "install_spark", "title": "Install spark to measure",
                        "detail": "Rules of thumb are guesses; spark measures tick times and "
                                  "shows which mod is spending them."})

    notes = []
    if keys & {"ferritecore", "modernfix"}:
        notes.append("FerriteCore/ModernFix are present: this pack needs noticeably less "
                     "heap than packs of its size usually do.")
    if kind == "mods" and not keys & {"alltheleaks", "memoryleakfix"} and len(files) > 150:
        notes.append("No memory-leak fix mod is present. On a pack this size, a daily "
                     "restart keeps the heap from creeping up over long uptimes.")
    if kind == "vanilla":
        notes.append("Vanilla takes no mods or plugins, so the gains here are the JVM, "
                     "server.properties and pre-generation. Paper runs the same world much "
                     "faster if you can switch.")

    paper = await paper_tuning(server_id) if kind == "plugins" else []
    mod_settings = await mod_tuning(server_id, keys)
    dynamic = next((x for x in mod_settings if x["key"] == "dynamic.enabled"), None)
    if dynamic and dynamic["applied"]:
        notes.append("ServerCore's dynamic mode is on: it moves view and simulation distance "
                     "itself, so the distances in server.properties are only where it starts.")
    spark_numbers = None
    try:
        from app import spark
        spark_numbers = await spark.latest_numbers(server_id)
    except Exception:       # noqa: BLE001
        pass
    return {
        "kind": kind, "loader": family, "minecraft": mc, "java": java,
        "container_mb": container_mb, "cpus": cpus,
        "jvm": {"args": parsed["args"], "issues": issues,
                "source": "user_jvm_args.txt + command" if args_file is not None else "command"},
        "mods": present, "actions": actions, "notes": notes,
        "paper": paper, "mod_settings": mod_settings, "spark": spark_numbers,
        "counts": {"bad": sum(1 for i in issues if i["level"] == "bad"),
                   "warn": sum(1 for i in issues if i["level"] == "warn")},
    }


def _plugin_bundles_spark(family: str, mc: str) -> bool:
    from app.spark import _at_least
    return family in ("paper", "purpur", "folia") and _at_least(mc, (1, 21))


# --- applying -------------------------------------------------------------------------


async def apply(server_id: str, *, jvm_fixes: list[dict], yaml_ids: list[str]) -> dict:
    """Apply chosen fixes where each setting actually lives.

    A JVM argument is edited in user_jvm_args.txt when that is where it came
    from, otherwise in the startup command. A YAML setting is changed only
    if the key exists. Everything takes effect at the next start.
    """
    reading = await analyse(server_id)
    server = await engine.get_server(server_id)
    command = server.get("execution_command") or ""
    args_text = None
    if "@user_jvm_args.txt" in command:
        try:
            args_text = await engine.read_file(server_id, "user_jvm_args.txt")
        except engine.EngineError:
            args_text = ""
    origin = {a["arg"]: a["from"] for a in reading["jvm"]["args"]}
    done: list[str] = []

    def edit(arg: str, new: str | None) -> None:
        nonlocal command, args_text
        where = origin.get(arg)
        if where == "user_jvm_args.txt" and args_text is not None:
            lines = []
            for line in args_text.splitlines():
                if arg not in line.split():
                    lines.append(line)
                    continue
                parts = [p for p in line.split() if p != arg]
                if new:
                    parts.append(new)
                if parts:
                    lines.append(" ".join(parts))
            args_text = "\n".join(lines) + "\n"
        elif where == "command":
            argv = shlex.split(command)
            argv = [new if a == arg else a for a in argv if not (a == arg and not new)]
            command = shlex.join(argv)
        else:
            return
        done.append(f"{arg} -> {new}" if new else f"removed {arg}")

    def add(arg: str) -> None:
        nonlocal command, args_text
        if args_text is not None:
            args_text = args_text.rstrip("\n") + f"\n{arg}\n"
        else:
            argv = shlex.split(command)
            argv.insert(1, arg)
            command = shlex.join(argv)
        done.append(f"added {arg}")

    for fix in jvm_fixes:
        if fix.get("remove_arg") and fix["remove_arg"] in origin:
            edit(fix["remove_arg"], None)
        elif fix.get("replace_arg"):
            old, new = fix["replace_arg"]
            if old in origin and re.fullmatch(r"-XX:[+-]?\w+(=[\w.]+)?", new or ""):
                edit(old, new)
        elif fix.get("add_arg") == "-Dlog4j2.formatMsgNoLookups=true":
            add(fix["add_arg"])
        elif fix.get("set_heap_mb"):
            mb = int(fix["set_heap_mb"])
            if not 512 <= mb <= 256 * 1024:
                continue
            for a in list(origin):
                if a.startswith(("-Xmx", "-Xms")):
                    edit(a, None)
            if args_text is not None:
                args_text = args_text.rstrip("\n") + f"\n-Xms{mb}M\n-Xmx{mb}M\n"
            else:
                argv = shlex.split(command)
                argv[1:1] = [f"-Xms{mb}M", f"-Xmx{mb}M"]
                command = shlex.join(argv)
            done.append(f"heap set to {mb} MB")
    if command != (server.get("execution_command") or ""):
        await engine.update_server(server_id, {"execution_command": command})
    if args_text is not None:
        await engine.write_file(server_id, "user_jvm_args.txt", args_text)

    by_id = {p["id"]: p for p in reading["paper"] + reading["mod_settings"]}
    files: dict[str, str] = {}
    for yid in yaml_ids:
        item = by_id.get(yid)
        if not item:
            continue
        text = files.get(item["file"])
        if text is None:
            text = await engine.read_file(server_id, item["file"])
        setter = json_set if item["id"].startswith("json:") else yaml_set
        new = setter(text, tuple(item["key"].split(".")), item["value"])
        if new is not None:
            files[item["file"]] = new
            done.append(f"{item['file']}: {item['key']} = {item['value']}")
    for path, text in files.items():
        await engine.write_file(server_id, path, text)
    return {"applied": done, "restart_needed": bool(done)}


async def install_tool(job, server_id: str, which: str) -> dict:
    """Chunky or spark, from its own published builds."""
    if which == "spark":
        from app import spark
        return await spark.install(job, server_id)
    if which != "chunky":
        raise ValueError(which)
    manifest = await engine.read_manifest(server_id)
    family = loaders.family_of(manifest.get("loader") or "")
    mc = manifest.get("minecraft") or ""
    from app import modrinth
    from app import mods as modmgr
    if family in loaders.PLUGIN_FAMILIES:
        # Plugins are not in CurseForge's Minecraft mod catalogue; the plugin
        # build is on Modrinth.
        versions = await modrinth.list_versions("chunky", game_version=mc,
                                                loaders=["paper", "bukkit", "spigot", "folia"])
        if not versions:
            raise RuntimeError(f"no Chunky plugin build for {mc}")
        v = versions[0]
        await modmgr.add_mod(server_id, source="modrinth", project_id=v["mod_id"],
                             file_id=v["file_id"], directory="plugins")
        return {"installed": "chunky", "where": "plugins", "source": "modrinth"}
    if family in ("", "vanilla"):
        raise RuntimeError("Vanilla cannot load Chunky -- it has no mod loader.")
    from app.repair import find_library
    found = await find_library("chunky", mc=mc, loader=family)
    if not found:
        raise RuntimeError(f"no Chunky build for {family} {mc}")
    await modmgr.add_mod(server_id, source=found["source"], project_id=found["project_id"],
                         file_id=found["file_id"], directory="mods")
    return {"installed": "chunky", "where": "mods", "source": found["source"]}


_CHUNKY_PROGRESS = re.compile(r"Processed:\s*([\d,]+)\s*chunks\s*\(([\d.]+)%\)", re.I)
_CHUNKY_DONE = re.compile(r"Task finished for|Task (?:complete|completed)", re.I)


async def pregenerate(job, server_id: str, *, radius: int,
                      world: str = "minecraft:overworld") -> dict:
    """Run Chunky out to `radius` blocks around spawn, following its progress.

    Cancelling the job pauses Chunky rather than abandoning it, so
    `chunky continue` (or running this again) picks up where it stopped.
    """
    import asyncio
    radius = max(250, min(int(radius), 20000))
    stats = await engine.get_stats(server_id)
    if not stats.get("running"):
        raise RuntimeError("start the server first -- Chunky runs inside it")
    for line in (f"chunky world {world}", "chunky spawn", f"chunky radius {radius}",
                 "chunky start"):
        await engine.send_command(server_id, line)
        await asyncio.sleep(1)
    job.log_line(f"Chunky started: {world}, radius {radius} blocks")
    percent = 0.0
    try:
        while True:
            await asyncio.sleep(10)
            lines = (await engine.console_lines(server_id))[-60:]
            for line in reversed(lines):
                m = _CHUNKY_PROGRESS.search(line)
                if m:
                    percent = float(m.group(2))
                    job.set_step(f"Pre-generating ({percent:.1f}%, {m.group(1)} chunks)",
                                 min(99, percent))
                    break
            if any(_CHUNKY_DONE.search(ln) for ln in lines):
                job.log_line("Chunky finished")
                return {"done": True, "radius": radius, "world": world}
            if not (await engine.get_stats(server_id)).get("running"):
                return {"done": False, "percent": percent,
                        "why": "the server stopped; Chunky resumes with 'chunky continue'"}
    except asyncio.CancelledError:
        try:
            await engine.send_command(server_id, "chunky pause")
        except Exception:       # noqa: BLE001
            pass
        raise
