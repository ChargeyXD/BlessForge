"""World backups on the node: content-addressed snapshots of a server folder.

From the Crafty comparison (design/CRAFTY-COMPARISON.md, item 1). Until now a
world existed once; a bad pack update, a griefed spawn or a corrupt region
was permanent.

A snapshot is a manifest -- every file's path, size, mtime, mode and SHA-256
-- and the files themselves live once each in an object store keyed by that
hash. So ten backups of an 8 GB modded world cost about one world plus what
changed, and a file whose size and mtime have not moved since the last
snapshot is not even read again (its hash is carried over). Per server:

    <backups_root>/<server uuid>/
        objects/ab/abcdef...        the file's bytes (or ...z: zlib-compressed)
        snapshots/<id>.json.gz      the manifest
        index.json                  the list: id, when, why, files, size, new bytes

Everything is read through the file jail: a link in the server folder is
never followed, a hard-linked file is skipped and named, and a restore
writes through the same jail as any upload. Running a server, the caller
saves first (save-off, save-all flush, wait for "Saved the game") and turns
saving back on after, so a live world is never copied mid-write.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import stat
import time
import uuid as uuidlib
import zipfile
import zlib
from pathlib import Path
from typing import Iterator

from agent import files

# Never in a backup: churn, or other tools' own backups. A server can add more.
NATIVE_TMP = ".blessforge-tmp"      # the JVM's temp folder, emptied at each start
DEFAULT_EXCLUDE = ("logs", "crash-reports", NATIVE_TMP, "cache", ".cache", "debug",
                   "backups", "simplebackups", ".connector")
CHUNK = 1024 * 1024
MANIFEST_VERSION = 1


class BackupError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _excluded(rel: str, exclude: tuple[str, ...]) -> bool:
    return any(rel == ex or rel.startswith(ex + "/") for ex in exclude)


def clean_exclude(extra) -> tuple[str, ...]:
    out = list(DEFAULT_EXCLUDE)
    for item in extra or []:
        rel = "/".join(files.parts_of(str(item)))
        if rel and rel not in out:
            out.append(rel)
    return tuple(out)


class Store:
    """One server's backups."""

    def __init__(self, root: Path, server_uuid: str):
        self.base = Path(root) / server_uuid
        self.objects = self.base / "objects"
        self.snapshots = self.base / "snapshots"
        self.index_path = self.base / "index.json"

    # --- the index ----------------------------------------------------------

    def index(self) -> list[dict]:
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return []

    def _write_index(self, items: list[dict]) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1), encoding="utf-8")
        tmp.replace(self.index_path)

    def manifest(self, snap_id: str) -> dict:
        path = self.snapshots / f"{snap_id}.json.gz"
        if not path.is_file() or "/" in snap_id or "\\" in snap_id or ".." in snap_id:
            raise BackupError(f"no backup {snap_id}", 404)
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)

    def object_path(self, sha: str) -> Path | None:
        raw = self.objects / sha[:2] / sha
        if raw.is_file():
            return raw
        packed = raw.with_name(sha + ".z")
        return packed if packed.is_file() else None

    def store_bytes(self) -> int:
        total = 0
        for dirpath, _dirs, names in os.walk(self.objects):
            for n in names:
                try:
                    total += os.lstat(os.path.join(dirpath, n)).st_size
                except OSError:
                    continue
        return total

    # --- taking one --------------------------------------------------------------

    def _walk(self, root: Path, exclude: tuple[str, ...]) -> Iterator[tuple[str, os.stat_result]]:
        """Every regular file under root, as (relative path, lstat). Links are
        never entered or listed; excluded folders are pruned, not walked."""
        base = str(root)
        walker = (os.fwalk(base, follow_symlinks=False) if files.SAFE_FDS
                  else ((d, ds, fs, None) for d, ds, fs in os.walk(base)))
        for dirpath, dirnames, filenames, dfd in walker:
            rel_dir = os.path.relpath(dirpath, base).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            keep = []
            for d in dirnames:
                rel = f"{rel_dir}/{d}" if rel_dir else d
                if not _excluded(rel, exclude):
                    keep.append(d)
            dirnames[:] = keep
            for name in filenames:
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if _excluded(rel, exclude):
                    continue
                try:
                    st = (os.stat(name, dir_fd=dfd, follow_symlinks=False) if dfd is not None
                          else os.lstat(os.path.join(dirpath, name)))
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    yield rel, st

    def take(self, root: Path, *, reason: str = "", exclude=(), compress: bool = False,
             progress=None) -> dict:
        """Snapshot `root`. Blocking: run it on a thread."""
        exclude = clean_exclude(exclude)
        snap_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuidlib.uuid4().hex[:6]
        previous = {}
        done = [s for s in self.index() if s.get("status") == "done"]
        if done:
            try:
                for rel, sha, size, mtime, _mode in self.manifest(done[0]["id"])["files"]:
                    previous[rel] = (sha, size, mtime)
            except BackupError:
                previous = {}
        self.objects.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(parents=True, exist_ok=True)
        tmp_dir = self.base / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        entries, skipped = [], []
        total = new_bytes = reused = 0
        started = time.time()
        for n, (rel, st) in enumerate(self._walk(root, exclude)):
            prev = previous.get(rel)
            if prev and prev[1] == st.st_size and prev[2] == st.st_mtime_ns \
                    and self.object_path(prev[0]) is not None:
                sha = prev[0]
                reused += 1
            else:
                try:
                    sha, written = self._store_file(root, rel, tmp_dir, compress)
                except files.JailError as e:
                    skipped.append(f"{rel}: {e}")
                    continue
                except OSError as e:
                    skipped.append(f"{rel}: {e.strerror or e}")
                    continue
                new_bytes += written
            entries.append([rel, sha, st.st_size, st.st_mtime_ns, stat.S_IMODE(st.st_mode)])
            total += st.st_size
            if progress and n % 200 == 0:
                progress(n, total)
        manifest = {"version": MANIFEST_VERSION, "id": snap_id, "created": started,
                    "reason": reason[:200], "exclude": list(exclude), "files": entries,
                    "skipped": skipped[:200]}
        path = self.snapshots / f"{snap_id}.json.gz"
        with gzip.open(path.with_suffix(".tmp"), "wt", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        path.with_suffix(".tmp").replace(path)
        summary = {"id": snap_id, "created": started, "reason": reason[:200],
                   "files": len(entries), "size": total, "new_bytes": new_bytes,
                   "reused": reused, "skipped": len(skipped), "seconds":
                   round(time.time() - started, 1), "status": "done"}
        self._write_index([summary] + self.index())
        return summary

    def _store_file(self, root: Path, rel: str, tmp_dir: Path, compress: bool
                    ) -> tuple[str, int]:
        """Hash `rel` while copying it into the store. (sha256, bytes written)."""
        h = hashlib.sha256()
        tmp = tmp_dir / uuidlib.uuid4().hex
        comp = zlib.compressobj(1) if compress else None
        written = 0
        try:
            with files.open_file(root, rel, "rb") as src, open(tmp, "wb") as out:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    data = comp.compress(chunk) if comp else chunk
                    out.write(data)
                    written += len(data)
                if comp:
                    tail = comp.flush()
                    out.write(tail)
                    written += len(tail)
            sha = h.hexdigest()
            if self.object_path(sha) is not None:
                tmp.unlink()
                return sha, 0
            dest = self.objects / sha[:2] / (sha + (".z" if compress else ""))
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp.replace(dest)
            return sha, written
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    # --- keeping and removing ------------------------------------------------------

    def delete(self, snap_id: str) -> None:
        self.manifest(snap_id)              # 404 for one that is not there
        (self.snapshots / f"{snap_id}.json.gz").unlink()
        self._write_index([s for s in self.index() if s.get("id") != snap_id])
        self.collect()

    def keep_only(self, keep: int) -> list[str]:
        """Delete all but the newest `keep` finished backups."""
        done = [s for s in self.index() if s.get("status") == "done"]
        gone = [s["id"] for s in done[keep:]] if keep and keep > 0 else []
        for snap_id in gone:
            try:
                (self.snapshots / f"{snap_id}.json.gz").unlink()
            except FileNotFoundError:
                pass
        if gone:
            self._write_index([s for s in self.index() if s.get("id") not in gone])
            self.collect()
        return gone

    def collect(self) -> int:
        """Delete objects no remaining manifest refers to. Returns bytes freed."""
        wanted = set()
        for s in self.index():
            try:
                wanted.update(e[1] for e in self.manifest(s["id"])["files"])
            except BackupError:
                continue
        freed = 0
        for dirpath, _dirs, names in os.walk(self.objects):
            for n in names:
                if n.removesuffix(".z") not in wanted:
                    p = os.path.join(dirpath, n)
                    try:
                        freed += os.lstat(p).st_size
                        os.unlink(p)
                    except OSError:
                        continue
        return freed

    # --- restoring and downloading ------------------------------------------------------

    def _read_object(self, sha: str) -> Iterator[bytes]:
        path = self.object_path(sha)
        if path is None:
            raise BackupError(f"a file of this backup is missing from the store ({sha[:12]})",
                              500)
        decomp = zlib.decompressobj() if path.name.endswith(".z") else None
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                yield decomp.decompress(chunk) if decomp else chunk
            if decomp:
                yield decomp.flush()

    def restore(self, root: Path, snap_id: str, progress=None) -> dict:
        """Make `root` exactly the snapshot: everything but the excluded
        folders is removed, then every file is written back. Blocking."""
        manifest = self.manifest(snap_id)
        exclude = tuple(manifest.get("exclude") or DEFAULT_EXCLUDE)
        for sha in {e[1] for e in manifest["files"]}:
            if self.object_path(sha) is None:
                raise BackupError("this backup is incomplete: a file is missing from the "
                                  "store, so nothing was changed", 500)
        removed = 0
        for name, meta in files.list_dir(root, ".").items():
            if _excluded(name, exclude) or name == NATIVE_TMP:
                continue
            files.delete(root, [name])
            removed += 1
        written = 0
        for n, (rel, sha, size, _mtime, mode) in enumerate(manifest["files"]):
            with files.open_file(root, rel, "wb") as out:
                for chunk in self._read_object(sha):
                    out.write(chunk)
            written += size
            if progress and n % 200 == 0:
                progress(n, written)
        return {"id": snap_id, "files": len(manifest["files"]), "bytes": written,
                "removed_top_level": removed}

    def zip_stream(self, snap_id: str) -> Iterator[bytes]:
        """The snapshot as a zip, generated as it is sent (never on disk)."""
        manifest = self.manifest(snap_id)

        class Sink:
            def __init__(self):
                self.chunks: list[bytes] = []
                self.pos = 0

            def write(self, b):
                self.chunks.append(bytes(b))
                self.pos += len(b)
                return len(b)

            def tell(self):
                return self.pos

            def flush(self):
                pass

        sink = Sink()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
            for rel, sha, _size, mtime, _mode in manifest["files"]:
                info = zipfile.ZipInfo(rel, time.localtime(mtime / 1e9)[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                with zf.open(info, "w", force_zip64=True) as dst:
                    for chunk in self._read_object(sha):
                        dst.write(chunk)
                        if sink.chunks:
                            yield b"".join(sink.chunks)
                            sink.chunks.clear()
            if sink.chunks:
                yield b"".join(sink.chunks)
                sink.chunks.clear()
        if sink.chunks:
            yield b"".join(sink.chunks)
