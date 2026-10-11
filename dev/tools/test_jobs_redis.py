#!/usr/bin/env python3
"""The job queue: a job outlives the API, and the browser re-attaches.

WP-04's promise, tested with real processes: an API, a worker, and a Redis
between them. The job is a self-test task (registered only under
BF_SELFTEST_TASKS=1) that counts slowly and reports every step, standing in
for an install -- what is under test is the queue, not a modpack.

  * a job submitted to the queue runs in the WORKER, not the API;
  * the API can be killed mid-job and the job keeps going;
  * a new API process re-attaches over /api/jobs/{id}/events and the browser
    gets every remaining frame, with no gap and no repeat;
  * cancel reaches a running job through Redis; a failing task is reported;
  * a worker that dies mid-job leaves the job reported as interrupted, not
    "running" forever; a job queued while no worker is up waits, then runs.

The first half runs in this process against the same Redis, checking the
stream itself: snapshot-then-events with no gap, and the reconcile of a
dead owner.

There is no Redis on the box this was written on, so the Redis is fakeredis
speaking the real protocol over TCP (the worker is started through
dev/tools/run_test_worker.py, which skips arq's one INFO log line fakeredis
does not answer). Everything else -- arq, redis-py, uvicorn, the app -- is
the real thing. Point REDIS_TEST_URL at a real Redis to use that instead.
"""
import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TMP = pathlib.Path(tempfile.mkdtemp(prefix="bf-jobs-"))
REAL = os.environ.get("REDIS_TEST_URL", "").strip()
_server = None
if REAL:
    REDIS_URL = REAL
else:
    from fakeredis import TcpFakeServer
    rport = free_port()
    _server = TcpFakeServer(("127.0.0.1", rport), server_type="redis")
    # One thread per connection, non-daemon by default: a connection still
    # open at the end (a killed subprocess's, say) would keep this process
    # from ever exiting -- after every check had passed.
    _server.daemon_threads = True
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    REDIS_URL = f"redis://127.0.0.1:{rport}/0"

ENV = {
    **os.environ,
    "REDIS_URL": REDIS_URL,
    "BF_SELFTEST_TASKS": "1",
    "BF_JOB_HEARTBEAT_TTL": "4",
    "DATA_DIR": str(TMP), "STATE_DIR": str(TMP / "state"),
    "CACHE_DIR": str(TMP / "cache"),
    "DATABASE_URL": f"sqlite+aiosqlite:///{(TMP / 'bf.db').as_posix()}",
    "ENGINE": "mock", "CURSEFORGE_API_KEY": "", "CURSEFORGE_API_KEY_B64": "",
    "OLLAMA_URL": "", "MODRINTH_ENABLED": "false", "UPDATE_CHECK_HOURS": "0",
    "PYTHONUNBUFFERED": "1",
}
os.environ.update({k: v for k, v in ENV.items() if k not in os.environ or k in (
    "REDIS_URL", "BF_SELFTEST_TASKS", "BF_JOB_HEARTBEAT_TTL", "DATA_DIR",
    "STATE_DIR", "CACHE_DIR", "DATABASE_URL", "ENGINE", "UPDATE_CHECK_HOURS")})

import httpx  # noqa: E402

from app import jobs, tasks  # noqa: E402

results = []


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""),
          flush=True)


# --- processes ------------------------------------------------------------------

LOG = open(TMP / "procs.log", "ab")


def start_worker():
    return subprocess.Popen([sys.executable, str(ROOT / "dev/tools/run_test_worker.py")],
                            cwd=ROOT, env=ENV, stdout=LOG, stderr=subprocess.STDOUT)


def start_api(port):
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=ENV, stdout=LOG, stderr=subprocess.STDOUT)


def stop(proc, hard=False):
    if proc and proc.poll() is None:
        proc.kill() if hard else proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def wait_up(base, seconds=40):
    deadline = time.time() + seconds
    async with httpx.AsyncClient() as c:
        while time.time() < deadline:
            try:
                if (await c.get(base + "/api/healthz", timeout=2)).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.3)
    return False


async def wait_for(pred, seconds=20, every=0.25):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = await pred()
        if value:
            return value
        await asyncio.sleep(every)
    return None


async def sign_in(base) -> dict:
    async with httpx.AsyncClient(base_url=base) as c:
        r = await c.post("/api/auth/login", json={"username": "admin", "password": "password"})
        cookie = r.cookies
        await c.post("/api/auth/password", cookies=cookie,
                     json={"current": "password", "password": "a long test passphrase"})
        r = await c.post("/api/auth/login", json={"username": "admin",
                                                  "password": "a long test passphrase"})
        return dict(r.cookies)


