"""Jobs: long operations that report progress and outlive the request.

Installs take minutes and push hundreds of megabytes around, so the HTTP
request that starts one returns immediately with a job id; the UI then
follows /api/jobs/{id}/events for live progress.

Where a job runs
----------------
  * `registry.submit(job, "task_name", **params)` runs a NAMED task from
    app/tasks.py. With REDIS_URL set it goes on the arq queue and the worker
    process (app/worker.py) runs it -- so restarting the API mid-install does
    not stop the install. Without Redis (a dev run, the tests) the same task
    runs in this process. A task is named, with JSON parameters, because a
    closure cannot cross a process boundary.
  * `registry.start(job, coroutine_factory)` runs a closure here, in the API
    process. Kept for the short, read-only jobs (the AI analyses, dealing a
    roulette hand) whose bodies are bound up with the route that starts
    them. If this process stops while one runs, it is reported as
    interrupted rather than left "running" forever.

How progress travels
--------------------
In-process subscribers get every frame directly. With Redis, every frame is
also appended to a stream per job (`bf:job:<id>:events`, capped) and a
snapshot of the job is kept beside it, stamped with the id of the last
event it reflects. A follower -- any API process, including one started
after the job was -- reads the snapshot, then the stream from exactly that
id: no gap, no duplicate, and a reconnect after a restart picks up where it
left off.

Who is alive
------------
Each process that runs jobs keeps a heartbeat key (`bf:owner:<id>`, 30 s).
A job whose owner's heartbeat has gone, and that has not been updated since,
is reported as interrupted the next time anybody reads it.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import gc
import json
import logging
import os
import time
import traceback
import uuid
from typing import Any, AsyncIterator, Callable, Coroutine

from app import config

log = logging.getLogger(__name__)


# Returning freed memory to the operating system.
#
# An install allocates in large blocks -- jars, archives, upload buffers --
# and glibc serves those from arenas it does not hand back on free(). CPython
# frees the objects promptly and the process still shows the peak: on this
# machine a 301-mod preflight settled at 270 MB after peaking at 429 MB, and
# stayed there. `malloc_trim` releases the unused top of each arena, which is
# the difference between "used 400 MB once" and "holds 400 MB forever".
#
# Best-effort by design: musl has no malloc_trim, and a platform without it
# should lose the optimisation, not the app.
def _load_malloc_trim():
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6",
                           use_errno=True)
        trim = libc.malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        return trim
    except (OSError, AttributeError):
        return None


_MALLOC_TRIM = _load_malloc_trim()

# Only worth doing after jobs that actually move bulk. Trimming after every
# small job would be pointless syscall traffic.
HEAVY_JOB_KINDS = {"install", "switch", "preflight", "deep_scan", "add_mod",
                   "identify", "provision", "roulette", "export", "client_scan"}


def release_memory() -> None:
    """Collect, then hand the arenas back."""
    gc.collect()
    if _MALLOC_TRIM is not None:
        try:
            _MALLOC_TRIM(0)
        except Exception:
            pass


MAX_LOG_LINES = 500
KEEP_FINISHED_SECONDS = 60 * 60
# Coalescing thresholds for streamed model output.
STREAM_FLUSH_CHARS = 48
STREAM_FLUSH_SECONDS = 0.25
MAX_STREAM_CHARS = 40000

TERMINAL = ("done", "error", "cancelled")
# Redis: how long a job's record outlives it, and how much history it keeps.
FINISHED_TTL = 24 * 3600
RUNNING_TTL = 7 * 24 * 3600
EVENTS_MAXLEN = 2000
# A process that has not beaten for HEARTBEAT_TTL seconds is gone. Tests
# shorten it (BF_JOB_HEARTBEAT_TTL) so a dead worker is noticed in seconds.
HEARTBEAT_TTL = max(3, int(os.environ.get("BF_JOB_HEARTBEAT_TTL", "") or 30))
HEARTBEAT_EVERY = max(1, HEARTBEAT_TTL // 3)
INDEX_KEY = "bf:jobs"

# Who runs jobs in this process. The worker replaces it with its own.
OWNER = f"api:{uuid.uuid4().hex[:10]}"


def _job_key(job_id: str) -> str:
    return f"bf:job:{job_id}"


def _events_key(job_id: str) -> str:
    return f"bf:job:{job_id}:events"


def _cancel_key(job_id: str) -> str:
    return f"bf:job:{job_id}:cancel"


def _owner_key(owner: str) -> str:
    return f"bf:owner:{owner}"


class Job:
    def __init__(self, kind: str, title: str, *, server_id: str | None = None,
                 server_name: str | None = None, job_id: str | None = None,
                 created: float | None = None):
        self.id = job_id or uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        # Which instance this job acts on. Activity lists every job the tab
        # has ever seen, and "Deep dependency scan" on its own says nothing
        # about *which* of a dozen servers is being scanned.
        self.server_id = server_id
        self.server_name = server_name
        self.status = "pending"  # pending | running | done | error | cancelled
        self.step = ""
        self.percent = 0.0
        self.log: list[dict] = []
        self.result: Any = None
        self.error: str | None = None
        self.created = created or time.time()
        self.finished: float | None = None
        self.owner: str | None = None
        # Incremental output (model tokens), plus the pending coalescing buffer.
        self.stream: str = ""
        self._stream_buffer: str = ""
        self._stream_flushed: float = 0.0
        self._subscribers: list[asyncio.Queue] = []
        self._sinks: list[Callable[[dict], None]] = []
        self._task: asyncio.Task | None = None

    # --- progress reporting (called from the task) ---------------------

    def set_step(self, step: str, percent: float | None = None) -> None:
        self.step = step
        if percent is not None:
            self.percent = max(0.0, min(100.0, percent))
        self.emit("step", step)

    def set_instance(self, server_id: str | None, server_name: str | None = None
                     ) -> None:
        """Name the instance this job works on, once it is known.

        An install does not have a server id until the node creates one, so
        this is called mid-flight as well as at creation.
        """
        if server_id:
            self.server_id = server_id
        if server_name:
            self.server_name = server_name
        self.emit("instance", server_name or server_id or "")

    def log_line(self, message: str, level: str = "info") -> None:
        entry = {"t": time.time(), "level": level, "message": message}
        self.log.append(entry)
        if len(self.log) > MAX_LOG_LINES:
            del self.log[: len(self.log) - MAX_LOG_LINES]
        self.emit("log", message, level=level)

    def stream_chunk(self, text: str, *, flush: bool = False) -> None:
        """Feed incremental output (e.g. model tokens) to watchers.

        Emitting one SSE frame per token would swamp both the queue and the
        browser, so chunks are coalesced and flushed on a short interval or
        when the buffer grows past a threshold.
        """
        if text:
            self.stream += text
            self._stream_buffer += text
        now = time.time()
        big_enough = len(self._stream_buffer) >= STREAM_FLUSH_CHARS
        due = (now - self._stream_flushed) >= STREAM_FLUSH_SECONDS
        if self._stream_buffer and (flush or big_enough or due):
            self.emit("stream", self._stream_buffer,
                      total=len(self.stream))
            self._stream_buffer = ""
            self._stream_flushed = now

    def emit(self, event: str, message: str = "", **extra) -> None:
        payload = {
            "event": event,
            "job_id": self.id,
            "status": self.status,
            "step": self.step,
            "percent": round(self.percent, 1),
            "message": message,
            "server_id": self.server_id,
            "server_name": self.server_name,
            **extra,
        }
        # ANY frame that says the job is over has to carry the result.
        #
        # A client closes its EventSource on the first frame reporting a
        # terminal status, and there is more than one such frame: the runner
        # sets `status = "done"` and then calls set_step("Finished"), so a
        # *step* event carrying status=done is emitted before the "end" event
        # is. Attaching the result only to "end" left that first frame
        # resultless, which is what made every job whose result drives the
        # next screen -- the client-only review, an AI plan, a deep scan --
        # hand the UI `undefined` and silently do nothing.
        #
        # Keyed on status rather than on the event name so that no future
        # emit between "the job finished" and "end" can reintroduce this.
        if event == "end" or self.status in TERMINAL:
            payload.setdefault("result", self.result)
            payload.setdefault("error", self.error)
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass
        for sink in list(self._sinks):
            try:
                sink(payload)
            except Exception:       # noqa: BLE001 -- a sink never breaks a job
                pass

    # --- subscription (called from the SSE endpoint) -------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "server_id": self.server_id,
            "server_name": self.server_name,
            "status": self.status,
            "step": self.step,
            "percent": round(self.percent, 1),
            "error": self.error,
            "result": self.result,
            "created": self.created,
            "finished": self.finished,
            "owner": self.owner,
            "log": self.log[-100:],
            # Trimmed so a reconnecting client gets the tail, not megabytes.
            "stream": self.stream[-MAX_STREAM_CHARS:],
            "elapsed": round((self.finished or time.time()) - self.created, 1),
        }

    def cancel(self) -> bool:
        if self._task and not self._task.done():
            self._task.cancel()
            return True
        return False


async def run_job(job: Job, coro_factory: Callable[[Job], Coroutine]) -> None:
    """Run a job's body and record how it ended. Shared by every runner."""
    job.status = "running"
    job.emit("start")
    try:
        job.result = await coro_factory(job)
        job.status = "done"
        job.percent = 100.0
        job.set_step("Finished", 100.0)
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.error = "cancelled"
        job.log_line("Job cancelled", "warn")
        raise
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.log_line(f"{type(e).__name__}: {e}", "error")
        job.log_line(traceback.format_exc()[-1500:], "error")
    finally:
        job.finished = time.time()
        job.emit("end")
        if job.kind in HEAVY_JOB_KINDS:
            release_memory()


