"""In-memory job registry with SSE-friendly progress streaming.

Installs take minutes and push hundreds of megabytes around, so the HTTP
request that starts one returns immediately with a job id; the UI then
follows /api/jobs/{id}/events for live progress.
"""
from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from typing import Any, Callable, Coroutine

MAX_LOG_LINES = 500
KEEP_FINISHED_SECONDS = 60 * 60
# Coalescing thresholds for streamed model output.
STREAM_FLUSH_CHARS = 48
STREAM_FLUSH_SECONDS = 0.25
MAX_STREAM_CHARS = 40000


class Job:
    def __init__(self, kind: str, title: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
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
            **extra,
        }
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

    def create(self, kind: str, title: str) -> Job:
        self._prune()
        job = Job(kind, title)
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

        job._task = asyncio.create_task(runner())
        return job

    def _prune(self) -> None:
        cutoff = time.time() - KEEP_FINISHED_SECONDS
        for jid, job in list(self._jobs.items()):
            if job.finished and job.finished < cutoff:
                del self._jobs[jid]


registry = JobRegistry()