async def follow_http(base, cookies, job_id, stop_after=None):
    """Read the SSE stream like a browser. Returns (frames, closed_cleanly)."""
    frames = []
    try:
        async with httpx.AsyncClient(base_url=base, cookies=cookies, timeout=60) as c:
            async with c.stream("GET", f"/api/jobs/{job_id}/events") as r:
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    frame = json.loads(line[6:])
                    frames.append(frame)
                    if frame.get("event") == "end":
                        return frames, True
                    if stop_after and stop_after(frames):
                        return frames, False
    except (httpx.HTTPError, httpx.StreamError):
        return frames, False
    return frames, False


def ticks(frames):
    out = []
    for f in frames:
        if f.get("event") == "snapshot":
            out += [e["message"] for e in f.get("log", []) if e["message"].startswith("tick")]
        elif f.get("event") == "log" and str(f.get("message", "")).startswith("tick"):
            out.append(f["message"])
    return out


# --- the tests ------------------------------------------------------------------


async def in_process():
    """Part A: the stream itself, in this process, against the same Redis."""
    r = await jobs.get_redis()
    await r.flushdb()

    # 1. a job run here, followed by a registry that has never seen it --
    #    which is what any other API process is.
    job = jobs.registry.create("selftest", "Counting here")

    async def body(j):
        for i in range(1, 9):
            j.set_step(f"step {i}", i * 12)
            j.log_line(f"tick {i}")
            await asyncio.sleep(0.05)
        return {"answer": 42}

    other = jobs.JobRegistry()
    jobs.registry.start(job, body)
    await asyncio.sleep(0.12)              # a few frames already out
    frames = [f async for f in other.follow(job.id) if f is not None]
    check("another process can follow a job it never started",
          frames[0]["event"] == "snapshot" and frames[-1]["event"] == "end")
    check("...and the end carries the result", frames[-1].get("result") == {"answer": 42},
          frames[-1].get("result"))
    check("...with every log line exactly once, in order",
          ticks(frames) == [f"tick {i}" for i in range(1, 9)], ticks(frames))

    # 2. reconnecting mid-job: the snapshot and the stream meet exactly.
    job = jobs.registry.create("selftest", "Counting again")

    async def slow(j):
        for i in range(1, 13):
            j.log_line(f"tick {i}")
            await asyncio.sleep(0.06)
        return "done"

    jobs.registry.start(job, slow)
    first = []
    async for f in jobs.JobRegistry().follow(job.id):
        if f is not None:
            first.append(f)
        if len(ticks(first)) >= 4:
            break                          # the browser tab goes away
    await asyncio.sleep(0.2)               # the job carries on meanwhile
    second = [f async for f in jobs.JobRegistry().follow(job.id) if f is not None]
    check("a reconnect gets every remaining frame, with no gap and no repeat",
          ticks(second) == [f"tick {i}" for i in range(1, 13)], ticks(second))

    # 3. a job whose owner has gone is not "running" forever.
    ghost = {"id": "ghost1234567", "kind": "install", "title": "Ghost", "status": "running",
             "step": "Uploading", "percent": 40, "owner": "worker:gone", "log": [],
             "created": time.time() - 100, "updated": time.time() - 60,
             "last_event_id": "0-0", "server_id": None, "server_name": None}
    await r.set(jobs._job_key(ghost["id"]), json.dumps(ghost))
    await r.zadd(jobs.INDEX_KEY, {ghost["id"]: ghost["created"]})
    seen = await jobs.JobRegistry().snapshot(ghost["id"])
    check("a running job whose worker stopped is reported as interrupted",
          seen["status"] == "error" and "interrupted" in seen["error"], seen.get("error"))
    tail = [f async for f in jobs.JobRegistry().follow(ghost["id"]) if f is not None]
    check("...and following it ends instead of hanging", tail[-1]["event"] == "end")

    # 4. what can be submitted.
    try:
        await jobs.registry.submit(jobs.registry.create("x", "x"), "no_such_task")
        unknown = False
    except ValueError:
        unknown = True
    check("an unknown task name is refused at submit, not in the worker", unknown)
    try:
        await jobs.registry.submit(jobs.registry.create("x", "x"), "selftest_count",
                                   steps=object())
        bad = False
    except TypeError:
        bad = True
    check("parameters that are not JSON are refused at submit", bad)
    local = jobs.registry.create("deep_scan", "Scan")
    await jobs.registry.submit(local, "deep_scan", server_id="nope")
    check("with the in-memory engine, a task that touches servers stays in this process",
          jobs.registry.get(local.id) is not None
          and not tasks.may_queue(tasks.get("deep_scan")))
    await asyncio.sleep(0.2)


