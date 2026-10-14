"""Server-jar updates for Paper, Purpur and Folia.

From the server-manager comparison in design/ (item 8). Mods have the sweep;
the server jar itself had nothing, so a Paper server stayed on the build it
was created with until somebody replaced the jar by hand.

`check` compares the build the server runs with the newest build its project
publishes for the same Minecraft version (never a different Minecraft
version: that is a world upgrade, not an update). `apply` does the update
the careful way:

  1. back the world up first (the server's "before risky changes" setting),
  2. stop the server if it is running, and wait until it has,
  3. fetch the new jar (the node first, BlessForge as relay, checksum checked),
  4. point the startup command at it, keeping every JVM flag the owner set,
  5. remove the old jar, record the build, and start the server again if it
     was running.

A scheduled task (`update_jar` in app/schedules.py) runs it on a clock, which
is how "follow new builds" is switched on.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from app import engine, loaders, templates

FAMILIES = ("paper", "purpur", "folia")
STOP_WAIT = 240             # seconds to wait for a server to stop


class JarUpdateError(Exception):
    """Something the user is told (a 400 or 409, with the reason)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class _Quiet:
    """A job for a run with nobody watching (a scheduled task)."""

    def log_line(self, *_a, **_k) -> None:
        pass

    def set_step(self, *_a, **_k) -> None:
        pass


def build_of(filename: str) -> str:
    """The build number in a jar name: paper-1.21.1-123.jar -> 123."""
    m = re.search(r"-(\d+)\.jar$", filename or "")
    return m.group(1) if m else ""


def newer(latest: str, current: str) -> bool:
    if not latest:
        return False
    if not current:
        return True                 # unknown build: the newest is worth offering
    if latest.isdigit() and current.isdigit():
        return int(latest) > int(current)
    return latest != current


async def _facts(server_id: str) -> dict[str, Any]:
    manifest = await engine.read_manifest(server_id)
    server = await engine.get_server(server_id)
    family = loaders.family_of(manifest.get("loader") or "")
    exe = server.get("executable") or ""
    if not family:
        name = exe.lower()
        family = next((f for f in FAMILIES if name.startswith(f)), "")
    mc = manifest.get("minecraft") or ""
    if not mc:
        m = re.search(r"-(\d+\.\d+(?:\.\d+)?)-\d+\.jar$", exe)
        mc = m.group(1) if m else ""
    current = str(manifest.get("loader_version") or "") or build_of(exe)
    return {"manifest": manifest, "server": server, "family": family, "minecraft": mc,
            "current": current, "jar": exe}


async def check(server_id: str) -> dict[str, Any]:
    f = await _facts(server_id)
    out = {"family": f["family"], "minecraft": f["minecraft"], "current": f["current"],
           "jar": f["jar"], "supported": f["family"] in FAMILIES and bool(f["minecraft"]),
           "latest": None, "update_available": False, "checked": time.time()}
    if not out["supported"]:
        out["reason"] = ("Only Paper, Purpur and Folia servers are updated here; mods "
                         "and modpacks have their own updates.")
        return out
    src = await loaders.resolve(f["family"], f["minecraft"])
    if not src:
        out["reason"] = (f"{f['family'].title()} did not answer, or publishes no build "
                         f"for Minecraft {f['minecraft']}.")
        return out
    out["latest"] = str(src.get("loader_version") or "")
    out["update_available"] = newer(out["latest"], f["current"])
    return out


async def _running(server_id: str) -> bool:
    try:
        return bool((await engine.get_stats(server_id)).get("running"))
    except Exception:       # noqa: BLE001
        return False


async def _wait_stopped(server_id: str, timeout: float = STOP_WAIT) -> None:
    deadline = time.monotonic() + timeout
    while await _running(server_id):
        if time.monotonic() > deadline:
            raise JarUpdateError("The server did not stop in time, so the jar was not "
                                 "changed. Stop it by hand and try again.", 409)
        await asyncio.sleep(2)


def _command_for(old_cmd: str, old_jar: str, new_jar: str, family: str) -> str:
    """The startup command with the new jar, every other flag kept."""
    if old_jar and old_cmd and old_jar in old_cmd:
        return old_cmd.replace(old_jar, new_jar)
    heap = " ".join(re.findall(r"-Xm[sx]\S+", old_cmd or "")) or "-Xms1G -Xmx2G"
    return templates.startup(family, jar=new_jar, jvm=heap)


async def apply(server_id: str, job: Any = None, *, force: bool = False) -> dict[str, Any]:
    """Update the server jar to the newest build. Returns what it did."""
    job = job or _Quiet()
    from app import provision, worldbackups
    job.set_step("Checking for a newer build", 5)
    info = await check(server_id)
    if not info["supported"]:
        raise JarUpdateError(info.get("reason") or "This server's jar is not updated here.")
    if not info["latest"]:
        raise JarUpdateError(info.get("reason") or "No build was found.", 409)
    if not info["update_available"] and not force:
        job.log_line(f"Already on the newest build ({info['current']}).")
        return {"updated": False, "current": info["current"], "latest": info["latest"]}

    f = await _facts(server_id)
    family, mc = f["family"], f["minecraft"]
    src = await loaders.resolve(family, mc)
    if not src:
        raise JarUpdateError(f"{family.title()} did not answer; nothing was changed.", 409)
    was_running = await _running(server_id)

    job.set_step("Backing up the world", 15)
    safety = await worldbackups.before_risky(
        server_id, f"updating {family} {info['current'] or '?'} to build {info['latest']}", job)

    if was_running:
        job.set_step("Stopping the server", 35)
        job.log_line("Stopping the server for the update")
        await engine.power(server_id, "stop")
        await _wait_stopped(server_id)

    job.set_step(f"Fetching {family} build {src.get('loader_version')}", 50)
    java = await templates.java_major(family, mc)
    await provision._fetch_loader(job, server_id, family, mc, src, java)

    old_jar, new_jar = f["jar"], src["filename"]
    command = _command_for(f["server"].get("execution_command") or "", old_jar, new_jar, family)
    await engine.update_server(server_id, {"executable": new_jar, "execution_command": command})
    if old_jar and old_jar != new_jar and "/" not in old_jar and old_jar.endswith(".jar"):
        try:
            await engine.delete_paths(server_id, [old_jar])
        except Exception as e:      # noqa: BLE001 -- a leftover jar harms nothing
            job.log_line(f"The old jar {old_jar} could not be removed ({e})", "warn")
    manifest = f["manifest"]
    manifest["loader_version"] = str(src.get("loader_version") or "")
    try:
        await engine.write_manifest(server_id, manifest)
    except Exception:       # noqa: BLE001 -- an unmanaged server has no manifest to keep
        pass
    job.log_line(f"{family.title()} {info['current'] or '?'} -> {src.get('loader_version')} "
                 f"({new_jar})")

    if was_running:
        job.set_step("Starting the server again", 90)
        await engine.power(server_id, "start")
    job.set_step("Done", 100)
    return {"updated": True, "from": info["current"], "to": str(src.get("loader_version")),
            "jar": new_jar, "backup": (safety or {}).get("id"), "restarted": was_running}
