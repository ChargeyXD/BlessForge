"""Host capability detection and per-instance performance tuning.

Two jobs:

  * Work out what the machine running the servers can actually give -- which
    is not the same as what a modpack asks for. A pack that "recommends 8 GB"
    on a 12 GB box with four other containers resident will die at startup
    with no log, so the ceiling has to come from the host, not the pack.

  * Turn that into concrete, individually toggleable settings: JVM flags and
    a handful of server.properties values, each with a plain explanation of
    what it does and why it was suggested.

Nothing here is applied automatically. The optimizer proposes; the user picks.
"""
from __future__ import annotations

import os
import re
import shutil

from app import config, crafty

# Aikar's flags -- the long-standing community baseline for Minecraft servers.
# G1GC tuned for large short-lived allocation rates rather than raw throughput,
# which is what a modded server produces.
# https://docs.papermc.io/paper/aikars-flags
AIKAR_FLAGS = [
    ("-XX:+UseG1GC", "Use the G1 garbage collector", "gc",
     "G1 keeps pauses short and predictable, which matters far more for tick "
     "stability than raw throughput."),
    ("-XX:+ParallelRefProcEnabled", "Process references in parallel", "gc",
     "Modded servers create huge numbers of references; processing them on one "
     "thread shows up as periodic lag spikes."),
    ("-XX:MaxGCPauseMillis=200", "Target 200 ms maximum GC pause", "gc",
     "Keeps any single collection under roughly four ticks."),
    ("-XX:+UnlockExperimentalVMOptions", "Allow experimental JVM options", "gc",
     "Required before the G1 tuning flags below are accepted."),
    ("-XX:+DisableExplicitGC", "Ignore System.gc() calls from mods", "gc",
     "Some mods call System.gc() directly, forcing an expensive full collection "
     "for no benefit."),
    ("-XX:+AlwaysPreTouch", "Commit the heap up front", "memory",
     "Touches every heap page at startup so the OS does not fault them in "
     "mid-tick later. Costs a slower start."),
    ("-XX:G1NewSizePercent=30", "Minimum young generation 30%", "gc",
     "Most modded allocations die young; a bigger young gen collects them "
     "cheaply."),
    ("-XX:G1MaxNewSizePercent=40", "Maximum young generation 40%", "gc",
     "Upper bound for the same."),
    ("-XX:G1HeapRegionSize=8M", "8 MB heap regions", "gc",
     "Suits the multi-gigabyte heaps modded servers run with."),
    ("-XX:G1ReservePercent=20", "Reserve 20% of the heap", "gc",
     "Headroom so a sudden allocation burst does not trigger a full GC."),
    ("-XX:G1HeapWastePercent=5", "Tolerate 5% waste", "gc",
     "Stops G1 doing expensive collections to reclaim very little."),
    ("-XX:G1MixedGCCountTarget=4", "Four mixed collections per cycle", "gc",
     "Spreads old-generation cleanup over several short pauses."),
    ("-XX:InitiatingHeapOccupancyPercent=15", "Start collecting at 15% occupancy",
     "gc", "Begins old-gen work early, so it never has to happen all at once."),
    ("-XX:G1MixedGCLiveThresholdPercent=90", "Collect regions up to 90% live",
     "gc", "Lets G1 reclaim regions it would otherwise skip."),
    ("-XX:G1RSetUpdatingPauseTimePercent=5", "Cap remembered-set work at 5%",
     "gc", "Moves remembered-set updates onto concurrent threads."),
    ("-XX:SurvivorRatio=32", "Smaller survivor spaces", "gc",
     "Modded workloads promote little; large survivor spaces just waste heap."),
    ("-XX:+PerfDisableSharedMem", "Disable shared memory perf counters", "io",
     "Stops the JVM writing perf data to /tmp, which can stall on slow disks."),
    ("-XX:MaxTenuringThreshold=1", "Promote survivors quickly", "gc",
     "Anything that survives one collection is usually long-lived anyway."),
    ("-Dusing.aikars.flags=https://mcflags.emc.gs", "Tag the flag set", "meta",
     "Marks this server as using Aikar's flags; harmless, helps support."),
    ("-Daikars.new.flags=true", "Use the modern flag set", "meta",
     "Companion to the tag above."),
]