# --- Redis --------------------------------------------------------------------

_redis = None


async def get_redis():
    """The shared Redis client, or None when REDIS_URL is not set."""
    global _redis
    if not config.REDIS_URL:
        return None
    if _redis is None:
        from redis.asyncio import Redis
        _redis = Redis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:       # noqa: BLE001
            pass
    _redis = None


def _dump(obj: Any) -> str:
    # A result is whatever a task returned; anything JSON cannot carry is
    # turned into text rather than losing the whole frame.
    return json.dumps(obj, default=str)


class Pump:
    """Moves one job's frames into Redis without making `emit` async.

    `emit` is called from deep inside task code, synchronously, many times a
    second. It only puts the frame on a queue; this drains the queue in
    batches -- the frames onto the job's stream, then the snapshot, stamped
    with the id of the last frame it reflects.
    """

    def __init__(self, job: Job, redis):
        self.job = job
        self.redis = redis
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task = asyncio.create_task(self._run())

    def push(self, payload: dict) -> None:
        self.queue.put_nowait(payload)

    async def _run(self) -> None:
        last_id = None
        while True:
            batch = [await self.queue.get()]
            while not self.queue.empty() and len(batch) < 200:
                batch.append(self.queue.get_nowait())
            final = any(p is None for p in batch)
            frames = [p for p in batch if p is not None]
            try:
                if frames:
                    pipe = self.redis.pipeline(transaction=False)
                    for p in frames:
                        pipe.xadd(_events_key(self.job.id), {"d": _dump(p)},
                                  maxlen=EVENTS_MAXLEN, approximate=True)
                    ids = await pipe.execute()
                    last_id = ids[-1]
                await self.write_snapshot(last_id)
            except Exception as e:      # noqa: BLE001 -- the job carries on
                log.warning("could not publish job %s progress: %s", self.job.id, e)
            if final:
                return

    async def write_snapshot(self, last_id: str | None) -> None:
        snap = self.job.snapshot()
        snap["updated"] = time.time()
        if last_id:
            snap["last_event_id"] = last_id
        else:
            old = await self.redis.get(_job_key(self.job.id))
            snap["last_event_id"] = (json.loads(old).get("last_event_id")
                                     if old else "0-0")
        done = self.job.status in TERMINAL
        ttl = FINISHED_TTL if done else RUNNING_TTL
        pipe = self.redis.pipeline(transaction=False)
        pipe.set(_job_key(self.job.id), _dump(snap), ex=ttl)
        pipe.zadd(INDEX_KEY, {self.job.id: self.job.created})
        if done:
            pipe.expire(_events_key(self.job.id), FINISHED_TTL)
        await pipe.execute()

    async def close(self) -> None:
        self.queue.put_nowait(None)
        try:
            await asyncio.wait_for(self.task, timeout=30)
        except Exception:       # noqa: BLE001
            self.task.cancel()


