#!/usr/bin/env python3
"""Start the job worker against a test Redis -- for dev/tools/test_jobs_redis.py.

The real worker is `arq app.worker.WorkerSettings`. This runs the same
settings, with one accommodation: arq logs the Redis server's version at
start by sending INFO, which fakeredis (the stand-in Redis the tests use on a
box with no Redis) does not implement. That log line is skipped; nothing
else about the worker changes.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import arq.worker  # noqa: E402


async def _no_info(*_args, **_kwargs):
    return None


arq.worker.log_redis_info = _no_info

from arq.worker import run_worker  # noqa: E402

from app.worker import WorkerSettings  # noqa: E402

if __name__ == "__main__":
    run_worker(WorkerSettings)