# Applied on top of Aikar's when the heap is large (>= 12 GB), per his notes.
LARGE_HEAP_OVERRIDES = {
    "-XX:G1NewSizePercent=30": "-XX:G1NewSizePercent=40",
    "-XX:G1MaxNewSizePercent=40": "-XX:G1MaxNewSizePercent=50",
    "-XX:G1HeapRegionSize=8M": "-XX:G1HeapRegionSize=16M",
    "-XX:G1ReservePercent=20": "-XX:G1ReservePercent=15",
    "-XX:InitiatingHeapOccupancyPercent=15": "-XX:InitiatingHeapOccupancyPercent=20",
}

EXTRA_FLAGS = [
    ("-Dlog4j2.formatMsgNoLookups=true", "Block the Log4Shell lookup path",
     "security",
     "Neutralises CVE-2021-44228 on older packs that still ship a vulnerable "
     "log4j. Harmless on patched versions."),
    ("-XX:+UseStringDeduplication", "Deduplicate identical strings", "memory",
     "Modded servers hold many duplicate strings (item and tag ids). Reclaims "
     "real memory on large packs."),
    ("-Dfml.readTimeout=180", "Raise the mod handshake timeout", "network",
     "Large packs can exceed the default timeout while syncing registries, "
     "which players see as a timeout on join."),
]


def host_specs() -> dict:
    """What the machine running the Minecraft servers has.

    Read from `/proc/meminfo`, which inside a container still reports the
    host's memory -- deliberately NOT from this container's own cgroup limit.

    That distinction was got wrong once and it matters: BlessForge reads its
    *own* cgroup, but the servers it sizes heaps for run under Crafty, which
    is a different container. Capping BlessForge at 1 GB (which the compose
    file now does, because streaming installs no longer need more) made it
    believe the machine had 1 GB and offer every pack a 1 GB heap.

    The app's own cap is still reported, as `app_limit_gb`, because it is
    worth seeing in the setup banner -- it just has no bearing on how much
    memory a Minecraft server can be given. When Crafty runs on a *different*
    machine, neither number is right and `HOST_RAM_GB` is the answer.
    """
    total_bytes = 0
    available_bytes = 0
    source = "unknown"
    app_limit_bytes = 0

    # This container's own ceiling: informational only.
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as fh:
                raw = fh.read().strip()
            if raw and raw != "max":
                value = int(raw)
                # Unlimited is reported as an implausibly huge number.
                if 0 < value < (1 << 62):
                    app_limit_bytes = value
        except (OSError, ValueError):
            continue

    meminfo: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                m = re.match(r"(\w+):\s+(\d+)\s*kB", line)
                if m:
                    meminfo[m.group(1)] = int(m.group(2)) * 1024
    except OSError:
        pass

    total_bytes = meminfo.get("MemTotal", 0)
    source = "host" if total_bytes else "unknown"
    # Last resort only: if /proc/meminfo is unreadable, our own cap is a
    # better guess than nothing.
    if not total_bytes and app_limit_bytes:
        total_bytes = app_limit_bytes
        source = "cgroup"
    available_bytes = meminfo.get("MemAvailable", 0)

    cpus = os.cpu_count() or 1
    try:  # cgroup CPU quota, if one is set
        with open("/sys/fs/cgroup/cpu.max") as fh:
            quota, period = fh.read().split()
            if quota != "max":
                cpus = max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass

    model = ""
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass

    disk_free = 0
    try:
        disk_free = shutil.disk_usage(str(config.DATA_DIR)).free
    except OSError:
        pass

    return {
        "total_ram_gb": round(total_bytes / 1024**3, 1),
        "available_ram_gb": round(available_bytes / 1024**3, 1),
        "cpu_count": cpus,
        "cpu_model": model,
        "disk_free_gb": round(disk_free / 1024**3, 1),
        "source": source,
        "note": (
            "Measured inside this container. If BlessForge and Crafty run on "
            "different machines, these are BlessForge's numbers -- set "
            "HOST_RAM_GB to describe the Crafty host instead."
        ) if source != "cgroup" else None,
        "app_limit_gb": round(app_limit_bytes / (1024 ** 3), 1)
        if app_limit_bytes else None,
    }