async def heartbeat_forever() -> None:
    """Say this process is alive, so its running jobs are not declared dead."""
    r = await get_redis()
    if r is None:
        return
    try:
        while True:
            try:
                await r.set(_owner_key(OWNER), str(time.time()), ex=HEARTBEAT_TTL)
            except Exception as e:      # noqa: BLE001 -- retried next beat
                log.warning("heartbeat failed: %s", e)
            await asyncio.sleep(HEARTBEAT_EVERY)
    finally:
        try:
            await r.delete(_owner_key(OWNER))
        except Exception:       # noqa: BLE001
            pass


async def queue_health() -> dict:
    """For /api/health: is there a queue, and is anything working it."""
    if not config.REDIS_URL:
        return {"ok": True, "mode": "in-process",
                "note": "no REDIS_URL: jobs run inside the API process"}
    try:
        r = await get_redis()
        await r.ping()
        workers = [k async for k in r.scan_iter(match="bf:owner:worker:*", count=100)]
        queued = await r.zcard("arq:queue")
        out = {"ok": bool(workers), "mode": "worker", "workers": len(workers),
               "queued": int(queued or 0)}
        if not workers:
            out["error"] = ("no worker is running, so installs and other "
                            "queued jobs will wait -- start the worker service")
        return out
    except Exception as e:      # noqa: BLE001 -- reported, not raised
        return {"ok": False, "mode": "worker", "error": f"Redis: {e}"}


