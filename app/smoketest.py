"""Start a server once, watch it boot, stop it, and say what happened.

A freshly installed server has never run, so it has no log -- and Diagnose
reads logs. That is the blind spot this closes: the moment when everything
that is wrong with a pack is still completely invisible. A roulette-built
server here reported no findings at all while carrying eleven mods its loader
could not load, and would have reported nothing until somebody started it and
read the console themselves.

So: start it, watch the console until the loader either finishes or gives up,
stop it again, and run the ordinary log analysis over what it produced. The
server is left exactly as it was found -- stopped -- because this is a
question, not a deployment.
"""
from __future__ import annotations

import asyncio
import re
import time

from app import crafty, diagnostics
from app.jobs import Job

# "Done (12.345s)! For help, type "help"" is vanilla's line for a finished
# boot; the loader ones catch a failure before the game gets that far.
_READY = re.compile(r'Done \([\d.]+s\)!|For help, type "help"', re.I)
# Deliberately specific. This used to include a bare `Caused by: java.lang.`,
# which a modded server trips on the way *up*: mods probe for client-only
# classes and catch the miss, and on a box with no outbound DNS every mod that
# version-checks logs a ConnectException chain. Tensura Evolutions reaches
# "Done (25.641s)" with three of those in its log, and the boot test called it
# failed and stopped it mid-load, twice. A failure has to say so in FML's own
# words, or be the JVM giving up.
_FAILED = re.compile(
    r"Failed to start the minecraft server"
    r"|A potential solution has been determined"
    r"|Missing or unsupported mandatory dependencies"
    r"|Loading errors encountered"
    r"|The game crashed whilst"
    r"|Mod loading has failed"
    r"|LoadingFailedException|ModLoadingException"
    r"|---- Minecraft Crash Report ----"
    r'|Exception in thread "main"'
    r"|java\.lang\.OutOfMemoryError"
    r"|\[[^\]]*/FATAL\]",
    re.I,
)


async def run(job: Job, server_id: str, *, timeout: int = 300,
              stop_when_done: bool = True) -> dict:
    """Boot once and report. Never leaves the server running by accident."""
    was = {}
    try:
        was = await crafty.get_server(server_id)
    except Exception:
        pass
    if was.get("running"):
        raise RuntimeError(
            "this server is already running -- stop it first, or read the "
            "console directly instead of booting it again")

    job.set_step("Starting the server", 5)
    # Where this run's output begins. Crafty's console buffer spans restarts,
    # so a previous boot's failure sits in it and would be read as this one's;
    # everything from here on is ours.
    try:
        base = len(await crafty.console_lines(server_id, from_file=True))
    except Exception:
        base = 0
    started_at = time.time()
    await crafty.server_action(server_id, "start_server")

    outcome = "timeout"
    ready_after = None
    lines: list[str] = []
    job.set_step("Watching the boot", 10)

    try:
        while time.time() - started_at < timeout:
            await asyncio.sleep(5)
            try:
                # From the log file, not the live console. Crafty's console
                # buffer holds about seventy lines: on a 253-mod pack it had
                # already scrolled past "Done (13.138s)" by the next poll, so
                # a server that was up got watched for the full five minutes
                # and reported as a timeout. The file read returns the boot.
                lines = await crafty.console_lines(server_id, from_file=True)
            except Exception:
                continue
            # Everything this run has said, not a sliding tail of it. The tail
            # used to be 400 lines, and a big pack keeps logging long after it
            # is up -- Tensura Evolutions pushes "Done (25.641s)" out of a
            # 400-line window within one 5-second poll, so a server that had
            # booted was watched for the full five minutes and called a
            # timeout.
            if len(lines) < base:      # buffer rotated under us
                base = 0
            text = "\n".join(lines[base:])
            elapsed = int(time.time() - started_at)
            job.set_step(f"Watching the boot ({elapsed}s)",
                         10 + 70 * min(elapsed / max(timeout, 1), 1.0))
            if _READY.search(text):
                outcome = "ready"
                ready_after = elapsed
                job.log_line(f"Server reached 'Done' after {elapsed}s")
                break
            if _FAILED.search(text):
                outcome = "failed"
                job.log_line(f"The loader reported a failure after {elapsed}s",
                             "warn")
                break
        else:
            job.log_line(
                f"No 'Done' and no failure within {timeout}s -- either this "
                "pack simply boots slowly, or it is stuck.", "warn")
    finally:
        # Whatever happened above, do not leave a server up that was down.
        if stop_when_done:
            job.set_step("Stopping the server", 85)
            try:
                await crafty.server_action(server_id, "stop_server")
                job.log_line("Server stopped again")
            except Exception as e:
                job.log_line(
                    f"Could not stop the server after the test: {e}. "
                    "It may still be running.", "warn")

    job.set_step("Reading what it wrote", 92)
    analysis = {}
    try:
        analysis = await diagnostics.analyse_logs(server_id)
    except Exception as e:
        job.log_line(f"Could not analyse the log: {e}", "warn")

    findings = analysis.get("findings") or []
    if outcome == "ready" and not findings:
        job.log_line("Booted clean: no loader errors in the log.")
    elif findings:
        job.log_line(f"{len(findings)} issue(s) found in the boot log")

    return {
        "outcome": outcome,          # ready | failed | timeout
        "seconds": ready_after if ready_after is not None
        else int(time.time() - started_at),
        "timeout": timeout,
        "findings": findings,
        "log_tail": "\n".join(lines[-200:]),
        "has_logs": analysis.get("has_logs", False),
        "log_path": analysis.get("log_path"),
        "crash_path": analysis.get("crash_path"),
    }