def effective_host() -> dict:
    """Host specs, with env overrides for when Crafty runs elsewhere."""
    specs = host_specs()
    override_ram = os.environ.get("HOST_RAM_GB")
    override_cpu = os.environ.get("HOST_CPU_COUNT")
    if override_ram:
        try:
            specs["total_ram_gb"] = float(override_ram)
            specs["available_ram_gb"] = float(override_ram)
            specs["source"] = "manual override"
        except ValueError:
            pass
    if override_cpu:
        try:
            specs["cpu_count"] = int(override_cpu)
        except ValueError:
            pass
    # Load average is the one host number that says whether the machine is
    # busy *right now*, which is exactly the question someone sizing a heap is
    # asking. Reported per-core so it reads the same on any box.
    try:
        one, _five, _fifteen = os.getloadavg()
        specs["load_1m"] = round(one, 2)
        cpus = specs.get("cpu_count") or 1
        specs["load_per_core"] = round(one / cpus, 2)
    except (OSError, AttributeError):
        specs["load_1m"] = None
        specs["load_per_core"] = None
    return specs


def recommend_memory(
    *, pack_recommended_mb: int, mod_count: int, host: dict
) -> dict:
    """Decide a heap size from what the pack wants and what the host has.

    The pack's number is a request, not a promise. Reserve room for the OS,
    Crafty itself and anything else already running, then cap the request at
    what is genuinely left.
    """
    total = host.get("total_ram_gb") or 0
    available = host.get("available_ram_gb") or total

    # Reserve for OS + other services: 25% of total, at least 1.5 GB, at most 4.
    reserve = min(4.0, max(1.5, total * 0.25))
    ceiling = max(1.0, min(total - reserve, available - 0.5 if available else total))

    if pack_recommended_mb:
        wanted = pack_recommended_mb / 1024
        basis = "the pack's recommended RAM"
    else:
        # Rough scale from mod count when the pack says nothing.
        if mod_count >= 300:
            wanted = 8.0
        elif mod_count >= 150:
            wanted = 6.0
        elif mod_count >= 50:
            wanted = 4.0
        else:
            wanted = 3.0
        basis = f"a {mod_count}-mod pack with no stated recommendation"

    heap = min(wanted, ceiling)
    heap = max(1.0, round(heap * 2) / 2)  # snap to 0.5 GB

    warnings = []
    if heap < wanted:
        warnings.append(
            f"The pack wants {wanted:.1f} GB but this host can only safely give "
            f"{heap:.1f} GB. Expect slower chunk generation, and stop other "
            f"containers if you need more."
        )
    if heap < 4 and (pack_recommended_mb or mod_count > 150):
        warnings.append(
            "Under 4 GB, large modded packs often fail to finish loading."
        )
    if total and total < 8:
        warnings.append(
            f"This host has {total:.1f} GB total. Running a big pack and other "
            "services together will be tight."
        )
    return {
        "heap_gb": heap,
        "requested_gb": round(wanted, 1),
        "ceiling_gb": round(ceiling, 1),
        "reserve_gb": round(reserve, 1),
        "basis": basis,
        "warnings": warnings,
    }


def build_flag_plan(
    *, heap_gb: float, host: dict, mc_version: str = "", loader: str = ""
) -> list[dict]:
    """The full set of candidate JVM flags, each independently toggleable."""
    large = heap_gb >= 12
    plan: list[dict] = []

    for flag, label, group, why in AIKAR_FLAGS:
        actual = LARGE_HEAP_OVERRIDES.get(flag, flag) if large else flag
        plan.append({
            "flag": actual,
            "label": label,
            "group": group,
            "why": why,
            "enabled": True,
            "recommended": True,
        })

    cpus = host.get("cpu_count") or 2
    # G1 picks thread counts from the CPU count, which inside a container can
    # be the host's rather than the share this server actually gets.
    gc_threads = max(2, min(8, cpus - 1))
    plan.append({
        "flag": f"-XX:ParallelGCThreads={gc_threads}",
        "label": f"Use {gc_threads} parallel GC threads",
        "group": "gc",
        "why": (
            f"This host reports {cpus} CPUs. Pinning GC threads stops the JVM "
            "over-subscribing cores it has to share with the server thread."
        ),
        "enabled": True,
        "recommended": True,
    })
    plan.append({
        "flag": f"-XX:ConcGCThreads={max(1, gc_threads // 4)}",
        "label": "Limit concurrent GC threads",
        "group": "gc",
        "why": "Concurrent GC work should not compete with the main tick loop.",
        "enabled": True,
        "recommended": True,
    })

    for flag, label, group, why in EXTRA_FLAGS:
        recommended = True
        if flag.startswith("-XX:+UseStringDeduplication"):
            recommended = heap_gb >= 6
        plan.append({
            "flag": flag,
            "label": label,
            "group": group,
            "why": why,
            "enabled": recommended,
            "recommended": recommended,
        })

    if host.get("available_ram_gb", 0) and host["available_ram_gb"] < heap_gb + 1:
        # AlwaysPreTouch commits the whole heap immediately; on a tight host
        # that turns a slow start into a failed one.
        for entry in plan:
            if entry["flag"] == "-XX:+AlwaysPreTouch":
                entry["enabled"] = False
                entry["recommended"] = False
                entry["why"] += (
                    " Disabled here: this host does not have enough free memory "
                    "to commit the whole heap up front."
                )
    return plan


