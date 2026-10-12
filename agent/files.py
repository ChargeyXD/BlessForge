"""Every file operation on a server folder, jailed to that folder.

The folder belongs to a game server, and the game server -- or a mod in it,
or the customer who uploaded the mod -- is not trusted. Anything it can put
in the folder it can use against the agent, which runs with more rights
than the container. So:

  * **No path leaves the folder.** Absolute paths, `..`, NUL bytes and drive
    letters are refused before anything touches the disk.
  * **No path goes through a link.** A symlink (or, on Windows, any reparse
    point -- a junction is a symlink by another name) anywhere along a path
    is refused, not followed. `world -> /etc` is just a refusal.
  * **No race.** On Linux every component is opened with O_NOFOLLOW relative
    to the already-open parent directory (openat), so swapping a checked
    directory for a symlink between the check and the use opens nothing --
    there is no gap between the two to race. (Windows has no openat; there
    the path is checked, the file opened, and then the OPEN HANDLE asked
    where it really is -- GetFinalPathNameByHandle -- and refused unless that
    is inside the folder. The agent runs on Linux; Windows is for
    development, and its listings are not race-free.)
  * **No hard links out.** A regular file with more than one link is
    refused for reading and writing: its other name may be outside the
    folder, and no amount of path checking can see where.
  * **No zip slip.** An archive is checked member by member before anything
    is extracted, and refused whole if one member would land outside its
    folder, is a link, or would blow past the size limit.

Every function takes the server root and a server-relative path, as the
HTTP API receives them.
"""
from __future__ import annotations

import io
import os
import posixpath
import shutil
import stat
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Linux has openat (dir_fd) and O_NOFOLLOW; that is the race-free path.
SAFE_FDS = (os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")
            and hasattr(os, "O_DIRECTORY"))
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _final_name = ctypes.windll.kernel32.GetFinalPathNameByHandleW
    _final_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                            wintypes.DWORD]
    _final_name.restype = wintypes.DWORD

MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_EXTRACT_BYTES = 20 * 1024 ** 3
MAX_EXTRACT_RATIO = 200          # a 1 MB zip may not claim to hold 200 MB+


class JailError(Exception):
    """A refusal. `status` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- paths ---------------------------------------------------------------------


def parts_of(rel: str | None) -> list[str]:
    """A server-relative path as its components, or JailError."""
    raw = (rel or ".").replace("\\", "/").strip()
    if "\x00" in raw:
        raise JailError("that path contains a null byte")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise JailError("only paths inside the server folder are allowed")
    out = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise JailError("that path points outside the server folder")
        out.append(part)
    return out


def check_name(name: str) -> str:
    n = (name or "").strip()
    if not n or "/" in n or "\\" in n or "\x00" in n or set(n) <= {"."}:
        raise JailError(f"{name!r} is not a valid name")
    if len(n) > 255:
        raise JailError("that name is too long")
    return n


def is_link(st: os.stat_result) -> bool:
    """A symlink, or on Windows any reparse point (junctions included)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & _REPARSE)


# --- the race-free walk (Linux) ----------------------------------------------------


