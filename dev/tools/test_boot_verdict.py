#!/usr/bin/env python3
"""Does a boot log actually say the server failed?

Both detectors here used to answer "yes" for a server that was booting
perfectly well, because a modded server logs caught exceptions on the way up:
mods probe for client-only classes and swallow the miss, and on a box with no
outbound DNS every mod that version-checks logs a ConnectException chain.
Tensura Evolutions reaches `Done (25.641s)` with three such stacks in its log,
and it was reported failed and stopped mid-load twice.

The fixtures are real logs from that server -- one boot that finished and one
that genuinely could not, from the same pack minutes apart.

Offline. No network, no Docker, no backend.

    python3 dev/tools/test_boot_verdict.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.diagnostics import _scan_missing_client_class  # noqa: E402
from app.smoketest import _FAILED, _READY  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
OK = (FIXTURES / "boot-ok-with-caught-exceptions.log").read_text()
BAD = (FIXTURES / "boot-failed-missing-dependency.log").read_text()

out = []


def check(name, cond, extra=""):
    out.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


# --- the fixtures are what we think they are -----------------------------
check("the good log records a finished boot", "Done (" in OK)
check("the good log does contain caught exceptions",
      OK.count("Caused by: java.lang.") >= 2,
      f"{OK.count('Caused by: java.lang.')} of them")
check("the bad log never finished booting", "Done (" not in BAD)

# --- smoke test verdicts --------------------------------------------------
check("a finished boot is not called a failure", not _FAILED.search(OK),
      (_FAILED.search(OK) or [None]) and (_FAILED.search(OK).group(0)
                                          if _FAILED.search(OK) else ""))
check("a finished boot is recognised as ready", bool(_READY.search(OK)))
check("a real loader failure is still caught", bool(_FAILED.search(BAD)),
      _FAILED.search(BAD).group(0) if _FAILED.search(BAD) else "MISSED")
check("a failed boot is not called ready", not _READY.search(BAD))

# The specific phrases a failure speaks in. Losing one of these silently is
# how a broken pack starts reporting as healthy.
for phrase in [
    "Failed to start the minecraft server",
    "A potential solution has been determined",
    "Missing or unsupported mandatory dependencies",
    "Loading errors encountered",
    "Mod loading has failed",
    "---- Minecraft Crash Report ----",
    'Exception in thread "main"',
    "java.lang.OutOfMemoryError: Java heap space",
    "[main/FATAL] [net.neoforged.fml/]: boom",
]:
    check(f"still fatal: {phrase[:46]}", bool(_FAILED.search(phrase)))

# ...and the ones that must not be, because a healthy server logs them.
for phrase in [
    "Caused by: java.lang.ClassNotFoundException: net.minecraft.client.ParticleStatus",
    "Caused by: java.lang.IllegalStateException: Expected BEGIN_OBJECT but was STRING",
    "Caused by: java.net.ConnectException",
    "Caused by: java.nio.channels.UnresolvedAddressException",
    "[modloading-sync-worker/ERROR] [Moonlight/]: Fabric API detected!",
]:
    check(f"not fatal: {phrase[:46]}", not _FAILED.search(phrase))

# --- the client-only finding ---------------------------------------------
check("no client-only finding on a server that booted",
      len(_scan_missing_client_class(OK)) == 0,
      f"{len(_scan_missing_client_class(OK))} raised")
check("no client-only finding on a failure that was about something else",
      len(_scan_missing_client_class(BAD)) == 0)

fatal_client = (
    '[main/ERROR] [net.neoforged.fml/]: Caused by: '
    'java.lang.NoClassDefFoundError: net/minecraft/client/gui/screens/Screen\n'
    '[main/ERROR]: Failed to start the minecraft server\n'
)
found = _scan_missing_client_class(fatal_client)
check("a client class that did kill the boot is still reported",
      len(found) == 1, found[0]["title"] if found else "MISSED")

# The same stack, but the server went on to finish: survived, not fatal.
survived = fatal_client.replace(
    "[main/ERROR]: Failed to start the minecraft server",
    '[main/INFO]: Done (25.641s)! For help, type "help"')
check("the same stack is ignored when the boot finished after it",
      len(_scan_missing_client_class(survived)) == 0)

# Ordering matters: a failed boot appended after a good one is still a failure.
restarted = survived + fatal_client
check("a later failed boot is not excused by an earlier good one",
      len(_scan_missing_client_class(restarted)) == 1)

passed = sum(out)
print(f"\n{passed}/{len(out)} checks passed")
sys.exit(0 if passed == len(out) else 1)
