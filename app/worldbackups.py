"""World backups, from the panel's side.

The work happens on the node (agent/worldbackup.py: content-addressed
snapshots, a running server saved first). This module is what the API, the
scheduler and the risky actions call: take one and wait for it, list them,
restore, delete, and the per-server settings that shape them.

Not to be confused with app/backups.py, which is undo for mod-set changes
(renames and config edits) and never copies a world.

Settings live in the server's limits under "backups":

  * keep          -- the newest N are kept, older ones pruned (default 8)
  * exclude       -- extra folders left out (logs, caches and other tools'
                     backups are always left out)
  * compress      -- zlib the stored files (worlds are mostly compressed
                     already; it mainly helps configs and datapacks)
  * before_risky  -- take one automatically before a pack version switch and
                     before a restore (default on). If that backup fails, the
                     risky action does not go ahead.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app import db, engine

log = logging.getLogger(__name__)

DEFAULTS = {"keep": 8, "exclude": [], "compress": False, "before_risky": True}
POLL = 2.0


class BackupFailed(RuntimeError):
    pass


def human(n: int | float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def settings_of(server: dict) -> dict:
    stored = ((server.get("limits") or {}).get("backups") or {})
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


async def settings(server_id: str) -> dict:
    return settings_of(await engine.get_server(server_id))


async def save_settings(server_id: str, changes: dict) -> dict:
    current = await settings(server_id)
    if "keep" in changes:
        keep = int(changes["keep"])
        if not 1 <= keep <= 500:
            raise ValueError("keep is between 1 and 500")
        current["keep"] = keep
    if "exclude" in changes:
        items = changes["exclude"]
        if isinstance(items, str):
            items = [x.strip() for x in items.split(",")]
        clean = []
        for item in items or []:
            rel = str(item).strip().strip("/")
            if not rel:
                continue
            if ".." in rel.split("/") or rel.startswith(("/", "\\")) or len(rel) > 200:
                raise ValueError(f"'{item}' is not a folder inside the server")
            clean.append(rel)
        current["exclude"] = clean[:64]
    for key in ("compress", "before_risky"):
        if key in changes:
            current[key] = bool(changes[key])
    await engine.update_server(server_id, {"limits": {"backups": current}})
    return current


async def _wait(job_id: str, job=None, timeout: float = 6 * 3600) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        rec = await engine.world_backup_job(job_id)
        if job is not None and rec.get("progress") and rec["progress"] != last:
            last = rec["progress"]
            job.set_step(f"{last.get('files', 0)} files, {human(last.get('bytes'))}", 50)
        if rec.get("status") == "done":
            return rec
        if rec.get("status") == "error":
            raise BackupFailed(rec.get("error") or "the backup failed")
        await asyncio.sleep(POLL)
    raise BackupFailed("the backup did not finish in time")


async def take(server_id: str, *, reason: str = "", keep: int | None = None,
               job=None) -> dict:
    """Back the world up now and wait for it. Returns the snapshot summary."""
    opts = await settings(server_id)
    if job is not None:
        job.set_step("Backing up the world", 5)
    job_id = await engine.world_backup_start(server_id, {
        "reason": reason or "manual", "keep": keep or opts["keep"],
        "exclude": opts["exclude"], "compress": opts["compress"]})
    rec = await _wait(job_id, job)
    result = dict(rec.get("result") or {})
    result["size_human"] = human(result.get("size"))
    result["new_human"] = human(result.get("new_bytes"))
    for line in rec.get("log") or []:
        if job is not None:
            job.log_line(line)
    if job is not None:
        job.log_line(f"Backup {result.get('id')}: {result.get('files')} files, "
                     f"{result['size_human']} ({result['new_human']} new on disk"
                     + (f", {result.get('reused')} unchanged files not read again"
                        if result.get("reused") else "") + ")")
    try:
        from app.db.models import Backup
        async with db.session() as s:
            s.add(Backup(server_id=server_id, snapshot_id=str(result.get("id") or ""),
                         size=int(result.get("size") or 0), status="done"))
    except Exception as e:      # noqa: BLE001 -- the backup exists on the node regardless
        log.debug("could not record backup %s: %s", result.get("id"), e)
    return result


async def listing(server_id: str) -> dict:
    data = await engine.world_backups(server_id)
    snaps = []
    for s in data.get("snapshots") or []:
        snaps.append({**s, "size_human": human(s.get("size")),
                      "new_human": human(s.get("new_bytes"))})
    return {"snapshots": snaps, "store_bytes": data.get("store_bytes") or 0,
            "store_human": human(data.get("store_bytes")), "running": data.get("running") or [],
            "settings": await settings(server_id)}


async def restore(server_id: str, snapshot_id: str, *, start_after: bool = True,
                  job=None) -> dict:
    opts = await settings(server_id)
    if job is not None:
        job.set_step(f"Restoring {snapshot_id}", 5)
        job.log_line("The server is stopped for the restore"
                     + (", and backed up as it is now first" if opts["before_risky"] else ""))
    job_id = await engine.world_backup_restore(server_id, snapshot_id, {
        "start_after": bool(start_after), "safety": bool(opts["before_risky"])})
    rec = await _wait(job_id, job)
    result = dict(rec.get("result") or {})
    if rec.get("safety_backup"):
        result["safety_backup"] = rec["safety_backup"]
        if job is not None:
            job.log_line(f"Saved the world as it was first: backup {rec['safety_backup']}")
    if job is not None:
        job.log_line(f"Restored {result.get('files')} files from {snapshot_id}")
    return result


async def delete(server_id: str, snapshot_id: str) -> None:
    await engine.world_backup_delete(server_id, snapshot_id)


async def before_risky(server_id: str, what: str, job=None) -> dict | None:
    """The automatic backup before something hard to undo. None when the
    server has it switched off. Raises if it fails: the risky action must
    not go ahead without the safety net it was promised."""
    opts = await settings(server_id)
    if not opts["before_risky"]:
        return None
    if job is not None:
        job.log_line(f"Backing up the world before {what}")
    try:
        return await take(server_id, reason=f"Before {what}", job=job)
    except Exception as e:
        raise BackupFailed(f"the backup before {what} failed ({e}), so nothing was "
                           "changed. Turn 'back up before risky changes' off in the "
                           "Backups tab to go ahead without one.") from e


async def stream(server_id: str, snapshot_id: str):
    async for chunk in engine.world_backup_stream(server_id, snapshot_id):
        yield chunk


def summary(data: dict[str, Any]) -> str:
    return f"{data.get('id')} ({human(data.get('size'))})"