async def _reconcile(r, snap: dict) -> dict:
    """A running job whose owner has stopped is not running. Say so, once."""
    if snap.get("status") in TERMINAL or snap.get("status") == "pending":
        return snap
    owner = snap.get("owner")
    stale = time.time() - float(snap.get("updated") or 0) > HEARTBEAT_TTL
    if not owner or not stale or await r.exists(_owner_key(owner)):
        return snap
    snap.update(status="error", finished=time.time(),
                error=("interrupted: the process running this job stopped "
                       "(restarted or crashed) before it finished"))
    end = {"event": "end", "job_id": snap["id"], "status": "error",
           "step": snap.get("step"), "percent": snap.get("percent"),
           "message": snap["error"], "server_id": snap.get("server_id"),
           "server_name": snap.get("server_name"), "result": None,
           "error": snap["error"]}
    last = await r.xadd(_events_key(snap["id"]), {"d": _dump(end)},
                        maxlen=EVENTS_MAXLEN, approximate=True)
    snap["last_event_id"] = last
    await r.set(_job_key(snap["id"]), _dump(snap), ex=FINISHED_TTL)
    return snap


# --- the registry --------------------------------------------------------------

_arq = None


async def _arq_pool():
    global _arq
    if _arq is None:
        from arq import create_pool
        from arq.connections import RedisSettings
        _arq = await create_pool(RedisSettings.from_dsn(config.REDIS_URL),
                                 job_serializer=serialize,
                                 job_deserializer=deserialize)
    return _arq


def serialize(obj: Any) -> bytes:
    """arq's payloads as JSON, not pickle: Redis must never be able to hand
    the worker code to run."""
    return json.dumps(obj, default=str).encode()


def deserialize(raw: bytes) -> Any:
    return json.loads(raw)


