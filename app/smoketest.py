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

from app import engine, diagnostics
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


# A boot that stops logging after an exception has hung, not slowed down.
FATAL_QUIET_S = 120
HUNG_AFTER_ERROR_S = 300
HUNG_SILENT_S = 900
_ERROR_TAIL = re.compile(r"/(?:ERROR|FATAL)\]|^Caused by: |^\S*Exception: ", re.M)


def _last_error(text: str) -> str | None:
    """The most telling line of a boot that died without a recognised failure
    line: its last "Caused by", else its last ERROR."""
    lines = text.splitlines()
    for pattern in (r"Caused by: ", r"/(?:ERROR|FATAL)\]|Exception in thread"):
        for line in reversed(lines):
            if re.search(pattern, line):
                return line.strip()[:400]
    return None


async def run(job: Job, server_id: str, *, timeout: int = 300,
              stop_when_done: bool = True) -> dict:
    """Boot once and report. Never leaves the server running by accident."""
    # From the stats, which carry `running`; the server record does not, so
    # reading it there meant this guard never fired.
    was = {}
    try:
        was = await engine.get_stats(server_id)
    except Exception:
        pass
    if was.get("running"):
        raise RuntimeError(
            "this server is already running -- stop it first, or read the "
            "console directly instead of booting it again")

    job.set_step("Starting the server", 5)
    # Where this run's output begins. The node's console buffer spans restarts,
    # so a previous boot's failure sits in it and would be read as this one's;
    # everything from here on is ours.
    try:
        previous = await engine.console_lines(server_id, from_file=True)
    except Exception:
        previous = []
    base = len(previous)
    previous_first = previous[0] if previous else None
    started_at = time.time()
    await engine.power(server_id, "start")

    outcome = "timeout"
    oom = False
    last_signature: tuple = ()
    last_growth = time.time()
    ready_after = None
    lines: list[str] = []
    failure_line: str | None = None
    failure_at: float | None = None
    job.set_step("Watching the boot", 10)

    try:
        while time.time() - started_at < timeout:
            await asyncio.sleep(5)
            try:
                # From the log file, not the live console. A console ring
                # buffer is finite: on a 253-mod pack one had
                # already scrolled past "Done (13.138s)" by the next poll, so
                # a server that was up got watched for the full five minutes
                # and reported as a timeout. The file read returns the boot.
                lines = await engine.console_lines(server_id, from_file=True)
            except Exception:
                continue
            # Everything this run has said, not a sliding tail of it. The tail
            # used to be 400 lines, and a big pack keeps logging long after it
            # is up -- Tensura Evolutions pushes "Done (25.641s)" out of a
            # 400-line window within one 5-second poll, so a server that had
            # booted was watched for the full five minutes and called a
            # timeout.
            # A start rotates latest.log, so a different first line means a
            # new file: all of it is this run's, however long it already is.
            if len(lines) < base or (lines and previous_first is not None
                                     and lines[0] != previous_first):
                base = 0
            text = "\n".join(lines[base:])
            # When the log last moved. A busy boot keeps writing; a hung one
            # stops. (Count and last line together: a capped read keeps the
            # count still while the text moves on.)
            signature = (len(lines), lines[-1] if lines else "")
            if not lines:
                # Nothing readable this time (rotated mid-read, or too big to
                # read): that says nothing about the server, so it is not
                # silence either.
                last_growth = time.time()
            elif signature != last_signature:
                last_signature, last_growth = signature, time.time()
            silent = time.time() - last_growth
            elapsed = int(time.time() - started_at)
            job.set_step(f"Watching the boot ({elapsed}s)",
                         10 + 70 * min(elapsed / max(timeout, 1), 1.0))
            if _READY.search(text):
                outcome = "ready"
                ready_after = elapsed
                job.log_line(f"Server reached 'Done' after {elapsed}s")
                if failure_line:
                    job.log_line("An earlier line looked fatal but was not -- the "
                                 f"server booted past it: {failure_line[:160]}")
                break
            m = _FAILED.search(text)
            if m and failure_line is None:
                start = text.rfind("\n", 0, m.start()) + 1
                end = text.find("\n", m.end())
                failure_line = text[start: end if end >= 0 else None].strip()
                failure_at = time.time()
                job.log_line(f"A failure-looking line after {elapsed}s: "
                             f"{failure_line[:200]}", "warn")
            # A server that has exited has failed its boot, whatever its log
            # said. This was only asked after a recognised failure line, and
            # a crash without one went unnoticed: Beyond Depth died 30 seconds
            # in on a mixin error and was watched for 36 minutes.
            running = True
            stats = {}
            try:
                stats = await engine.get_stats(server_id)
                running = bool(stats.get("running")) and not stats.get("crashed")
            except Exception:
                pass
            if not running:
                outcome = "failed"
                if (stats.get("last_exit") or {}).get("oom"):
                    # Killed from outside for its memory limit: the log just
                    # stops, and its last error is somebody else's.
                    oom = True
                    failure_line = ("Killed for running out of memory: the JVM used more "
                                    "than its container's limit")
                else:
                    failure_line = failure_line or _last_error(text)
                job.log_line(f"The server exited after {elapsed}s", "warn")
                break
            # A line is only a failed boot if the server then actually dies.
            # The pattern alone stopped healthy boots: a 1.12 pack logs FATAL
            # lines it survives, and was killed mid-load by this test.
            # A fatal-looking line is fatal once the log has stopped for two
            # minutes after it. Wall time alone killed 2BROKE4LORE while it
            # was still "Preparing spawn area" on a slow box.
            if failure_line:
                if failure_at and time.time() - failure_at > FATAL_QUIET_S and silent > FATAL_QUIET_S:
                    outcome = "failed"
                    job.log_line("Nothing logged for two minutes after that line -- "
                                 "treating it as fatal.", "warn")
                    break
            elif silent > HUNG_AFTER_ERROR_S and _ERROR_TAIL.search(
                    "\n".join(lines[-60:])):
                # Hung after an exception with no line in FML's own words:
                # BMC4 sat on "Failed to load registries" for 29 minutes.
                outcome = "failed"
                failure_line = _last_error(text)
                job.log_line(f"No output for {int(silent)}s after an error -- the boot "
                             "has hung.", "warn")
                break
            elif silent > HUNG_SILENT_S:
                outcome = "failed"
                failure_line = _last_error(text) or "no output at all for 15 minutes"
                job.log_line(f"No output for {int(silent)}s -- the boot has hung.", "warn")
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
                await engine.power(server_id, "stop")
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
        "failure_line": failure_line,
        "oom": oom,
        "findings": findings,
        "log_tail": "\n".join(lines[-200:]),
        "has_logs": analysis.get("has_logs", False),
        "log_path": analysis.get("log_path"),
        "crash_path": analysis.get("crash_path"),
    }
