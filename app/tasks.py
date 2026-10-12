"""The named tasks a job can run, wherever it runs.

A job that has to survive the API restarting runs in the worker process, and
a closure cannot cross that boundary -- so these are named, take only JSON
parameters, and are looked up by name on the other side. Each wraps one
call the API used to make inline; nothing here has logic of its own.

`engine=True` marks a task that touches servers. The in-memory mock engine
(ENGINE=mock) lives in one process and a worker would see a different,
empty one, so with the mock those tasks always run in the API process. On
the agent engine every process reaches the same nodes, and they queue.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app import config
from app.jobs import Job


@dataclass(frozen=True)
class TaskSpec:
    name: str
    fn: Callable[..., Awaitable[Any]]
    engine: bool = True


TASKS: dict[str, TaskSpec] = {}


def task(name: str, *, engine: bool = True):
    def register(fn):
        TASKS[name] = TaskSpec(name, fn, engine)
        return fn
    return register


def get(name: str) -> TaskSpec:
    try:
        return TASKS[name]
    except KeyError:
        raise ValueError(f"no task called {name!r}")


def may_queue(spec: TaskSpec) -> bool:
    """Whether this task can go to the worker rather than run here."""
    from app import engine as eng
    return not (spec.engine and eng.kind() == "mock")


# --- installs and provisioning -------------------------------------------------


@task("install_modpack")
async def install_modpack(job: Job, **params) -> dict:
    from app import installer
    return await installer.install_modpack(job, **params)


@task("switch_pack_version")
async def switch_pack_version(job: Job, **params) -> dict:
    from app import installer
    return await installer.switch_pack_version(job, **params)


@task("preflight")
async def preflight(job: Job, **params) -> dict:
    from app import installer
    return await installer.preflight(job, **params)


@task("create_server")
async def create_server(job: Job, **params) -> dict:
    from app import provision
    return await provision.create_empty(job, **params)


@task("reinstall_loader")
async def reinstall_loader(job: Job, server_id: str) -> dict:
    from app import provision
    return await provision.reinstall_loader(job, server_id)


@task("install_roll")
async def install_roll(job: Job, **params) -> dict:
    from app import roulette
    return await roulette.install_roll(job, **params)


# --- one server -------------------------------------------------------------


@task("add_mod")
async def add_mod(job: Job, server_id: str, **params) -> dict:
    from app import mods
    return await mods.add_mod_with_dependencies(job, server_id, **params)


@task("install_plugin")
async def install_plugin(job: Job, server_id: str, **params) -> dict:
    from app import plugins
    return await plugins.install(job, server_id, **params)


@task("identify_mods")
async def identify_mods(job: Job, server_id: str, directory: str = "mods") -> dict:
    from app import mods
    return await mods.identify_mods(job, server_id, directory)


@task("deep_scan")
async def deep_scan(job: Job, server_id: str) -> dict:
    from app import diagnostics
    return await diagnostics.deep_scan(job, server_id)


@task("client_scan")
async def client_scan(job: Job, server_id: str, directory: str = "mods") -> dict:
    from app import clientscan
    return await clientscan.scan_instance(job, server_id, directory)


@task("boot_repair")
async def boot_repair(job: Job, server_id: str) -> dict:
    from app import repair
    return await repair.boot_and_repair(job, server_id)


@task("fix_dependency")
async def fix_dependency(job: Job, server_id: str, mod_id: str,
                         required_by: str | None = None) -> dict:
    from app import repair
    return await repair.fix_dependency(job, server_id, mod_id, required_by)


@task("spark_install")
async def spark_install(job: Job, server_id: str) -> dict:
    from app import spark
    return await spark.install(job, server_id)


@task("spark_health")
async def spark_health(job: Job, server_id: str) -> dict:
    from app import spark
    return await spark.health(job, server_id)


@task("spark_profile")
async def spark_profile(job: Job, server_id: str, seconds: int = 60,
                        upload: bool = False) -> dict:
    from app import spark
    return await spark.profile(job, server_id, seconds=seconds, upload=upload)


@task("smoke_test")
async def smoke_test(job: Job, server_id: str, timeout: int = 300) -> dict:
    from app import smoketest
    return await smoketest.run(job, server_id, timeout=timeout)


@task("export_instance")
async def export_instance(job: Job, server_id: str, include_disabled: bool = False,
                          bundle_others: bool = True) -> dict:
    """Write a server out as a CurseForge pack, onto the shared /data volume."""
    from app import exporter
    result = await exporter.export_instance(
        job, server_id, include_disabled=include_disabled,
        bundle_others=bundle_others)
    blob = result.pop("bytes")
    path = config.DATA_DIR / "exports" / result["filename"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    result["download"] = f"/api/exports/{result['filename']}"
    return result


# --- the fleet ------------------------------------------------------------------


@task("check_updates")
async def check_updates(job: Job) -> dict:
    from app import watcher
    return await watcher.check_all()


# --- the queue's own test ------------------------------------------------------
#
# Registered only when BF_SELFTEST_TASKS=1, which nothing in production sets.
# It is how dev/tools/test_jobs_redis.py proves an install survives the API
# restarting: a job that takes long enough to restart the API in the middle
# of, and reports progress the whole way, without needing a pack, a node or
# the network.

if os.environ.get("BF_SELFTEST_TASKS") == "1":
    @task("selftest_count", engine=False)
    async def selftest_count(job: Job, steps: int = 10, delay: float = 0.5) -> dict:
        import asyncio
        for i in range(1, steps + 1):
            job.set_step(f"Counting ({i}/{steps})", 100 * i / steps)
            job.log_line(f"tick {i}")
            await asyncio.sleep(delay)
        from app import jobs
        return {"counted": steps, "pid": os.getpid(), "owner": jobs.OWNER}

    @task("selftest_fail", engine=False)
    async def selftest_fail(job: Job) -> dict:
        job.log_line("about to fail")
        raise RuntimeError("this task fails on purpose")
