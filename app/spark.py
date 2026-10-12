"""spark -- the Minecraft profiler -- driven from the panel.

spark (https://github.com/lucko/spark, by lucko) is GPL-3.0. BlessForge does
not bundle, link or modify any of it: on the operator's request it installs
spark's own published jar from CurseForge (Modrinth when CurseForge cannot
provide it), exactly like any other mod, and from then on only types spark's
documented console commands and reads what they print. Running a GPL program
is unrestricted; the license's conditions attach to distributing it, which
BlessForge never does. The UI credits it and links its source.

What it is used for:

  * a health snapshot -- TPS, tick times (MSPT), CPU, memory, GC -- read
    from `spark tps` and `spark health show --memory`;
  * a recorded profile: `spark profiler start`, a fixed wait, then
    `spark profiler stop`. By default spark uploads the result to its public
    viewer (spark.lucko.me) and prints the link; with `--save-to-file` it is
    kept on the server instead. The operator picks; the default is to keep
    it local, because an upload publishes the profile to anyone with the
    link;
  * the snapshot numbers feed the Tune analysis (app/perf.py): a server
    holding 20 TPS with headroom needs nothing; one whose 95th-percentile
    tick is over 50 ms is overloaded, and that changes what is worth
    recommending.

Reports are kept in `.blessforge-spark.json` in the server folder, newest
first, so they live and die with the server they describe.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from app import engine, loaders
from app import mods as modmgr
from app.jobs import Job

SPARK_CURSEFORGE = 361579           # the spark mod (Forge, NeoForge, Fabric)
SPARK_MODRINTH = "spark"
REPORTS_FILE = ".blessforge-spark.json"
MAX_REPORTS = 20
SOURCE_URL = "https://github.com/lucko/spark"
LICENSE = "GPL-3.0"

_VIEWER = re.compile(r"https://spark\.lucko\.me/[A-Za-z0-9]+")
_SAVED = re.compile(r"(\S+\.sparkprofile)")
_FLOATS = re.compile(r"\*?(\d+(?:\.\d+)?)")
_QUAD = re.compile(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")
_PCT = re.compile(r"(\d+(?:\.\d+)?)%")
_MEM = re.compile(r"(\d+(?:\.\d+)?)\s*(GB|MB|KB)\s*/\s*(\d+(?:\.\d+)?)\s*(GB|MB|KB)", re.I)
_GC = re.compile(r"([A-Z][\w ]*?(?:Generation|Collection|Cycles|Pauses|ZGC[\w ]*))\s*:?\s*"
                 r"(?:avg\s*)?([\d.]+)\s*ms\s*avg[^\n]*?([\d.]+)\s*(?:collections? )?per minute",
                 re.I)


class SparkError(RuntimeError):
    pass


# --- is it there -----------------------------------------------------------------


async def status(server_id: str) -> dict:
    manifest = await engine.read_manifest(server_id)
    family = loaders.family_of(manifest.get("loader") or "")
    mc = manifest.get("minecraft") or ""
    out = {"loader": family, "minecraft": mc, "installed": False, "where": None,
           "file": None, "supported": family not in ("", "vanilla"),
           "license": LICENSE, "source": SOURCE_URL,
           "reports": await reports(server_id)}
    if family in ("paper", "purpur", "folia") and _at_least(mc, (1, 21)):
        # Paper ships spark built in since 1.21; the command is just there.
        out.update(installed=True, where="bundled")
        return out
    folder = "plugins" if family in loaders.PLUGIN_FAMILIES else "mods"
    try:
        names = await engine.list_dir(server_id, folder)
    except engine.EngineError:
        names = {}
    for name in names:
        if re.match(r"spark[-_]", name, re.I) and name.lower().endswith(".jar"):
            out.update(installed=True, where=folder, file=name)
            break
    if family == "vanilla":
        out["why_not"] = ("Vanilla has no mod loader, so spark cannot run on it. "
                          "Paper (which bundles it) or Fabric can.")
    return out


def _at_least(mc: str, want: tuple[int, int]) -> bool:
    try:
        parts = tuple(int(p) for p in (mc or "0.0").split(".")[:2])
    except ValueError:
        return False
    if parts[0] >= 26:          # the year-numbered releases are all newer
        return True
    return parts >= want


async def install(job: Job, server_id: str) -> dict:
    """spark's own published build for this loader and version."""
    st = await status(server_id)
    if st["installed"]:
        return {"installed": True, "already": True, "where": st["where"]}
    if not st["supported"]:
        raise SparkError(st.get("why_not") or "spark cannot run on this server")
    family, mc = st["loader"], st["minecraft"]
    from app import curseforge, modrinth
    if family in loaders.PLUGIN_FAMILIES:
        # The plugin build lives on Modrinth as the same project.
        versions = await modrinth.list_versions(SPARK_MODRINTH, game_version=mc,
                                                loaders=["paper", "bukkit", "spigot"])
        if not versions:
            versions = await modrinth.list_versions(SPARK_MODRINTH,
                                                    loaders=["paper", "bukkit", "spigot"])
        if not versions:
            raise SparkError("no spark plugin build was found on Modrinth")
        v = versions[0]
        await modmgr.add_mod(server_id, source="modrinth", project_id=v["mod_id"],
                             file_id=v["file_id"], directory="plugins")
        return {"installed": True, "where": "plugins", "source": "modrinth",
                "version": v.get("version_number")}
    files = [f for f in await curseforge.list_files(SPARK_CURSEFORGE, game_version=mc,
                                                     mod_loader=family)
             if not f.get("distribution_blocked")]
    if files:
        f = files[0]
        await modmgr.add_mod(server_id, source="curseforge", project_id=SPARK_CURSEFORGE,
                             file_id=f["file_id"], directory="mods")
        return {"installed": True, "where": "mods", "source": "curseforge",
                "version": f.get("display_name")}
    versions = await modrinth.list_versions(SPARK_MODRINTH, game_version=mc, loader=family)
    if not versions:
        raise SparkError(f"no spark build for {family} {mc} on CurseForge or Modrinth")
    v = versions[0]
    await modmgr.add_mod(server_id, source="modrinth", project_id=v["mod_id"],
                         file_id=v["file_id"], directory="mods")
    return {"installed": True, "where": "mods", "source": "modrinth",
            "version": v.get("version_number")}


