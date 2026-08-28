"""In-memory job registry with SSE-friendly progress streaming.

Installs take minutes and push hundreds of megabytes around, so the HTTP
request that starts one returns immediately with a job id; the UI then
follows /api/jobs/{id}/events for live progress.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import gc
import time
import traceback
import uuid
from typing import Any, Callable, Coroutine

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
                   "identify"}


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


class Job:
    def __init__(self, kind: str, title: str, *, server_id: str | None = None,
                 server_name: str | None = None):
        self.id = uuid.uuid4().hex[:12]
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
        self.created = time.time()
        self.finished: float | None = None
        # Incremental output (model tokens), plus the pending coalescing buffer.
        self.stream: str = ""
        self._stream_buffer: str = ""
        self._stream_flushed: float = 0.0
        self._subscribers: list[asyncio.Queue] = []
        self._task: asyncio.Task | None = None

    # --- progress reporting (called from the worker) -------------------

    def set_step(self, step: str, percent: float | None = None) -> None:
        self.step = step
        if percent is not None:
            self.percent = max(0.0, min(100.0, percent))
        self.emit("step", step)

    def set_instance(self, server_id: str | None, server_name: str | None = None
                     ) -> None:
        """Name the instance this job works on, once it is known.

        An install does not have a server id until Crafty creates one, so
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
        if event == "end" or self.status in ("done", "error", "cancelled"):
            payload.setdefault("result", self.result)
            payload.setdefault("error", self.error)
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
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


class JobRegistry:
    def __init__(self):
        self._jobs: dict[str, Job] = {}

    def create(self, kind: str, title: str, *, server_id: str | None = None,
               server_name: str | None = None) -> Job:
        self._prune()
        job = Job(kind, title, server_id=server_id, server_name=server_name)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        return [j.snapshot() for j in sorted(
            self._jobs.values(), key=lambda x: x.created, reverse=True
        )]

    def start(self, job: Job, coro_factory: Callable[[Job], Coroutine]) -> Job:
        async def runner():
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

        job._task = asyncio.create_task(runner())
        return job

    def _prune(self) -> None:
        cutoff = time.time() - KEEP_FINISHED_SECONDS
        for jid, job in list(self._jobs.items()):
            if job.finished and job.finished < cutoff:
                del self._jobs[jid]


registry = JobRegistry()
