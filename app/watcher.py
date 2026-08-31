"""Looking for mod updates on a schedule, so the answer is already there.

Checking a server used to cost one request per mod and the best part of a
minute, which is why this could not have existed before: nobody would run a
225-request sweep across six servers on a timer. The bulk path made a full
check a few seconds, so a periodic pass is now cheap enough to simply do.

Results are cached to disk and served from there. The check never installs
anything and never touches a running server -- it reads the install manifest
and asks the catalogues what the newest build is.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from app import config, crafty

log = logging.getLogger("blessforge.watcher")

_STATE = "update-checks.json"


def _path():
    return config.DATA_DIR / _STATE


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return {"checked_at": None, "servers": {}}


def _save(state: dict) -> None:
    try:
        _path().write_text(json.dumps(state, indent=2))
    except OSError as e:
        log.warning("could not save update state: %s", e)


async def check_all() -> dict:
    """One pass over every managed server. Returns the new state."""
    from app import mods as modmgr

    state = {"checked_at": time.time(), "servers": {}}
    try:
        servers = await crafty.list_servers()
    except Exception as e:
        log.warning("update sweep: cannot list servers: %s", e)
        return load()

    for server in servers:
        sid = server.get("server_id") or server.get("id")
        if not sid:
            continue
        name = server.get("server_name") or sid
        try:
            result = await modmgr.check_updates(sid)
        except Exception as e:
            # A server BlessForge did not install has no manifest to read.
            # That is not a failure worth recording as one.
            state["servers"][sid] = {"name": name, "error": str(e)[:200],
                                     "updates": 0}
            continue
        state["servers"][sid] = {
            "name": name,
            "checked": result.get("checked", 0),
            "updates": len(result.get("updates") or []),
            "items": [
                {"name": u.get("name"), "file": u.get("file"),
                 "latest_version": u.get("latest_version")}
                for u in (result.get("updates") or [])[:50]
            ],
        }
    _save(state)
    total = sum(s.get("updates", 0) for s in state["servers"].values())
    log.info("update sweep: %d server(s), %d mod update(s) available",
             len(state["servers"]), total)
    return state


async def run_forever() -> None:
    """The background loop. Cancelled on shutdown."""
    hours = config.UPDATE_CHECK_HOURS
    if hours <= 0:
        log.info("scheduled update checks are off (UPDATE_CHECK_HOURS=0)")
        return
    # Not on boot: starting BlessForge should not fire a sweep at Crafty and
    # both catalogues while the app is still coming up.
    await asyncio.sleep(300)
    while True:
        try:
            await check_all()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("update sweep failed: %s", e)
        await asyncio.sleep(hours * 3600)
