#!/usr/bin/env python3
"""The job stream must deliver a job's RESULT, not just its status.

This is a regression test for the bug that made "Review & strip client-only
mods before installing" appear to do nothing at all: the preflight job ran to
completion, the browser saw it finish, and no review screen ever appeared.

The cause was one missing field. Every job emits an `end` event when its
worker returns, and that payload carried status but no result. The SSE
endpoint separately yields a final frame built from the full snapshot -- but
it is only reached once the queue has drained, so the bare `end` always
arrived first. The browser closes its EventSource on the first frame that
says the job is over, so the frame carrying the result was never read, and
`showReview(undefined)` threw inside a catch that swallowed it.

Nothing about that is visible from the outside, which is why it gets a test:
the job succeeded, the log looked perfect, and the feature silently did
nothing.

Runs offline -- no Crafty, no network.
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app.jobs import registry  # noqa: E402

results = []


def check(name, condition, extra=""):
    results.append(bool(condition))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


async def collect(job, frames, ready):
    """Subscribe exactly as the SSE endpoint does, and record every frame."""
    q = job.subscribe()
    frames.append({"event": "snapshot", **job.snapshot()})
    ready.set()
    try:
        while True:
            if job.status in ("done", "error", "cancelled") and q.empty():
                frames.append({"event": "end", **job.snapshot()})
                return
            try:
                frames.append(await asyncio.wait_for(q.get(), timeout=5))
            except asyncio.TimeoutError:
                return
    finally:
        job.unsubscribe(q)


async def main():
    # --- a job whose result drives the next screen ----------------------
    job = registry.create("preflight", "Analysing pack",
                          server_id="abc-123", server_name="Test Instance")

    async def work(j):
        j.set_step("Inspecting mods", 50)
        j.log_line("found 2 client-only mods")
        return {"review": {"candidates": [{"file_name": "figura.jar"}]},
                "pack": {"name": "Test Pack"}}

    frames, ready = [], asyncio.Event()
    watcher = asyncio.create_task(collect(job, frames, ready))
    await ready.wait()
    registry.start(job, work)
    await watcher

    terminal = [f for f in frames
                if f.get("event") == "end"
                or f.get("status") in ("done", "error", "cancelled")]
    check("the job produced a terminal frame", terminal, f"{len(frames)} frames")

    first = terminal[0]
    check("the FIRST terminal frame carries the result", first.get("result"),
          "this is the one a browser reads before closing its stream")
    check("the result is the worker's return value",
          (first.get("result") or {}).get("pack", {}).get("name") == "Test Pack")
    check("every terminal frame carries a result",
          all(f.get("result") for f in terminal), f"{len(terminal)} terminal frames")

    # --- the instance a job acts on travels with it ---------------------
    check("frames name the instance",
          all(f.get("server_name") == "Test Instance" for f in frames),
          "so Activity can say which server a task ran on")
    check("the snapshot names the instance",
          job.snapshot()["server_name"] == "Test Instance")

    # --- a job learning its instance mid-flight -------------------------
    late = registry.create("install", "Install pack")
    check("a job may start without an instance", late.snapshot()["server_id"] is None)
    late.set_instance("xyz-789", "Freshly Created")
    check("set_instance fills it in later",
          late.snapshot()["server_name"] == "Freshly Created")

    # --- a failing job must still report why -----------------------------
    bad = registry.create("preflight", "Doomed")

    async def boom(j):
        raise RuntimeError("the pack archive is not a readable zip")

    frames2, ready2 = [], asyncio.Event()
    watcher2 = asyncio.create_task(collect(bad, frames2, ready2))
    await ready2.wait()
    registry.start(bad, boom)
    await watcher2

    terminal2 = [f for f in frames2 if f.get("event") == "end"]
    check("a failed job's terminal frame carries the error",
          terminal2 and "readable zip" in (terminal2[0].get("error") or ""),
          terminal2[0].get("error") if terminal2 else "no end frame")

    # --- the frames are JSON-serialisable, as SSE requires ---------------
    try:
        for f in frames + frames2:
            json.dumps(f)
        ok = True
    except TypeError as e:
        ok = False
        print("   ", e)
    check("every frame survives json.dumps", ok)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


sys.exit(asyncio.run(main()))