PROPERTY_TUNING = [
    ("view-distance", "8",
     "Chunks sent to each player. The single biggest server-side cost on a "
     "modded pack; 8 is a good balance, 6 if the CPU is weak."),
    ("simulation-distance", "6",
     "How far entities and blocks actually tick. Lower is much cheaper and "
     "players rarely notice."),
    ("max-tick-time", "-1",
     "Disables the watchdog that kills the server on a slow tick. Big packs "
     "routinely exceed the default during chunk generation and world load."),
    ("sync-chunk-writes", "false",
     "Writes chunks asynchronously. Noticeably smoother on spinning disks."),
    ("network-compression-threshold", "256",
     "Compress larger packets only; modded servers send a lot of small ones."),
    ("entity-broadcast-range-percentage", "80",
     "Slightly narrows entity tracking range, cutting packet volume."),
]


def build_property_plan(host: dict, mod_count: int) -> list[dict]:
    cpus = host.get("cpu_count") or 2
    plan = []
    for key, value, why in PROPERTY_TUNING:
        actual = value
        if key == "view-distance":
            actual = "6" if (cpus <= 2 or mod_count > 300) else "8"
        if key == "simulation-distance":
            actual = "4" if (cpus <= 2 or mod_count > 300) else "6"
        plan.append({
            "key": key,
            "value": actual,
            "why": why,
            "enabled": True,
            "recommended": True,
        })
    return plan


def render_jvm_args(flags: list[str], heap_gb: float, *, xms_equals_xmx: bool = True
                    ) -> str:
    """Produce user_jvm_args.txt.

    Xms is set equal to Xmx on purpose: a heap that grows on demand causes
    repeated full collections during the exact period a modded server is
    busiest, which is world load.
    """
    heap_mb = int(heap_gb * 1024)
    xms = heap_mb if xms_equals_xmx else max(1024, heap_mb // 2)
    lines = [
        "# Managed by BlessForge -- edit through the Optimize tab, or by hand.",
        f"-Xms{xms}M",
        f"-Xmx{heap_mb}M",
    ]
    lines.extend(flags)
    return "\n".join(lines) + "\n"


async def read_current_jvm_args(server_id: str) -> dict:
    """Parse the instance's existing user_jvm_args.txt back into structure."""
    try:
        raw = await crafty.read_file(server_id, "user_jvm_args.txt")
    except crafty.CraftyError:
        return {"exists": False, "flags": [], "xmx_mb": 0, "xms_mb": 0, "raw": ""}

    flags = []
    xmx = xms = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"-Xmx(\d+)([GgMmKk])?", line)
        if m:
            xmx = _to_mb(m.group(1), m.group(2))
            continue
        m = re.match(r"-Xms(\d+)([GgMmKk])?", line)
        if m:
            xms = _to_mb(m.group(1), m.group(2))
            continue
        flags.append(line)
    return {"exists": True, "flags": flags, "xmx_mb": xmx, "xms_mb": xms, "raw": raw}


def _to_mb(value: str, unit: str | None) -> int:
    n = int(value)
    unit = (unit or "M").upper()
    return {"G": n * 1024, "M": n, "K": max(1, n // 1024)}.get(unit, n)