class JobRegistry:
    def __init__(self):
        self._jobs: dict[str, Job] = {}

    def create(self, kind: str, title: str, *, server_id: str | None = None,
               server_name: str | None = None) -> Job:
        self._prune()
        return Job(kind, title, server_id=server_id, server_name=server_name)

    def get(self, job_id: str) -> Job | None:
        """A job running in THIS process, or None."""
        return self._jobs.get(job_id)

    def start(self, job: Job, coro_factory: Callable[[Job], Coroutine]) -> Job:
        """Run a closure as a job, here, in this process."""
        self._jobs[job.id] = job
        job.owner = OWNER

        async def go():
            r = await get_redis()
            pump = None
            if r is not None:
                pump = Pump(job, r)
                job._sinks.append(pump.push)
                await pump.write_snapshot(None)
            try:
                await run_job(job, coro_factory)
            finally:
                if pump is not None:
                    await pump.close()

        job._task = asyncio.create_task(go())
        return job

    async def submit(self, job: Job, task: str, **params: Any) -> Job:
        """Run a NAMED task: on the worker when there is one, else here."""
        from app import tasks
        spec = tasks.get(task)
        json.dumps(params)              # a TypeError here beats one in the worker
        r = await get_redis()
        if r is None or not tasks.may_queue(spec):
            return self.start(job, lambda j: spec.fn(j, **params))
        snap = job.snapshot()
        snap.update(status="pending", owner=None, task=task, updated=time.time(),
                    last_event_id="0-0", step="Waiting for a worker")
        await r.set(_job_key(job.id), _dump(snap), ex=RUNNING_TTL)
        await r.zadd(INDEX_KEY, {job.id: job.created})
        pool = await _arq_pool()
        await pool.enqueue_job(
            "run_job", job.id, task, params, job.kind, job.title,
            job.server_id, job.server_name, job.created, _job_id=job.id)
        return job

    async def snapshot(self, job_id: str) -> dict | None:
        local = self._jobs.get(job_id)
        if local is not None:
            return local.snapshot()
        r = await get_redis()
        if r is None:
            return None
        raw = await r.get(_job_key(job_id))
        if not raw:
            return None
        return await _reconcile(r, json.loads(raw))

    async def list(self) -> list[dict]:
        out = {j.id: j.snapshot() for j in self._jobs.values()}
        r = await get_redis()
        if r is not None:
            ids = [i for i in await r.zrevrange(INDEX_KEY, 0, 199) if i not in out]
            if ids:
                raws = await r.mget([_job_key(i) for i in ids])
                gone = []
                for i, raw in zip(ids, raws):
                    if raw:
                        out[i] = await _reconcile(r, json.loads(raw))
                    else:
                        gone.append(i)
                if gone:
                    await r.zrem(INDEX_KEY, *gone)
        return sorted(out.values(), key=lambda s: s.get("created") or 0, reverse=True)

    async def cancel(self, job_id: str) -> bool:
        local = self._jobs.get(job_id)
        if local is not None:
            return local.cancel()
        snap = await self.snapshot(job_id)
        if not snap or snap.get("status") in TERMINAL:
            return False
        r = await get_redis()
        # A flag the worker checks every second; a job still in the queue
        # sees it before it starts and never runs.
        await r.set(_cancel_key(job_id), "1", ex=3600)
        return True

    async def follow(self, job_id: str) -> AsyncIterator[dict | None]:
        """Every frame of a job, from a snapshot onwards, ending with `end`.

        Yields None as a keepalive when nothing has happened for a while.
        """
        local = self._jobs.get(job_id)
        if local is not None:
            q = local.subscribe()
            try:
                yield {"event": "snapshot", **local.snapshot()}
                while True:
                    if local.status in TERMINAL and q.empty():
                        yield {"event": "end", **local.snapshot()}
                        return
                    try:
                        yield await asyncio.wait_for(q.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield None
            finally:
                local.unsubscribe(q)

        r = await get_redis()
        snap = await self.snapshot(job_id)
        if r is None or snap is None:
            return
        yield {"event": "snapshot", **snap}
        if snap.get("status") in TERMINAL:
            yield {"event": "end", **snap}
            return
        last = snap.get("last_event_id") or "0-0"
        while True:
            resp = await r.xread({_events_key(job_id): last}, block=15000, count=200)
            if not resp:
                # Quiet: check the owner is still there, then keep the
                # connection alive.
                again = await self.snapshot(job_id)
                if again is None or again.get("status") in TERMINAL:
                    if again is not None:
                        yield {"event": "end", **again}
                    return
                yield None
                continue
            for entry_id, fields in resp[0][1]:
                last = entry_id
                payload = json.loads(fields["d"])
                yield payload
                if payload.get("event") == "end":
                    return

    def _prune(self) -> None:
        cutoff = time.time() - KEEP_FINISHED_SECONDS
        for jid, job in list(self._jobs.items()):
            if job.finished and job.finished < cutoff:
                del self._jobs[jid]


registry = JobRegistry()
