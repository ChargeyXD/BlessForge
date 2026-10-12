#!/usr/bin/env python3
"""Every template, really installed and really booted, through a node agent.

Not part of the offline loop: this downloads each loader from its own
project (Mojang, PaperMC, PurpurMC, FabricMC, MinecraftForge, NeoForged),
runs the Forge/NeoForge installers, and boots a real Minecraft server for
each -- several minutes and a few hundred MB.

    python dev/tools/boot_templates.py                 # all seven
    python dev/tools/boot_templates.py paper forge     # some
    BF_JAVA_8=... BF_JAVA_17=... BF_JAVA_21=... BF_JAVA_25=...   # where the JREs are

The chain is the production one apart from isolation (dev/tools/_agentstack.py):
the panel on the agent engine, a registered node, the agent's PROCESS runtime
with one local JRE per Java major standing in for the runtime images. Create
goes through POST /api/provision/server and the boot through POST
/api/instances/{id}/smoke-test, exactly as the UI calls them; each server is
deleted afterwards.
"""
import asyncio
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from _agentstack import follow, free_port, stack  # noqa: E402

# (template, preferred Minecraft version, why this one)
CASES = [
    ("vanilla", "26.1", "Java 25: the year-numbered releases"),
    ("paper", "1.21.4", "Java 21"),
    ("purpur", "1.21.1", "Java 21"),
    ("folia", "1.21.4", "Java 21"),
    ("fabric", "1.20.1", "Java 17"),
    ("forge", "1.20.1", "Java 17, installer, run.sh + args files"),
    ("forge", "1.12.2", "Java 8, installer, the pre-1.17 runnable jar"),
    ("neoforge", "1.21.1", "Java 21, installer"),
]


async def pick_version(family: str, preferred: str) -> str:
    from app import loaders
    try:
        versions = await loaders.versions_for(family)
    except Exception:       # noqa: BLE001
        return preferred
    if preferred in versions:
        return preferred
    major = ".".join(preferred.split(".")[:2])
    same = sorted((v for v in versions if v.startswith(major)), key=loaders._version_key)
    return same[-1] if same else preferred


async def boot_one(c, family: str, mc: str, why: str) -> dict:
    row = {"template": family, "minecraft": mc, "why": why}
    t0 = time.time()
    r = await c.post("/api/provision/server", json={
        "name": f"Boot {family} {mc}", "loader": family, "minecraft": mc,
        "port": free_port(), "mem_max": 2, "online_mode": False})
    snap = await follow(c, r.json()["job_id"], 1800)
    sid = (snap.get("result") or {}).get("server_id") or snap.get("server_id")
    row["install"] = snap.get("status")
    row["install_seconds"] = int(time.time() - t0)
    if snap.get("status") != "done" or not sid:
        row["error"] = snap.get("error")
        row["log"] = [ln["message"] for ln in (snap.get("log") or [])][-8:]
        return row
    inst = next(i for i in (await c.get("/api/instances")).json()["items"]
                if i["server_id"] == sid)
    row["java"] = inst.get("java_major")
    row["command"] = inst.get("execution_command")
    t1 = time.time()
    r = await c.post(f"/api/instances/{sid}/smoke-test", json={"timeout": 600})
    boot = await follow(c, r.json()["job_id"], 900)
    result = boot.get("result") or {}
    row["boot"] = result.get("outcome") or boot.get("status")
    row["boot_seconds"] = int(time.time() - t1)
    if row["boot"] != "ready":
        row["log_tail"] = (result.get("log_tail") or "")[-1200:]
    else:
        row["console"] = await command_round_trip(c, sid)
    await c.request("DELETE", f"/api/instances/{sid}")
    return row


async def command_round_trip(c, sid: str) -> str:
    """Start it, type `list` into its console, read the answer, stop it.

    The boot test reads the log file; this proves the other direction --
    a command reaching the server's stdin (through the container attach on
    the Docker runtime) and its reply coming back -- and that stop is
    graceful.
    """
    # The node's console buffer spans restarts, so the boot test's own
    # "Done" and "Stopping" are still in it. Everything is read AFTER a
    # marker this run produced: the agent's "[BlessForge] Starting" line for
    # the boot, and its "> list" echo for the answer and the stop.
    async def lines() -> list[str]:
        r = await c.get(f"/api/instances/{sid}/console", params={"source": "buffer"})
        return r.json().get("lines") or []

    def after(ls: list[str], marker: str) -> list[str]:
        idx = max((i for i, ln in enumerate(ls) if marker in ln), default=None)
        return [] if idx is None else ls[idx + 1:]

    async def until(marker, wanted, seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if any(wanted in ln for ln in after(await lines(), marker)):
                return True
            await asyncio.sleep(2)
        return False

    await c.post(f"/api/instances/{sid}/action/start_server")
    if not await until("[BlessForge] Starting", "Done (", 600):
        return "did not come up again"
    await c.post(f"/api/instances/{sid}/command", json={"command": "list"})
    answered = await until("> list", "players online", 60)
    await c.post(f"/api/instances/{sid}/action/stop_server")
    # "Stopping the server" is what the `stop` COMMAND prints -- so this also
    # proves the stop went in through stdin rather than as a signal.
    stopped = await until("> list", "Stopping the server", 120)
    return (GOOD_CONSOLE if answered and stopped else
            f"answered={answered} stopped_gracefully={stopped}")


async def main(only: list[str]) -> bool:
    runtime = "process"
    if "--docker" in only:
        runtime = "docker"
        only = [o for o in only if o != "--docker"]
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bf-boot-"))
    report = []
    async with stack(tmp, runtime=runtime) as s:
        print(f"runtime: {runtime}", "JREs:" if s.javas else "",
              json.dumps(s.javas, indent=1) if s.javas else "", flush=True)
        for family, preferred, why in CASES:
            if only and family not in only:
                continue
            mc = await pick_version(family, preferred)
            row = await boot_one(s.client, family, mc, why)
            ok = _ok(row)
            print(f"{'BOOTED' if ok else 'FAILED'}  {family} {mc} ({why})  "
                  f"install {row.get('install_seconds')}s, boot {row.get('boot_seconds')}s  "
                  f"{row.get('console') or ''}{row.get('error') or ''}", flush=True)
            report.append(row)
    out = tmp / "boot-report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    booted = sum(1 for r in report if _ok(r))
    print(f"\n{booted}/{len(report)} templates booted. Report: {out}")
    return booted == len(report) and bool(report)


GOOD_CONSOLE = "list answered, stopped gracefully"


def _ok(row: dict) -> bool:
    """Booted, and -- when it got that far -- answered a command and stopped."""
    return row.get("boot") == "ready" and row.get("console", GOOD_CONSOLE) == GOOD_CONSOLE


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main(sys.argv[1:])) else 1)