# --- talking to it -------------------------------------------------------------------


async def _command_output(server_id: str, command: str, *, wait: float = 6.0,
                          until: re.Pattern | None = None) -> list[str]:
    """Send a console command and collect the lines that follow it."""
    before = len(await engine.console_lines(server_id))
    await engine.send_command(server_id, command)
    deadline = time.time() + wait
    lines: list[str] = []
    while time.time() < deadline:
        await asyncio.sleep(1.0)
        now = await engine.console_lines(server_id)
        lines = now[before:] if len(now) >= before else now[-200:]
        if until and any(until.search(ln) for ln in lines):
            break
    return lines


def _strip(line: str) -> str:
    """'[12:00:00] [Server thread/INFO]: [⚡] TPS ...' -> 'TPS ...'"""
    line = re.sub(r"^\[[^\]]*\]\s*(?:\[[^\]]*\]\s*)*:?\s*", "", line)
    return re.sub(r"^\[\S{1,4}\]\s*", "", line).strip()


def parse_health(lines: list[str]) -> dict:
    """Numbers out of `spark tps` and `spark health show --memory`.

    Tolerant on purpose: the wording has moved between spark versions, so
    each figure is found by the heading above it, and a figure that is not
    there is simply absent rather than an error.
    """
    text = [_strip(ln) for ln in lines]
    out: dict = {}

    def after(heading: str, n: int = 3) -> list[str]:
        for i, ln in enumerate(text):
            if heading.lower() in ln.lower():
                return [t for t in text[i + 1:i + 1 + n] if t]
        return []

    tps = after("TPS from last", 1)
    if tps:
        vals = [float(v) for v in _FLOATS.findall(tps[0])][:5]
        if vals:
            out["tps"] = dict(zip(("5s", "10s", "1m", "5m", "15m"), vals))
    ticks = after("Tick durations", 1)
    if ticks:
        quads = _QUAD.findall(ticks[0])
        if quads:
            mn, med, p95, mx = (float(v) for v in quads[0])
            out["mspt"] = {"min": mn, "median": med, "p95": p95, "max": mx,
                           "window": "10s"}
            if len(quads) > 1:
                mn, med, p95, mx = (float(v) for v in quads[1])
                out["mspt_1m"] = {"min": mn, "median": med, "p95": p95, "max": mx}
    for ln in after("CPU usage", 3):
        pcts = [float(v) for v in _PCT.findall(ln)]
        if "system" in ln.lower() and pcts:
            out["cpu_system"] = pcts[0]
        elif "process" in ln.lower() and pcts:
            out["cpu_process"] = pcts[0]
    for i, ln in enumerate(text):
        if "memory" in ln.lower():
            for cand in text[i:i + 4]:
                m = _MEM.search(cand)
                if m:
                    scale = {"GB": 1024, "MB": 1, "KB": 1 / 1024}
                    used = float(m.group(1)) * scale[m.group(2).upper()]
                    total = float(m.group(3)) * scale[m.group(4).upper()]
                    out["memory_mb"] = {"used": round(used), "max": round(total),
                                        "percent": round(used / total * 100, 1) if total else None}
                    break
            if "memory_mb" in out:
                break
    gcs = []
    for ln in text:
        m = _GC.search(ln)
        if m:
            gcs.append({"collector": m.group(1).strip(), "avg_ms": float(m.group(2)),
                        "per_minute": float(m.group(3))})
    if gcs:
        out["gc"] = gcs
    return out


