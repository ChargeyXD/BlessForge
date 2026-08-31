"""Undo for the changes that cannot otherwise be undone.

Switching a pack version, bulk-disabling a dozen jars or letting the assistant
apply a fix are all one-way today: the previous state is gone the moment the
rename lands. This records enough to put it back.

What it deliberately does NOT do is copy the mods folder. A snapshot of a
225-mod pack would be a gigabyte, taken before every routine toggle, on the
same disk as the worlds -- so the feature would be switched off within a week.
Almost every destructive action here is a *rename*, and the inverse of a
rename is another rename. So a snapshot is:

  * which jars are present and whether each is enabled -- a few KB of names;
  * the install manifest, which is the pack identity;
  * the bytes of any file about to be overwritten, config edits included,
    since those are small and genuinely unrecoverable.

That restores a mod set exactly, costs nothing to take, and is honest about
its limits: a jar deleted from disk is named in the snapshot but cannot be
brought back from it, and the restore says so rather than pretending.
"""
from __future__ import annotations

import json
import logging
import posixpath
import time
import uuid
from typing import Any

from app import config, crafty

log = logging.getLogger("blessforge.backups")

DISABLED_SUFFIX = ".disabled"


def _dir(server_id: str):
    safe = "".join(c for c in server_id if c.isalnum() or c in "-_")[:64]
    path = config.DATA_DIR / "backups" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _base(name: str) -> str:
    return name[: -len(DISABLED_SUFFIX)] if name.endswith(DISABLED_SUFFIX) else name


async def snapshot(server_id: str, reason: str, *,
                   files: dict[str, bytes] | None = None,
                   directory: str = "mods") -> dict:
    """Record the current mod state, plus the bytes of any file being replaced.

    Never raises. A backup that fails must not take the action with it -- the
    user asked to disable a mod, not to find out that the backup store is
    unwritable.
    """
    snap: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "at": time.time(),
        "reason": reason,
        "server_id": server_id,
        "mods": {},
        "manifest": None,
        "files": [],
        "partial": False,
    }
    try:
        entries = await crafty.list_dir(server_id, directory)
        snap["mods"] = {
            name: not name.endswith(DISABLED_SUFFIX)
            for name, meta in entries.items()
            if name != "root_path" and isinstance(meta, dict) and not meta.get("dir")
            and name.lower().endswith((".jar", ".jar" + DISABLED_SUFFIX))
        }
    except Exception as e:
        snap["partial"] = True
        log.warning("snapshot %s: could not list %s: %s", server_id, directory, e)

    try:
        snap["manifest"] = await crafty.read_studio_manifest(server_id)
    except Exception:
        snap["partial"] = True

    root = _dir(server_id)
    if files:
        blobs = root / snap["id"]
        blobs.mkdir(parents=True, exist_ok=True)
        for path, blob in files.items():
            safe = path.replace("/", "__")
            try:
                (blobs / safe).write_bytes(blob)
                snap["files"].append({"path": path, "stored": safe,
                                      "bytes": len(blob)})
            except OSError as e:
                snap["partial"] = True
                log.warning("snapshot %s: could not store %s: %s", server_id, path, e)

    try:
        (root / f"{snap['id']}.json").write_text(json.dumps(snap, indent=2))
    except OSError as e:
        log.warning("snapshot %s: could not write: %s", server_id, e)
        return {**snap, "saved": False}

    _prune(server_id)
    return {**snap, "saved": True}


def list_snapshots(server_id: str) -> list[dict]:
    out = []
    for path in sorted(_dir(server_id).glob("*.json"), reverse=True):
        try:
            snap = json.loads(path.read_text())
        except Exception:
            continue
        out.append({
            "id": snap.get("id"),
            "at": snap.get("at"),
            "reason": snap.get("reason"),
            "mods": len(snap.get("mods") or {}),
            "enabled": sum(1 for v in (snap.get("mods") or {}).values() if v),
            "files": len(snap.get("files") or []),
            "partial": snap.get("partial", False),
            "pack": ((snap.get("manifest") or {}).get("pack") or {}).get("name"),
        })
    return out


def read(server_id: str, snap_id: str) -> dict | None:
    path = _dir(server_id) / f"{snap_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


async def restore(server_id: str, snap_id: str, *,
                  directory: str = "mods") -> dict:
    """Put the mod set back the way the snapshot found it."""
    snap = read(server_id, snap_id)
    if not snap:
        raise ValueError(f"no snapshot '{snap_id}' for this server")

    # Restoring is itself a change to the mod set, so record where we were
    # first. That makes an undo undoable, which is what stops a restore from
    # being the scary button it would otherwise be.
    await snapshot(server_id, f"before restoring '{snap.get('reason')}'")

    want = snap.get("mods") or {}
    try:
        entries = await crafty.list_dir(server_id, directory)
    except crafty.CraftyError as e:
        raise RuntimeError(f"cannot read {directory}: {e}")
    have = {
        name: not name.endswith(DISABLED_SUFFIX)
        for name, meta in entries.items()
        if name != "root_path" and isinstance(meta, dict) and not meta.get("dir")
        and name.lower().endswith((".jar", ".jar" + DISABLED_SUFFIX))
    }

    by_base_have = {_base(n): n for n in have}
    changed: list[str] = []
    missing: list[str] = []
    failed: list[dict] = []

    for name, was_enabled in want.items():
        base = _base(name)
        current = by_base_have.get(base)
        if current is None:
            # In the snapshot but not on disk now: deleted, or renamed by
            # something outside BlessForge. Naming it is the honest outcome --
            # the snapshot holds the name, never the jar.
            missing.append(base)
            continue
        if (not current.endswith(DISABLED_SUFFIX)) == was_enabled:
            continue
        target = base if was_enabled else base + DISABLED_SUFFIX
        try:
            await crafty.rename_path(server_id, f"{directory}/{current}", target)
            changed.append(target)
        except Exception as e:
            failed.append({"file": current, "error": str(e)})

    restored_files = []
    for entry in snap.get("files") or []:
        blob_path = _dir(server_id) / snap_id / entry["stored"]
        if not blob_path.exists():
            continue
        try:
            await crafty.upload_file(
                server_id, posixpath.dirname(entry["path"]) or ".",
                posixpath.basename(entry["path"]), blob_path.read_bytes())
            restored_files.append(entry["path"])
        except Exception as e:
            failed.append({"file": entry["path"], "error": str(e)})

    return {
        "snapshot": snap_id,
        "reason": snap.get("reason"),
        "changed": sorted(changed),
        "files_restored": restored_files,
        "missing": sorted(missing),
        "failed": failed,
    }


def _prune(server_id: str) -> None:
    """Keep the most recent snapshots; the older ones stop being useful."""
    keep = config.MAX_BACKUPS
    if keep <= 0:
        return
    snaps = sorted(_dir(server_id).glob("*.json"), reverse=True)
    import shutil
    for path in snaps[keep:]:
        blobs = path.with_suffix("")
        try:
            path.unlink()
        except OSError:
            continue
        if blobs.is_dir():
            shutil.rmtree(blobs, ignore_errors=True)
