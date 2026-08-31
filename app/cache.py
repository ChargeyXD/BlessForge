"""Keeping the download cache from growing without limit.

Everything under CACHE_DIR is a second copy of something the mod sites will
hand out again -- jars pinned by file id, and the pack archives an install
was assembled from. None of it is needed once an install finishes, and none
of it was ever removed, so a working BlessForge accumulates gigabytes on the
same disk as the Minecraft worlds.

Pruning is least-recently-used by mtime, runs after a download rather than on
a timer, and never touches DATA_DIR itself -- uploads and roulette exports
live outside CACHE_DIR precisely because they cannot be re-downloaded.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from app import config

log = logging.getLogger("blessforge.cache")


def usage_bytes() -> int:
    total = 0
    for root, _dirs, files in os.walk(config.CACHE_DIR):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def prune(limit_gb: int | None = None) -> dict:
    """Delete the oldest cached files until the cache fits under the limit."""
    limit = (config.MAX_CACHE_GB if limit_gb is None else limit_gb) * 1024**3
    if limit <= 0 or not config.CACHE_DIR.exists():
        return {"pruned": 0, "freed": 0, "size": 0, "limit": limit}

    entries: list[tuple[float, int, Path]] = []
    for root, _dirs, files in os.walk(config.CACHE_DIR):
        for f in files:
            path = Path(root) / f
            try:
                st = path.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, path))

    size = sum(e[1] for e in entries)
    if size <= limit:
        return {"pruned": 0, "freed": 0, "size": size, "limit": limit}

    entries.sort()  # oldest first
    freed = pruned = 0
    for _mtime, nbytes, path in entries:
        if size - freed <= limit:
            break
        try:
            path.unlink()
        except OSError:
            continue
        freed += nbytes
        pruned += 1
    if pruned:
        log.info("cache prune: removed %d files, freed %.1f GB (limit %d GB)",
                 pruned, freed / 1024**3, limit // 1024**3)
    return {"pruned": pruned, "freed": freed, "size": size - freed, "limit": limit}