async def health(job: Job | None, server_id: str) -> dict:
    """A snapshot of how the server is coping right now."""
    st = await engine.get_stats(server_id)
    if not st.get("running"):
        raise SparkError("the server is not running -- start it, let it settle, then "
                         "take the snapshot")
    lines = await _command_output(server_id, "spark tps", wait=5,
                                  until=re.compile(r"CPU usage", re.I))
    lines += await _command_output(server_id, "spark health show --memory", wait=8,
                                   until=re.compile(r"Disk|Network", re.I))
    numbers = parse_health(lines)
    if not numbers:
        raise SparkError("spark did not answer -- is it installed, and has the server "
                         "finished starting?")
    report = {"kind": "health", "at": time.time(), "numbers": numbers,
              "players": st.get("online"), "advice": advise(numbers)}
    await _save_report(server_id, report)
    return report


async def profile(job: Job, server_id: str, *, seconds: int = 60,
                  upload: bool = False) -> dict:
    """Record a profile for `seconds`, then keep it on the server or upload it."""
    seconds = max(15, min(int(seconds), 600))
    st = await engine.get_stats(server_id)
    if not st.get("running"):
        raise SparkError("the server is not running")
    job.set_step("Starting the profiler", 5)
    started = await _command_output(server_id, "spark profiler start", wait=6,
                                    until=re.compile(r"[Pp]rofiler (?:now )?(?:running|started)"))
    if any("already" in _strip(ln).lower() for ln in started):
        await engine.send_command(server_id, "spark profiler cancel")
        await asyncio.sleep(2)
        await engine.send_command(server_id, "spark profiler start")
    for i in range(seconds):
        await asyncio.sleep(1)
        if i % 5 == 0:
            job.set_step(f"Profiling ({i}/{seconds}s)", 5 + 85 * i / seconds)
    job.set_step("Stopping the profiler", 92)
    stop = "spark profiler stop --comment BlessForge" + ("" if upload else " --save-to-file")
    lines = await _command_output(server_id, stop, wait=45,
                                  until=_VIEWER if upload else _SAVED)
    url = next((m.group(0) for ln in lines for m in [_VIEWER.search(ln)] if m), None)
    saved = next((m.group(1) for ln in lines for m in [_SAVED.search(_strip(ln))] if m), None)
    if not url and not saved:
        raise SparkError("spark did not report where the profile went -- the console "
                         "has its own message")
    numbers = {}
    try:
        numbers = (await health(None, server_id))["numbers"]
    except Exception:       # noqa: BLE001 -- the profile itself is what was asked for
        pass
    report = {"kind": "profile", "at": time.time(), "seconds": seconds,
              "viewer_url": url, "file": _server_relative(saved) if saved else None,
              "uploaded": bool(url), "numbers": numbers, "players": st.get("online"),
              "advice": advise(numbers)}
    await _save_report(server_id, report)
    return report


