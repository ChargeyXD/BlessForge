"""Locally-imported modpack archives (CurseForge "Export profile" zips).

The CurseForge desktop app can export any profile -- including one the user
assembled by hand -- as a zip. That zip is never published, so it has no
project id and no file id, and the entire install pipeline keys off exactly
those two numbers. This module gives an imported archive a stable local id
and parks it under `DATA_DIR/uploads`, so an import can travel through the
same preflight -> review -> install path as a catalogue pack instead of
needing a second one.

The archive is kept after the install: re-importing an updated export over an
existing instance is the normal way to update a private pack, and the review
step and the install both want to read the same file.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
import zipfile
from pathlib import Path
from typing import AsyncIterator

from app import config, packs


class UploadError(ValueError):
    """Raised for anything wrong with an imported archive."""


def _dir() -> Path:
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return config.UPLOAD_DIR


def archive_path(upload_id: str) -> Path:
    return _dir() / f"{_safe_id(upload_id)}.zip"


def _meta_path(upload_id: str) -> Path:
    return _dir() / f"{_safe_id(upload_id)}.json"


def _safe_id(upload_id: str) -> str:
    """Ids are ours, but they arrive back from the browser -- never trust one."""
    uid = str(upload_id or "")
    if not re.fullmatch(r"[0-9a-f]{8,32}", uid):
        raise UploadError("invalid upload id")
    return uid


def _safe_name(name: str) -> str:
    base = Path(str(name or "modpack.zip")).name
    base = re.sub(r"[^\w.\- ]+", "_", base).strip() or "modpack.zip"
    return base[:120]


# --- storage -----------------------------------------------------------


async def store(filename: str, chunks: AsyncIterator[bytes]) -> dict:
    """Stream an uploaded archive to disk, then analyse it.

    Written chunk by chunk rather than read into memory: a pack export with a
    world folder in it can be several gigabytes, and the container is sized
    for a cache, not for holding one of those.
    """
    upload_id = uuid.uuid4().hex[:16]
    path = archive_path(upload_id)
    digest = hashlib.sha1()
    size = 0
    limit = config.MAX_UPLOAD_MB * 1024 * 1024

    try:
        with path.open("wb") as fh:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    raise UploadError(
                        f"archive is larger than the {config.MAX_UPLOAD_MB} MB "
                        "import limit (raise MAX_UPLOAD_MB to allow it)"
                    )
                digest.update(chunk)
                fh.write(chunk)
    except UploadError:
        path.unlink(missing_ok=True)
        raise
    except OSError as e:
        path.unlink(missing_ok=True)
        raise UploadError(f"could not write the archive to {config.UPLOAD_DIR}: {e}")

    if not size:
        path.unlink(missing_ok=True)
        raise UploadError("the uploaded file is empty")

    try:
        summary = summarise(path)
    except Exception:
        path.unlink(missing_ok=True)
        raise

    record = {
        "upload_id": upload_id,
        "file_name": _safe_name(filename),
        "size": size,
        "sha1": digest.hexdigest(),
        "uploaded_at": time.time(),
        "summary": summary,
    }
    _meta_path(upload_id).write_text(json.dumps(record, indent=2), encoding="utf-8")
    _prune()
    return record


async def store_bytes(data: bytes, filename: str) -> dict:
    """Store an archive this app generated rather than one a user sent.

    Mod Roulette builds a pack in memory and then hands it to the ordinary
    import path, so the rolled pack travels the same road as an uploaded one
    -- same analysis, same review, same installer. Shares `store` rather than
    duplicating it so the size limit, the digest and the pruning stay in one
    place.
    """
    async def one() -> AsyncIterator[bytes]:
        yield data

    return await store(filename, one())


def get(upload_id: str) -> dict:
    meta = _meta_path(upload_id)
    if not meta.exists() or not archive_path(upload_id).exists():
        raise UploadError(
            "that imported archive is no longer on disk -- import it again"
        )
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UploadError(f"the import record is unreadable: {e}")


def list_uploads() -> list[dict]:
    """Newest first. Orphaned metadata (archive deleted) is skipped."""
    out: list[dict] = []
    if not config.UPLOAD_DIR.exists():
        return out
    for meta in config.UPLOAD_DIR.glob("*.json"):
        try:
            record = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        archive = config.UPLOAD_DIR / f"{record.get('upload_id')}.zip"
        if not archive.exists():
            continue
        out.append(record)
    out.sort(key=lambda r: r.get("uploaded_at") or 0, reverse=True)
    return out


def delete(upload_id: str) -> bool:
    existed = archive_path(upload_id).exists()
    archive_path(upload_id).unlink(missing_ok=True)
    _meta_path(upload_id).unlink(missing_ok=True)
    return existed


def _prune() -> None:
    """Keep only the most recent imports so /data cannot fill up silently."""
    records = list_uploads()
    for record in records[config.MAX_UPLOADS :]:
        try:
            delete(record["upload_id"])
        except Exception:
            pass


# --- analysis ----------------------------------------------------------


def summarise(path: Path) -> dict:
    """Cheap, offline description of an archive -- no CurseForge calls.

    This runs inside the upload request so the import dialog can say what the
    archive actually is before the user commits to a several-minute install.
    Only the archive's own metadata is read, so it stays fast on a 3 GB zip.
    """
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise UploadError(
            "that file is not a zip archive. Export the profile from the "
            "CurseForge app with 'Create Profile Export' and upload the .zip "
            "it produces."
        )
    with zf:
        bad = zf.testzip() if _small_enough(zf) else None
        if bad:
            raise UploadError(f"the archive is corrupt (bad entry: {bad})")
        try:
            plan = packs.analyse_archive(zf)
        except ValueError as e:
            raise UploadError(str(e))

        override_roots = sorted({
            m["target"].split("/")[0] for m in plan.overlay_members if "/" in m["target"]
        })
        override_jars = [
            m["target"] for m in plan.overlay_members
            if m["target"].startswith("mods/") and m["target"].endswith(".jar")
        ]
        if not plan.manifest_files and not plan.overlay_members:
            raise UploadError(
                "there is no modpack in that archive -- no manifest.json, no "
                "mods, no config. Upload the .zip the CurseForge app writes "
                "for a profile export, not a mod jar or a world folder."
            )

        return {
            "kind": plan.source,
            "name": plan.name,
            "version": plan.version,
            "minecraft": plan.mc_version,
            "loader": plan.loader,
            "loader_version": plan.loader_version,
            "crafty_loader": plan.crafty_loader,
            "java_version": plan.java_version,
            "recommended_ram_mb": plan.recommended_ram,
            "manifest_mods": len(plan.manifest_files),
            "override_jars": len(override_jars),
            "override_files": len(plan.overlay_members),
            "override_roots": override_roots,
            "warnings": plan.warnings,
            # A client export lists mods as ids only, so assembling it needs
            # the CurseForge API; a server pack ships the jars and does not.
            "needs_curseforge": plan.source == "manifest",
            "installable": bool(plan.crafty_loader and plan.mc_version),
        }


def _small_enough(zf: zipfile.ZipFile) -> bool:
    """testzip() decompresses everything, so only run it on modest archives."""
    return sum(i.file_size for i in zf.infolist()) <= 256 * 1024 * 1024
