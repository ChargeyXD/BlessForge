"""The job worker: installs, provisioning, and the scheduled update sweep.

    arq app.worker.WorkerSettings

A separate process from the API, sharing its database, its Redis and its
/data volume. The API puts a named task on the arq queue (app/jobs.py
`submit`); this runs it, writing every progress frame to the job's Redis
stream as it goes. So the API can be restarted -- redeployed, OOM-killed,
anything -- in the middle of a forty-minute install, and the install carries
on; the browser's EventSource reconnects and follows it from where it was.

Three rules this keeps:

  * **A task is never retried.** `max_tries=1`. A half-finished install
    re-run from the top creates a second server; a failure is reported and a
    person decides.
  * **Cancellation is a flag, checked every second.** The API sets
    `bf:job:<id>:cancel`; the task running it is cancelled here, and a task
    still in the queue sees the flag and never starts.
  * **One sweep, however many workers.** The update sweep runs in whichever
    worker holds `bf:lock:sweep`, so two workers do not both hit every
    catalogue on the same schedule.

malloc_trim still runs after the heavy jobs (`jobs.run_job`); this is the
process that now does the heavy lifting, so it is where it matters.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from arq.connections import RedisSettings
from arq.worker import func

from app import config, db, jobs, tasks, watcher, whitelist
from app import engine as eng

log = logging.getLogger("blessforge.worker")

OWNER = f"worker:{uuid.uuid4().hex[:10]}"
CANCEL_POLL = 1.0
SWEEP_LOCK = "bf:lock:sweep"
SWEEP_LOCK_TTL = 120


async def startup(ctx) -> None:
    jobs.OWNER = OWNER
    await db.init()
    eng.configure()
    await eng.load_nodes()
    await whitelist.load()
    ctx["heartbeat"] = asyncio.create_task(jobs.heartbeat_forever())
    ctx["nodes"] = asyncio.create_task(_nodes_until_found())
    ctx["sweep"] = asyncio.create_task(_sweep_forever())
    # Scheduled tasks. Runs are claimed in the database, so every worker can
    # run the loop and each task still fires once.
    from app import history, schedules
    ctx["schedules"] = asyncio.create_task(schedules.run_forever())
    # Stats history: one row per server per minute, the unique key keeping
    # it one however many workers record.
    ctx["history"] = asyncio.create_task(history.run_forever())
    log.info("worker %s ready (engine %s, database %s)",
             OWNER, eng.kind(), db.redacted_url())


async def _nodes_until_found() -> None:
    """The panel registers the bundled agent on its first start, and the
    worker may have started first. Until it can see a node, look again."""
    while eng.kind() == "agent" and not eng.drivers():
        await asyncio.sleep(10)
        try:
            await eng.load_nodes()
        except Exception as e:      # noqa: BLE001 -- the database may still be coming up
            log.warning("could not load nodes yet: %s", e)


async def shutdown(ctx) -> None:
    for key in ("sweep", "heartbeat", "nodes", "schedules", "history"):
        task = ctx.get(key)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    await jobs.close_redis()
    await db.dispose()
    from app import curseforge
    curseforge.shutdown_pool()
    from app.engine.agent import close_clients
    await close_clients()


async def _watch_cancel(r, job_id: str, target: asyncio.Task) -> None:
    while not target.done():
        try:
            if await r.get(jobs._cancel_key(job_id)):
                target.cancel()
                return
        except Exception:       # noqa: BLE001 -- Redis blip: check next second
            pass
        await asyncio.sleep(CANCEL_POLL)


async def run_job(ctx, job_id: str, task: str, params: dict, kind: str,
                  title: str, server_id: str | None, server_name: str | None,
                  created: float) -> None:
    """Run one queued task, publishing its progress as it goes."""
    r = await jobs.get_redis()
    job = jobs.Job(kind, title, server_id=server_id, server_name=server_name,
                   job_id=job_id, created=created)
    job.owner = OWNER
    pump = jobs.Pump(job, r)
    job._sinks.append(pump.push)
    try:
        if await r.get(jobs._cancel_key(job_id)):
            job.status, job.error = "cancelled", "cancelled before it started"
            job.finished = time.time()
            job.emit("end")
            return
        spec = tasks.get(task)
        # Decisions made in the panel since this worker started have to
        # count: the client-only review reads them synchronously.
        await whitelist.load()
        runner = asyncio.create_task(
            jobs.run_job(job, lambda j: spec.fn(j, **(params or {}))))
        watch = asyncio.create_task(_watch_cancel(r, job_id, runner))
        try:
            await runner
        except asyncio.CancelledError:
            if not runner.cancelled():
                raise
        finally:
            watch.cancel()
    finally:
        await pump.close()


async def _sweep_forever() -> None:
    """The scheduled update sweep, in exactly one worker."""
    hours = config.UPDATE_CHECK_HOURS
    if hours <= 0:
        return
    r = await jobs.get_redis()
    await asyncio.sleep(300)        # not on boot; see watcher.run_forever
    while True:
        try:
            held = await r.set(SWEEP_LOCK, OWNER, nx=True, ex=SWEEP_LOCK_TTL) \
                or await r.get(SWEEP_LOCK) == OWNER
            if held:
                await r.expire(SWEEP_LOCK, SWEEP_LOCK_TTL)
                last = watcher.load().get("checked_at") or 0
                if time.time() - last >= hours * 3600:
                    await watcher.check_all()
        except asyncio.CancelledError:
            raise
        except Exception as e:      # noqa: BLE001 -- next round tries again
            log.warning("update sweep failed: %s", e)
        await asyncio.sleep(60)


class WorkerSettings:
    functions = [func(run_job, name="run_job", max_tries=1,
                      timeout=config.JOB_TIMEOUT_SECONDS)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(config.REDIS_URL or "redis://localhost:6379")
    max_jobs = config.WORKER_CONCURRENCY
    job_timeout = config.JOB_TIMEOUT_SECONDS
    keep_result = 3600
    job_serializer = staticmethod(jobs.serialize)
    job_deserializer = staticmethod(jobs.deserialize)
    # Health is the heartbeat key, not arq's own check.
    health_check_interval = 60