def _server_relative(path: str) -> str:
    path = path.replace("\\", "/")
    for marker in ("/server/", "/config/spark/"):
        if marker in path:
            return ("config/spark/" + path.split("/config/spark/", 1)[1]
                    if "/config/spark/" in path else path.split(marker, 1)[1])
    return path.lstrip("./")


# --- what the numbers say ---------------------------------------------------------------


def advise(numbers: dict) -> list[dict]:
    """Plain reading of a snapshot. Thresholds are the community's: 20 TPS is
    full speed, a tick has 50 ms, and a GC that runs often or long is the
    heap talking."""
    out: list[dict] = []
    tps = (numbers.get("tps") or {}).get("1m") or (numbers.get("tps") or {}).get("10s")
    mspt = numbers.get("mspt") or {}
    if tps is not None and tps < 19.5:
        out.append({"level": "bad" if tps < 17 else "warn",
                    "title": f"Running at {tps:g} TPS, below the 20 it should hold",
                    "detail": "Every tick is taking longer than its 50 ms. A recorded "
                              "profile shows which mod or task is eating the time."})
    if mspt.get("p95") is not None:
        p95 = mspt["p95"]
        if p95 > 50:
            out.append({"level": "bad",
                        "title": f"1 tick in 20 takes {p95:g} ms (over the 50 ms budget)",
                        "detail": "That is lag players feel. Lower view and simulation "
                                  "distance, pre-generate the world, and profile it."})
        elif p95 > 40:
            out.append({"level": "warn", "title": f"Tick times are close to the budget "
                                                  f"({p95:g} ms at the 95th percentile)",
                        "detail": "Fine now, but more players or a new area will tip it "
                                  "over."})
        else:
            out.append({"level": "ok", "title": f"Ticks have headroom ({p95:g} ms at the "
                                                f"95th percentile, budget 50)",
                        "detail": "Nothing here needs fixing."})
    mem = numbers.get("memory_mb") or {}
    if mem.get("percent") and mem["percent"] > 90:
        out.append({"level": "warn", "title": f"Heap {mem['percent']:g}% full",
                    "detail": "Close to the ceiling: either the heap is too small for this "
                              "pack, or something is leaking. If it climbs between "
                              "snapshots without players, it is a leak."})
    for gc in numbers.get("gc") or []:
        if "old" in gc["collector"].lower() and gc["per_minute"] > 1:
            out.append({"level": "bad", "title": f"{gc['collector']}: "
                                                 f"{gc['per_minute']:g} per minute",
                        "detail": "Full collections this often mean the heap is too small "
                                  "for what is loaded -- give it more, or find the leak."})
        elif gc["avg_ms"] > 100:
            out.append({"level": "warn", "title": f"{gc['collector']} pauses average "
                                                  f"{gc['avg_ms']:g} ms",
                        "detail": "Long pauses show as stutter. The Tune tab's GC "
                                  "settings are worth a look."})
    if numbers.get("cpu_process") is not None and numbers["cpu_process"] > 90:
        out.append({"level": "warn", "title": f"The server uses {numbers['cpu_process']:g}% "
                                              "of its CPU",
                    "detail": "It is CPU-bound; more players will slow ticks down."})
    return out


# --- keeping them ---------------------------------------------------------------------


async def reports(server_id: str) -> list[dict]:
    try:
        data = json.loads(await engine.read_file(server_id, REPORTS_FILE))
        return data if isinstance(data, list) else []
    except Exception:       # noqa: BLE001 -- none yet
        return []


async def _save_report(server_id: str, report: dict) -> None:
    items = [report] + await reports(server_id)
    await engine.write_file(server_id, REPORTS_FILE,
                            json.dumps(items[:MAX_REPORTS], indent=1))


async def latest_numbers(server_id: str, max_age: float = 3 * 86400) -> dict | None:
    """The newest snapshot's numbers, for the Tune analysis -- if recent."""
    for r in await reports(server_id):
        if r.get("numbers") and time.time() - float(r.get("at") or 0) < max_age:
            return {**r["numbers"], "at": r["at"]}
    return None