async def across_processes():
    """Part B: an API, a worker, and the API restarted mid-job."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    worker = start_worker()
    api = start_api(port)
    try:
        check("the API comes up with a queue configured", await wait_up(base))
        cookies = await sign_in(base)

        seen_health = {}

        async def health():
            async with httpx.AsyncClient(base_url=base, cookies=cookies) as c:
                body = (await c.get("/api/health?fresh=true")).json()
            seen_health["last"] = body.get("checks", {}).get("queue", body)
            q = seen_health["last"]
            return q if isinstance(q, dict) and q.get("workers") else None
        q = await wait_for(health, seconds=45, every=1)
        check("the health check sees the worker", q and q["mode"] == "worker",
              seen_health.get("last"))

        # The submission happens here, as any API process would make it.
        job = jobs.registry.create("selftest", "Counting on the worker")
        await jobs.registry.submit(job, "selftest_count", steps=16, delay=0.5)
        check("a queued job is not run by the process that submitted it",
              jobs.registry.get(job.id) is None)

        first, _ = await follow_http(base, cookies, job.id,
                                     stop_after=lambda fs: len(ticks(fs)) >= 3)
        check("the browser follows the job over HTTP", len(ticks(first)) >= 3,
              len(ticks(first)))

        stop(api, hard=True)               # the API dies mid-job
        r = await jobs.get_redis()
        before = json.loads(await r.get(jobs._job_key(job.id)))["percent"]
        await asyncio.sleep(2.5)
        after = json.loads(await r.get(jobs._job_key(job.id)))["percent"]
        check("with the API gone, the job keeps going", after > before, f"{before} -> {after}")

        api = start_api(port)              # a new API process
        check("a new API process comes up", await wait_up(base))
        second, clean = await follow_http(base, cookies, job.id)
        snap = second[0] if second else {}
        check("the browser re-attaches to the job from the new API",
              snap.get("event") == "snapshot" and snap.get("status") in ("running", "done"),
              snap.get("status"))
        check("...and follows it to the end", clean and second[-1].get("status") == "done",
              second[-1].get("status") if second else None)
        check("...seeing every tick exactly once across snapshot and stream",
              ticks(second) == [f"tick {i}" for i in range(1, 17)], ticks(second))
        result = second[-1].get("result") or {}
        check("the job ran in the worker, not in either API",
              str(result.get("owner", "")).startswith("worker:")
              and result.get("pid") != os.getpid(), result)

        async with httpx.AsyncClient(base_url=base, cookies=cookies) as c:
            listed = (await c.get("/api/jobs")).json()["items"]
        check("the job list shows worker jobs", any(i["id"] == job.id for i in listed))

        # Cancel, through the API, reaches the worker.
        job = jobs.registry.create("selftest", "To cancel")
        await jobs.registry.submit(job, "selftest_count", steps=60, delay=0.5)
        await wait_for(lambda: _status(job.id, "running"))
        async with httpx.AsyncClient(base_url=base, cookies=cookies) as c:
            said = (await c.post(f"/api/jobs/{job.id}/cancel")).json()
        frames, _ = await follow_http(base, cookies, job.id)
        check("cancel reaches a job running on the worker",
              said.get("cancelled") and frames[-1].get("status") == "cancelled",
              frames[-1].get("status") if frames else said)

        # A failing task is reported as failed, with its reason.
        job = jobs.registry.create("selftest", "To fail")
        await jobs.registry.submit(job, "selftest_fail")
        frames, _ = await follow_http(base, cookies, job.id)
        check("a task that raises is reported as an error, with why",
              frames[-1].get("status") == "error"
              and "fails on purpose" in (frames[-1].get("error") or ""))

        # The worker dies mid-job.
        job = jobs.registry.create("selftest", "Orphaned")
        await jobs.registry.submit(job, "selftest_count", steps=60, delay=0.5)
        await wait_for(lambda: _status(job.id, "running"))
        stop(worker, hard=True)
        gone = await wait_for(lambda: _status(job.id, "error"), seconds=20, every=0.5)
        check("a job whose worker died is reported as interrupted, not running",
              gone and "interrupted" in (gone.get("error") or ""), gone)

        # Queued with no worker up: it waits, then runs when one starts.
        job = jobs.registry.create("selftest", "Patient")
        await jobs.registry.submit(job, "selftest_count", steps=3, delay=0.1)
        await asyncio.sleep(1.5)
        waiting = await jobs.JobRegistry().snapshot(job.id)
        check("with no worker up, a queued job waits rather than failing",
              waiting["status"] == "pending", waiting["status"])
        worker = start_worker()
        done = await wait_for(lambda: _status(job.id, "done"), seconds=30)
        check("...and runs once a worker starts", done is not None)
    finally:
        stop(api)
        stop(worker)


async def _status(job_id, want):
    snap = await jobs.JobRegistry().snapshot(job_id)
    return snap if snap and snap.get("status") == want else None


async def main():
    print(f"(redis: {'real, ' + REDIS_URL if REAL else 'fakeredis over TCP'})")
    await in_process()
    await across_processes()
    await jobs.close_redis()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return passed == len(results)


if __name__ == "__main__":
    ok = asyncio.run(main())
    LOG.close()
    if _server:
        _server.shutdown()
    sys.exit(0 if ok else 1)