@contextmanager
def _dir_fd(root: Path, dir_parts: list[str], *, create: bool = False) -> Iterator[int]:
    """An fd for root/dir_parts, every component opened with O_NOFOLLOW."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds = [os.open(str(root), flags)]
    try:
        for part in dir_parts:
            try:
                fds.append(os.open(part, flags, dir_fd=fds[-1]))
            except FileNotFoundError:
                if not create:
                    raise JailError(f"{'/'.join(dir_parts)} does not exist", 404)
                os.mkdir(part, 0o755, dir_fd=fds[-1])
                fds.append(os.open(part, flags, dir_fd=fds[-1]))
            except OSError as e:
                # ELOOP: it is a symlink. ENOTDIR: it is a file.
                raise JailError(f"{part} is a link or not a folder ({e.strerror})", 403)
        yield fds[-1]
    finally:
        for fd in reversed(fds):
            os.close(fd)


# --- the checked walk (Windows, for development) --------------------------------------


def _strip_prefix(p: str) -> str:
    r"""C:\x for \\?\C:\x, and \\host\share for \\?\UNC\host\share."""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    return p[4:] if p.startswith("\\\\?\\") else p


def _inside(root: Path, fd: int) -> bool:
    """Where an open file really is, asked of its handle: inside root or not.

    Whatever links were followed to open it, and whenever they were swapped
    in, the handle knows the file it ended up on.
    """
    buf = ctypes.create_unicode_buffer(32768)
    n = _final_name(msvcrt.get_osfhandle(fd), buf, 32768, 0)
    if not n or n >= 32768:
        return False
    final = os.path.normcase(_strip_prefix(buf.value))
    base = os.path.normcase(_strip_prefix(os.path.realpath(root))).rstrip("\\") + "\\"
    return final.startswith(base)


def _checked(root: Path, parts: list[str]) -> Path:
    """root/parts with no link anywhere along it. Missing tail is allowed."""
    st = os.lstat(root)
    if is_link(st):
        raise JailError("the server folder itself is a link", 403)
    path = root
    for part in parts:
        path = path / part
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return root.joinpath(*parts)
        if is_link(st):
            raise JailError(f"{part} is a link; links are not followed", 403)
    return path


def resolve(root: Path, rel: str) -> Path:
    """The real path for a server-relative one, every component checked."""
    parts = parts_of(rel)
    if SAFE_FDS:
        # The fd walk is the guarantee; this only produces a path for the
        # callers (listing, stat) that need one, after the same refusal.
        with _dir_fd(root, parts[:-1]) as fd:
            if parts:
                try:
                    st = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                    if is_link(st):
                        raise JailError(f"{parts[-1]} is a link; links are not "
                                        "followed", 403)
                except FileNotFoundError:
                    pass
        return root.joinpath(*parts)
    return _checked(root, parts)


@contextmanager
def open_file(root: Path, rel: str, mode: str) -> Iterator:
    """Open a file inside the jail. mode: "rb" or "wb" (write via temp + rename)."""
    parts = parts_of(rel)
    if not parts:
        raise JailError("that is the server folder, not a file")
    name = parts[-1]
    if mode == "rb":
        if SAFE_FDS:
            with _dir_fd(root, parts[:-1]) as dfd:
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
                except FileNotFoundError:
                    raise JailError(f"{rel} does not exist", 404)
                except OSError as e:
                    raise JailError(f"{name} cannot be opened ({e.strerror})", 403)
        else:
            path = _checked(root, parts)
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            except FileNotFoundError:
                raise JailError(f"{rel} does not exist", 404)
            except PermissionError:
                raise JailError(f"{rel} cannot be opened (a folder?)", 409)
            if not _inside(root, fd):
                os.close(fd)
                raise JailError(f"{rel} leads outside the server folder", 403)
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            os.close(fd)
            raise JailError(f"{rel} is a folder", 409)
        if st.st_nlink > 1:
            os.close(fd)
            raise JailError(f"{rel} has other hard links, which may be outside "
                            "the server folder; it is not served", 403)
        with os.fdopen(fd, "rb") as fh:
            yield fh
        return
    if mode == "wb":
        tmp_name = f".{name}.bf-{os.getpid()}-{time.monotonic_ns()}.tmp"
        if SAFE_FDS:
            with _dir_fd(root, parts[:-1], create=True) as dfd:
                try:
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    if is_link(st) or stat.S_ISDIR(st.st_mode):
                        raise JailError(f"{name} is a link or a folder", 403)
                    if st.st_nlink > 1:
                        raise JailError(f"{rel} has other hard links; it is not "
                                        "written", 403)
                except FileNotFoundError:
                    pass
                fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
                try:
                    with os.fdopen(fd, "wb") as fh:
                        yield fh
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.rename(tmp_name, name, src_dir_fd=dfd, dst_dir_fd=dfd)
                except BaseException:
                    try:
                        os.unlink(tmp_name, dir_fd=dfd)
                    except OSError:
                        pass
                    raise
            return
        parent = _checked(root, parts[:-1])
        parent.mkdir(parents=True, exist_ok=True)
        _checked(root, parts[:-1])          # still no link after creating it
        target = parent / name
        try:
            st = os.lstat(target)
            if is_link(st) or stat.S_ISDIR(st.st_mode):
                raise JailError(f"{name} is a link or a folder", 403)
            if st.st_nlink > 1:
                raise JailError(f"{rel} has other hard links; it is not written", 403)
        except FileNotFoundError:
            pass
        tmp = parent / tmp_name
        try:
            with open(tmp, "xb") as fh:
                if not _inside(root, fh.fileno()):
                    raise JailError(f"{rel} leads outside the server folder", 403)
                yield fh
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return
    raise ValueError(mode)


# --- operations ------------------------------------------------------------------


def list_dir(root: Path, rel: str = ".") -> dict:
    parts = parts_of(rel)
    base = "/".join(parts)
    out = {}

    def entry(name: str, st: os.stat_result) -> None:
        link = is_link(st)
        out[name] = {
            "path": f"{base}/{name}" if base else name,
            "dir": (not link) and stat.S_ISDIR(st.st_mode),
            "size": 0 if link or stat.S_ISDIR(st.st_mode) else st.st_size,
            "modified": st.st_mtime,
            **({"link": True} if link else {}),
        }

    if SAFE_FDS:
        # Listed through the fd that was opened with O_NOFOLLOW, so the
        # folder listed is the folder that was checked.
        with _dir_fd(root, parts) as fd:
            for name in os.listdir(fd):
                entry(name, os.stat(name, dir_fd=fd, follow_symlinks=False))
        return out
    path = _checked(root, parts)
    if not path.exists():
        raise JailError(f"{rel} does not exist", 404)
    if not path.is_dir():
        raise JailError(f"{rel} is not a folder", 409)
    with os.scandir(path) as it:
        for e in it:
            entry(e.name, e.stat(follow_symlinks=False))
    return out

def check_readable(root: Path, rel: str) -> None:
    """Raise JailError now if `rel` could not be served -- before a response
    has started streaming and it is too late to send a status code."""
    with open_file(root, rel, "rb"):
        pass


def read_bytes(root: Path, rel: str, limit: int | None = None) -> bytes:
    with open_file(root, rel, "rb") as fh:
        data = fh.read((limit + 1) if limit else -1)
    if limit and len(data) > limit:
        raise JailError(f"{rel} is larger than {limit // 1048576} MB", 413)
    return data


def read_tail(root: Path, rel: str, nbytes: int) -> str:
    """The last `nbytes` of a text file, from a whole line on. A log can be far
    past the read limit while the part that matters is at its end."""
    nbytes = max(1, min(int(nbytes), MAX_TEXT_BYTES))
    with open_file(root, rel, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - nbytes))
        data = fh.read(nbytes)
    text = data.decode("utf-8", "replace")
    if size > nbytes:
        text = text.split("\n", 1)[-1]
    return text


def read_text(root: Path, rel: str) -> str:
    data = read_bytes(root, rel, MAX_TEXT_BYTES)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise JailError(f"{rel} is not a readable text file", 415)


def write_bytes(root: Path, rel: str, data: bytes) -> None:
    with open_file(root, rel, "wb") as fh:
        fh.write(data)


def write_stream(root: Path, rel: str, chunks) -> int:
    """Write an iterable of byte chunks; returns the size written."""
    total = 0
    with open_file(root, rel, "wb") as fh:
        for chunk in chunks:
            fh.write(chunk)
            total += len(chunk)
    return total


def iter_file(root: Path, rel: str, size: int = 256 * 1024) -> Iterator[bytes]:
    with open_file(root, rel, "rb") as fh:
        while True:
            chunk = fh.read(size)
            if not chunk:
                return
            yield chunk


def mkdir(root: Path, parent: str, name: str) -> None:
    """Make root/parent/name and any missing folders above it."""
    parts = parts_of(parent) + [check_name(name)]
    if SAFE_FDS:
        # Each level is created and opened O_NOFOLLOW in turn; a file in the
        # way is ENOTDIR, a link in the way is ELOOP -- both refusals.
        with _dir_fd(root, parts, create=True):
            return
    path = _checked(root, parts)
    if path.exists() and not path.is_dir():
        raise JailError(f"{name} exists and is a file", 409)
    path.mkdir(parents=True, exist_ok=True)
    _checked(root, parts)


def create_file(root: Path, parent: str, name: str) -> None:
    rel = "/".join(parts_of(parent) + [check_name(name)])
    if resolve(root, rel).exists():
        return
    write_bytes(root, rel, b"")


def delete(root: Path, rels: list[str]) -> list[str]:
    done = []
    for rel in rels:
        parts = parts_of(rel)
        if not parts:
            raise JailError("refusing to delete the server folder itself")
        if SAFE_FDS:
            with _dir_fd(root, parts[:-1]) as dfd:
                try:
                    st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
                except FileNotFoundError:
                    raise JailError(f"{rel} does not exist", 404)
                if stat.S_ISDIR(st.st_mode):
                    # fd-based on Linux: a link inside the tree is removed,
                    # never followed (shutil.rmtree.avoids_symlink_attacks).
                    shutil.rmtree(parts[-1], dir_fd=dfd)
                else:
                    os.unlink(parts[-1], dir_fd=dfd)
        else:
            # The parents must be real folders; the entry itself may be a
            # link, which is removed -- never followed.
            path = _checked(root, parts[:-1]) / parts[-1]
            if not os.path.lexists(path):
                raise JailError(f"{rel} does not exist", 404)
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode) and not is_link(st):
                shutil.rmtree(path)
            elif stat.S_ISDIR(st.st_mode) or getattr(st, "st_reparse_tag", 0):
                os.rmdir(path)       # a junction: rmdir removes the link alone
            else:
                os.unlink(path)
        done.append("/".join(parts))
    return done

def _rename_at(root: Path, src_parts: list[str], dst_parts: list[str]) -> None:
    """Move one entry, with both parents opened O_NOFOLLOW (renameat)."""
    if SAFE_FDS:
        with _dir_fd(root, src_parts[:-1]) as sfd, _dir_fd(root, dst_parts[:-1]) as dfd:
            try:
                os.stat(src_parts[-1], dir_fd=sfd, follow_symlinks=False)
            except FileNotFoundError:
                raise JailError(f"{'/'.join(src_parts)} does not exist", 404)
            try:
                os.stat(dst_parts[-1], dir_fd=dfd, follow_symlinks=False)
                raise JailError(f"{dst_parts[-1]} already exists", 409)
            except FileNotFoundError:
                pass
            os.rename(src_parts[-1], dst_parts[-1], src_dir_fd=sfd, dst_dir_fd=dfd)
        return
    src = _checked(root, src_parts)
    if not os.path.lexists(src):
        raise JailError(f"{'/'.join(src_parts)} does not exist", 404)
    dst = _checked(root, dst_parts)
    if os.path.lexists(dst):
        raise JailError(f"{dst_parts[-1]} already exists", 409)
    os.rename(src, dst)


def rename(root: Path, rel: str, new_name: str) -> str:
    parts = parts_of(rel)
    if not parts:
        raise JailError("that is the server folder")
    dst = parts[:-1] + [check_name(new_name)]
    _rename_at(root, parts, dst)
    return "/".join(dst)

def move(root: Path, rel: str, dest_dir: str) -> str:
    parts = parts_of(rel)
    if not parts:
        raise JailError("that is the server folder")
    dest_parts = parts_of(dest_dir)
    if dest_parts[:len(parts)] == parts:
        raise JailError("cannot move a folder into itself")
    target = dest_parts + [parts[-1]]
    _rename_at(root, parts, target)
    return "/".join(target)

def _copy_tree(root: Path, src: list[str], dst: list[str]) -> None:
    """Copy a folder file by file through the jail. Links are skipped."""
    mkdir(root, "/".join(dst[:-1]) or ".", dst[-1])
    for name, meta in list_dir(root, "/".join(src)).items():
        if meta.get("link"):
            continue
        if meta["dir"]:
            _copy_tree(root, src + [name], dst + [name])
        else:
            with open_file(root, "/".join(src + [name]), "rb") as fin, \
                    open_file(root, "/".join(dst + [name]), "wb") as fout:
                shutil.copyfileobj(fin, fout, 1024 * 1024)


def copy(root: Path, rel: str, dest_dir: str) -> str:
    parts = parts_of(rel)
    if not parts:
        raise JailError("that is the server folder")
    dest_parts = parts_of(dest_dir)
    parent = list_dir(root, "/".join(parts[:-1]))
    meta = parent.get(parts[-1])
    if meta is None:
        raise JailError(f"{rel} does not exist", 404)
    if meta.get("link"):
        raise JailError("links are not copied", 403)
    name = parts[-1]
    if name in list_dir(root, dest_dir):
        stem, ext = posixpath.splitext(name)
        name = f"{stem} copy{ext}"
    target = dest_parts + [name]
    if meta["dir"]:
        if dest_parts[:len(parts)] == parts:
            raise JailError("cannot copy a folder into itself")
        _copy_tree(root, parts, target)
    else:
        with open_file(root, rel, "rb") as fin, \
                open_file(root, "/".join(target), "wb") as fout:
            shutil.copyfileobj(fin, fout, 1024 * 1024)
    return "/".join(target)

def chmod(root: Path, rel: str, mode: int) -> None:
    parts = parts_of(rel)
    mode = int(mode) & 0o777                 # no setuid, setgid or sticky
    if SAFE_FDS and parts:
        with _dir_fd(root, parts[:-1]) as dfd:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
            except FileNotFoundError:
                raise JailError(f"{rel} does not exist", 404)
            except OSError:
                raise JailError("links are not changed", 403)
            try:
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
        return
    path = _checked(root, parts)
    if not os.path.lexists(path):
        raise JailError(f"{rel} does not exist", 404)
    os.chmod(path, mode)

def _walk(base: Path) -> Iterator[tuple[str, list[tuple[str, os.stat_result]]]]:
    """(folder, [(name, lstat)]) for base and every folder under it.

    Links are listed but never entered. os.walk will not do here: on Windows
    it walks into junctions (they are not "symlinks" to it), so a junction
    to a drive root would make a search list the whole drive. On Linux fwalk holds
    each folder open as it goes, so a folder swapped for a link mid-walk is
    not entered either.
    """
    if SAFE_FDS:
        for dirpath, dirnames, filenames, dfd in os.fwalk(base, follow_symlinks=False):
            entries = []
            for name in dirnames + filenames:
                try:
                    entries.append((name, os.stat(name, dir_fd=dfd, follow_symlinks=False)))
                except OSError:
                    continue
            yield dirpath, entries
        return
    stack = [str(base)]
    while stack:
        folder = stack.pop()
        entries = []
        try:
            with os.scandir(folder) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    entries.append((e.name, st))
                    if stat.S_ISDIR(st.st_mode) and not is_link(st):
                        stack.append(e.path)
        except OSError:
            continue
        yield folder, entries


def usage(root: Path, rel: str = ".") -> dict:
    """Bytes per top-level folder, counting links as nothing."""
    base = resolve(root, rel)
    out = []
    total = 0
    with os.scandir(base) as it:
        for e in it:
            st = e.stat(follow_symlinks=False)
            size = files = 0
            if is_link(st):
                pass
            elif stat.S_ISDIR(st.st_mode):
                for _folder, entries in _walk(Path(e.path)):
                    for _name, fst in entries:
                        if not is_link(fst) and not stat.S_ISDIR(fst.st_mode):
                            size += fst.st_size
                            files += 1
            else:
                size, files = st.st_size, 1
            total += size
            out.append({"name": e.name, "dir": stat.S_ISDIR(st.st_mode) and not is_link(st),
                        "bytes": size, "files": files})
    out.sort(key=lambda d: -d["bytes"])
    return {"folders": out, "total_bytes": total}


def search(root: Path, query: str, rel: str = ".", limit: int = 300) -> list[dict]:
    q = (query or "").strip().lower()
    if len(q) < 2:
        raise JailError("type at least two characters to search")
    base = resolve(root, rel)
    hits = []
    for folder, entries in _walk(base):
        for name, st in entries:
            if q in name.lower():
                rel_path = Path(folder, name).relative_to(root).as_posix()
                hits.append({"name": name, "path": rel_path,
                             "dir": stat.S_ISDIR(st.st_mode) and not is_link(st),
                             "size": st.st_size, "modified": st.st_mtime})
                if len(hits) >= limit:
                    return hits
    return hits


# --- archives ------------------------------------------------------------------


def check_archive(zf: zipfile.ZipFile, archive_size: int,
                  limit: int = MAX_EXTRACT_BYTES) -> list[tuple[zipfile.ZipInfo, list[str]]]:
    """Every member, checked, before anything is written. Raises on the first bad one."""
    members = []
    total = 0
    for info in zf.infolist():
        try:
            parts = parts_of(info.filename)
        except JailError:
            raise JailError(f"the archive has a member outside its folder "
                            f"({info.filename!r}); nothing was extracted")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise JailError(f"the archive contains a link ({info.filename!r}); "
                            "nothing was extracted")
        total += info.file_size
        members.append((info, parts))
    if total > limit:
        raise JailError(f"the archive unpacks to {total // 1048576} MB, more than "
                        f"the {limit // 1048576} MB allowed; nothing was extracted", 413)
    if archive_size and total > archive_size * MAX_EXTRACT_RATIO and total > 64 * 1048576:
        raise JailError("the archive claims to unpack to far more than its size "
                        "could hold (a zip bomb?); nothing was extracted", 413)
    return members


def extract(root: Path, rel_zip: str, limit: int = MAX_EXTRACT_BYTES) -> list[str]:
    """Unpack a zip in the folder next to itself. All or nothing on the checks."""
    zip_parts = parts_of(rel_zip)
    data_size = os.lstat(resolve(root, rel_zip)).st_size
    with open_file(root, rel_zip, "rb") as fh:
        raw = fh.read() if data_size < 256 * 1048576 else None
        source = io.BytesIO(raw) if raw is not None else None
        if source is None:
            # Large archive: stream from the already-open, jail-checked handle.
            source = fh
            zf = zipfile.ZipFile(source)
        else:
            zf = zipfile.ZipFile(source)
        with zf:
            members = check_archive(zf, data_size, limit)
            base = zip_parts[:-1]
            written = []
            for info, parts in members:
                if not parts:
                    continue
                target = "/".join(base + parts)
                if info.is_dir():
                    mkdir(root, "/".join(base + parts[:-1]) or ".", parts[-1])
                    continue
                with zf.open(info) as src, open_file(root, target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                written.append(target)
    return written


def compress(root: Path, rels: list[str], dest_rel: str) -> str:
    """Zip files or folders into dest_rel. Links are skipped, never followed."""
    def files_under(rel_parts: list[str]):
        listing = list_dir(root, "/".join(rel_parts))
        for name, meta in listing.items():
            if meta.get("link"):
                continue
            if meta["dir"]:
                yield from files_under(rel_parts + [name])
            else:
                yield "/".join(rel_parts + [name])

    with open_file(root, dest_rel, "wb") as out:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in rels:
                parts = parts_of(rel)
                parent = list_dir(root, "/".join(parts[:-1]))
                meta = parent.get(parts[-1]) if parts else {"dir": True}
                if meta is None or meta.get("link"):
                    continue
                for arc in (files_under(parts) if meta["dir"] else ["/".join(parts)]):
                    if arc == "/".join(parts_of(dest_rel)):
                        continue
                    with open_file(root, arc, "rb") as src, zf.open(arc, "w") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
    return dest_rel

