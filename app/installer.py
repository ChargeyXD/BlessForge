"""The install engine: modpack -> live Crafty instance.

A pack reaches this module from one of two places, and everything after step
1 is identical for both:

  * the **CurseForge catalogue**, addressed by (mod_id, file_id); or
  * an **imported archive** the user uploaded, addressed by upload_id --
    a profile export from the CurseForge app, which has no catalogue ids
    because it was never published.

Flow, whichever archive shape we get:

  1. Fetch and analyse the pack archive (server pack preferred, client
     manifest as fallback) to learn loader + Minecraft version.
  2. Create the Crafty instance with the matching loader from Crafty's own
     jar catalog, so Crafty produces a correct execution command. Getting
     the Forge/NeoForge launch arguments right by hand is the single most
     error-prone part of a modded server, so we let Crafty own it.
  3. Wait for that loader install to finish on disk.
  4. Lay the pack's files over the top in bounded-size batches (zip ->
     upload -> ask Crafty to unzip), so memory stays flat regardless of
     pack size.
  5. Record exactly what was installed in .modpack-studio.json for later
     version switching, update checks and diagnostics.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from app import (cache, config, crafty, curseforge, jarmeta, modrinth,
                 optimizer, packs, properties, uploads)
from app import preflight as preflight_mod
from app import specs
from app.jobs import Job
from app.packs import PackPlan

# Upload batches are capped so a 3 GB pack never lands in memory at once.
#
# The cap used to be the only thing standing between a big pack and the
# machine's RAM, because every file in a batch was held as `bytes` and then
# copied twice more (into a BytesIO zip, then out of it). Now that batches
# are built on disk and streamed, this only bounds how much scratch space one
# batch occupies and how often we round-trip to Crafty -- so it can be
# smaller, which also makes progress smoother.
BATCH_BYTES = 64 * 1024 * 1024
BATCH_FILES = 400


class PackEntry:
    """One file destined for the instance, and where to read it from.

    Deliberately not the bytes. A modpack install moves a few hundred
    megabytes to a few gigabytes, and the whole reason this app used ~2.3 GB
    of RAM was that every jar and every override was held in memory from the
    moment it was fetched until the moment the last batch finished uploading.
    An entry is a *reference*: a cached jar's path, a member of the pack
    archive, or -- only for things generated on the fly -- actual bytes.
    """

    __slots__ = ("target", "path", "member", "blob", "size")

    def __init__(self, target: str, *, path: Path | None = None,
                 member: str | None = None, blob: bytes | None = None,
                 size: int = 0):
        self.target = target
        self.path = path
        self.member = member
        self.blob = blob
        self.size = size

    def write_into(self, archive: zipfile.ZipFile,
                   source: zipfile.ZipFile | None) -> None:
        """Copy this entry into an upload archive without buffering it."""
        if self.blob is not None:
            archive.writestr(self.target, self.blob)
        elif self.path is not None:
            archive.write(self.path, self.target)
        elif self.member is not None and source is not None:
            # Member-to-member: both sides are streams, so a 400 MB world
            # folder costs a 64 KB buffer rather than 400 MB.
            with source.open(self.member) as src, \
                    archive.open(self.target, "w") as dst:
                shutil.copyfileobj(src, dst, 1024 * 64)


def _cache_path(file_id: int, file_name: str) -> Path:
    safe = re.sub(r"[^\w.\-]+", "_", file_name or f"{file_id}.zip")
    return config.CACHE_DIR / f"{file_id}-{safe}"


async def fetch_pack_archive(job: Job, mod_id: int, file_id: int
                             ) -> tuple[Path, dict]:
    """Download a pack archive to disk and return its path.

    Deliberately a path, not bytes. These archives run from tens of megabytes
    to just under a gigabyte (the Better MC server pack in this machine's
    cache is 983 MB), and `zipfile` reads members perfectly well from a file
    -- so reading the whole thing into memory bought nothing and cost exactly
    its own size for the length of the install.

    Preflight and install both need the same archive, so the cached copy is
    reused; when the cache is unwritable it falls back to a temp file rather
    than to memory.
    """
    meta = await curseforge.get_file(mod_id, file_id)
    path = _cache_path(file_id, meta.get("file_name", ""))
    expected = meta.get("size") or 0

    if path.exists() and (not expected or abs(path.stat().st_size - expected) < 1024):
        job.log_line(f"Using cached {meta.get('file_name')}")
        return path, meta

    size_mb = expected / (1024 * 1024)
    job.log_line(f"Downloading {meta.get('file_name')} ({size_mb:.0f} MB)")
    data = await curseforge.download(meta)
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as e:
        job.log_line(f"Could not cache the archive: {e}", "warn")
        fallback = Path(tempfile.gettempdir()) / path.name
        fallback.write_bytes(data)
        path = fallback
    finally:
        del data
    return path, meta


async def preflight(
    job: Job,
    *,
    mod_id: int | None = None,
    file_id: int | None = None,
    upload_id: str | None = None,
    prefer_server_pack: bool = True,
) -> dict:
    """Analyse a pack without installing it.

    Produces the loader/version summary plus the list of mods that look
    client-only, each with the evidence behind that call, so the user can
    approve the removals rather than have them happen silently.
    """
    plan, zf, file_meta = await _load_plan(
        job, mod_id=mod_id, file_id=file_id, upload_id=upload_id,
        prefer_server_pack=prefer_server_pack,
    )

    if plan.source == "manifest":
        # The archive is passed in so hand-added jars sitting in
        # overrides/mods -- normal in a private export, absent from the
        # manifest -- get reviewed alongside the catalogue mods.
        review = await preflight_mod.analyse_manifest_pack(job, plan, zf=zf)
    else:
        review = await preflight_mod.analyse_server_pack_jars(job, zf, plan)

    ram_min, ram_max = packs.mem_from_ram_hint(
        plan.recommended_ram, config.DEFAULT_MEM_MIN, config.DEFAULT_MEM_MAX
    )
    host = specs.effective_host()
    memory = specs.recommend_memory(
        pack_recommended_mb=plan.recommended_ram,
        mod_count=review["total_mods"],
        host=host,
    )
    job.set_step("Analysis complete", 100)
    return {
        "mod_id": mod_id,
        "file_id": file_id,
        "upload_id": upload_id,
        "pack": {
            "name": plan.name,
            # Matches what the install will record: an export rarely names a
            # version, and the archive is reported separately rather than
            # standing in for one.
            "version": plan.version or (
                "" if upload_id else file_meta.get("display_name")
            ),
            "install_source": plan.source,
            "source": "upload" if upload_id else "curseforge",
            "archive": file_meta.get("file_name") if upload_id else None,
        },
        "minecraft": plan.mc_version,
        "loader": plan.loader,
        "loader_version": plan.loader_version,
        "crafty_loader": plan.crafty_loader,
        "recommended_ram_mb": plan.recommended_ram,
        "java_version": plan.java_version,
        "memory": memory,
        "host": host,
        "warnings": plan.warnings,
        "review": review,
        "suggested_mem_max": ram_max,
    }


async def resolve_pack_plan(mod_id: int, file_id: int, prefer_server_pack: bool = True
                            ) -> dict:
    """Inspect a pack version without installing it (drives the preview UI)."""
    file_meta = await curseforge.get_file(mod_id, file_id)
    server_pack_id = file_meta.get("server_pack_file_id")
    return {
        "file": file_meta,
        "has_server_pack": bool(server_pack_id),
        "server_pack_file_id": server_pack_id,
        "will_use": "server_pack" if (server_pack_id and prefer_server_pack)
        else "manifest",
    }


async def _load_plan(
    job: Job,
    *,
    mod_id: int | None = None,
    file_id: int | None = None,
    upload_id: str | None = None,
    prefer_server_pack: bool = True,
) -> tuple[PackPlan, zipfile.ZipFile, dict]:
    """Get the pack archive and work out what is in it, from either source."""
    if upload_id:
        return _load_upload_plan(job, upload_id)
    if not (mod_id and file_id):
        raise ValueError("either mod_id + file_id or upload_id is required")

    file_meta = await curseforge.get_file(mod_id, file_id)
    server_pack_id = file_meta.get("server_pack_file_id")

    if server_pack_id and prefer_server_pack:
        job.set_step("Downloading server pack", 5)
        try:
            archive, sp_meta = await fetch_pack_archive(job, mod_id, server_pack_id)
            zf = zipfile.ZipFile(archive)
            plan = packs.analyse_server_pack(zf)
            plan.name = plan.name or file_meta.get("display_name") or ""
            job.log_line(
                f"Server pack: {len(plan.overlay_members)} files, "
                f"loader={plan.loader or '?'} {plan.loader_version or ''}"
            )
            # A server pack with no mods is a downloader-script pack; the
            # manifest route is the only way to get real content.
            if any(m["target"].startswith("mods/") for m in plan.overlay_members):
                return plan, zf, file_meta
            job.log_line(
                "Server pack contains no mods (installer-script pack) -- "
                "falling back to the client manifest.",
                "warn",
            )
        except Exception as e:
            job.log_line(f"Server pack unusable ({e}); using client manifest.", "warn")

    job.set_step("Downloading pack manifest", 5)
    archive, _ = await fetch_pack_archive(job, mod_id, file_id)
    zf = zipfile.ZipFile(archive)
    plan = packs.analyse_client_pack(zf)
    plan.name = plan.name or file_meta.get("display_name") or ""
    job.log_line(
        f"Manifest pack: {len(plan.manifest_files)} mods + "
        f"{len(plan.overlay_members)} override files, "
        f"loader={plan.loader} {plan.loader_version}"
    )
    return plan, zf, file_meta


def _load_upload_plan(job: Job, upload_id: str
                      ) -> tuple[PackPlan, zipfile.ZipFile, dict]:
    """Same thing for an imported archive: it is already on disk.

    Opened from the path rather than read into memory -- an export carrying a
    world folder can be gigabytes, and only a few members are ever needed at
    once.
    """
    record = uploads.get(upload_id)
    path = uploads.archive_path(upload_id)
    job.set_step("Reading the imported archive", 5)

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"the imported archive is not a readable zip: {e}")

    plan = packs.analyse_archive(zf)
    plan.name = plan.name or Path(record.get("file_name", "")).stem or "Imported pack"
    file_meta = {
        "display_name": record.get("file_name"),
        "file_name": record.get("file_name"),
        "size": record.get("size"),
    }

    if plan.source == "manifest":
        job.log_line(
            f"Imported export '{plan.name}': {len(plan.manifest_files)} mods "
            f"from CurseForge + {len(packs.overlay_jars(plan))} bundled jars + "
            f"{len(plan.overlay_members)} override files, "
            f"loader={plan.loader or '?'} {plan.loader_version}"
        )
        if plan.manifest_files and not config.CURSEFORGE_API_KEY:
            # Nothing later in the flow can recover from this: the export
            # names its mods by CurseForge id and nothing else.
            raise RuntimeError(
                "This export lists its mods as CurseForge project ids, so a "
                "CurseForge API key is required to fetch them. Set "
                "CURSEFORGE_API_KEY_B64 and restart BlessForge."
            )
    else:
        job.log_line(
            f"Imported server pack '{plan.name}': "
            f"{len(plan.overlay_members)} files, "
            f"loader={plan.loader or '?'} {plan.loader_version}"
        )
    return plan, zf, file_meta


async def _download_manifest_mods(
    job: Job, plan: PackPlan, skip_client_only: bool,
    exclude_files: set[str] | None = None,
    disable_files: set[str] | None = None,
    client_reasons: dict[str, list[str]] | None = None,
) -> tuple[list[PackEntry], list[dict], list[dict], list[str], list[str]]:
    """Resolve and fetch every mod the manifest lists.

    Returns (files, records, problems, matched, disabled).

    Client-only mods are installed *disabled* rather than dropped. Stripping
    them made the instance a lie: the pack no longer matched what the user
    exported, nothing on the Mods tab explained where a mod had gone, and
    re-enabling one meant finding and downloading it by hand. A jar written
    as `<name>.jar.disabled` is inert to every loader -- the same convention
    Crafty and the CurseForge app use -- so the server still boots clean, but
    the mod is present, labelled, and one click from coming back.
    """
    entries = plan.manifest_files
    file_ids = [e["fileID"] for e in entries if e.get("fileID")]
    job.set_step(f"Resolving {len(file_ids)} mod files", 12)
    file_meta = await curseforge.get_files(file_ids)

    mod_ids = {m.get("mod_id") for m in file_meta.values() if m.get("mod_id")}
    projects = await curseforge.get_mods(mod_ids) if mod_ids else {}

    results: list[PackEntry] = []
    records: list[dict] = []
    problems: list[dict] = []
    matched_names: list[str] = []
    disabled_client: list[str] = []
    wrong_loader_mods: list[str] = []
    reasons = client_reasons or {}

    sem = asyncio.Semaphore(config.DOWNLOAD_CONCURRENCY)
    done = 0
    total = len(entries)
    lock = asyncio.Lock()

    async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:

        async def one(entry: dict):
            nonlocal done
            fid = entry.get("fileID")
            pid = entry.get("projectID")
            meta = file_meta.get(fid)
            project = projects.get(pid, {})
            name = project.get("name") or (meta or {}).get("file_name") or str(pid)

            async with sem:
                try:
                    if not meta:
                        problems.append({
                            "project_id": pid, "file_id": fid, "name": name,
                            "reason": "CurseForge returned no metadata for this file",
                        })
                        return
                    fname = meta.get("file_name") or f"{fid}.jar"
                    # The review step's decisions always win -- the user has
                    # already seen the evidence and made the call, so no
                    # heuristic should second-guess it.
                    drop = exclude_files is not None and fname in exclude_files
                    if drop:
                        matched_names.append(fname)
                        return
                    # Non-jar manifest entries are resource packs etc.
                    if not fname.lower().endswith(".jar"):
                        return

                    client_only = False
                    why: list[str] = []
                    if disable_files is not None and fname in disable_files:
                        client_only = True
                        matched_names.append(fname)
                        why = reasons.get(fname) or [
                            "flagged as client-only in the pre-install review"
                        ]

                    # Reuses whatever the review step already downloaded, and
                    # keeps it on disk: holding 300 jars in memory until the
                    # last batch uploads is what made this app's footprint
                    # scale with pack size.
                    cached = await curseforge.cache_jar(meta, client)
                    blob = None if cached else await curseforge.download_cached(
                        meta, client)

                    # With no review to go on, fall back to the jar's own
                    # declaration: Fabric/Quilt state their side outright, and
                    # a client-only mod left enabled takes the server down.
                    if not client_only and disable_files is None and skip_client_only:
                        probe = blob if blob is not None else cached.read_bytes()
                        info = jarmeta.parse(probe, fname)
                        del probe
                        if info.get("side") == "client":
                            client_only = True
                            why = ["the jar declares environment=client"]
                        elif packs.is_client_only_jar(fname):
                            client_only = True
                            why = ["name matches a known client-only mod"]

                    # A jar built for another loader is not a judgement call:
                    # Forge cannot read neoforge.mods.toml and will silently
                    # ignore the file, so the mod is absent with nothing said.
                    # Installing it disabled keeps the pack honest and says
                    # why, instead of leaving dead weight in mods/.
                    wrong_loader = False
                    if plan.loader:
                        markers = jarmeta.loader_markers(cached if cached else blob)
                        if not jarmeta.fits_loader(plan.loader, markers,
                                                   plan.mc_version or ""):
                            wrong_loader = True
                            why = [f"built for {'/'.join(sorted(markers))}, "
                                   f"but this server runs {plan.loader}"]

                    target = fname + (
                        ".disabled" if client_only or wrong_loader else "")
                    if client_only:
                        disabled_client.append(fname)
                    if wrong_loader:
                        wrong_loader_mods.append(fname)
                        # Rides the same "installed but disabled" list so the
                        # Mods tab, the manifest and the summary all already
                        # know how to show it.
                        disabled_client.append(fname)
                    results.append(PackEntry(
                        target, path=cached, blob=blob,
                        size=(cached.stat().st_size if cached else len(blob or b"")),
                    ))
                    records.append({
                        # Recorded under the enabled name whatever its state
                        # on disk, so toggling a mod never orphans its record.
                        "file": f"mods/{fname}",
                        "source": "curseforge",
                        "project_id": pid,
                        "file_id": fid,
                        "name": project.get("name") or fname,
                        "version": meta.get("display_name"),
                        # The catalogue already told us what this mod looks
                        # like; not recording it meant every row in the Mods
                        # list fell back to two grey initials until someone
                        # ran Identify, which re-fetched what was already here.
                        "logo": project.get("logo"),
                        "summary": project.get("summary"),
                        "required": entry.get("required", True),
                        "client_only": client_only,
                        "client_only_reasons": why if client_only else [],
                        "wrong_loader": wrong_loader,
                        "wrong_loader_reason": why[0] if wrong_loader else None,
                        "identified_at": time.time(),
                    })
                except Exception as e:
                    problems.append({
                        "project_id": pid, "file_id": fid, "name": name,
                        "reason": str(e),
                    })
                finally:
                    async with lock:
                        done += 1
                        if done % 10 == 0 or done == total:
                            job.set_step(
                                f"Downloading mods ({done}/{total})",
                                12 + 43 * (done / max(total, 1)),
                            )

        await asyncio.gather(*(one(e) for e in entries))

    if wrong_loader_mods:
        job.log_line(
            f"{len(wrong_loader_mods)} mod(s) are built for another loader and "
            f"were installed disabled -- {plan.loader} cannot load them: "
            + ", ".join(sorted(wrong_loader_mods)[:8])
            + ("..." if len(wrong_loader_mods) > 8 else ""),
            "warn",
        )
    client_only_disabled = [f for f in disabled_client if f not in set(wrong_loader_mods)]
    if client_only_disabled:
        job.log_line(
            f"Installed {len(client_only_disabled)} client-only mods as disabled "
            "(they are on the Mods tab, tagged client-side, and can be "
            "re-enabled in one click): "
            + ", ".join(sorted(client_only_disabled)[:8])
            + ("..." if len(client_only_disabled) > 8 else "")
        )
    if problems:
        job.log_line(f"{len(problems)} mods could not be downloaded", "warn")
        for p in problems[:10]:
            job.log_line(f"  - {p['name']}: {p['reason']}", "warn")
    return results, records, problems, matched_names, disabled_client


def _batch(items: list[PackEntry]) -> list[list[PackEntry]]:
    """Split entries into upload batches bounded by size and count."""
    batches: list[list[PackEntry]] = []
    current: list[PackEntry] = []
    size = 0
    for entry in items:
        if current and (size + entry.size > BATCH_BYTES
                        or len(current) >= BATCH_FILES):
            batches.append(current)
            current, size = [], 0
        current.append(entry)
        size += entry.size
    if current:
        batches.append(current)
    return batches


async def _push_batches(
    job: Job, server_id: str, items: list[PackEntry],
    start_pct: float, end_pct: float, label: str,
    source: zipfile.ZipFile | None = None,
) -> None:
    """Zip -> upload -> unzip, in batches, without buffering any of it.

    Both halves used to happen in memory: the batch was assembled in a
    `BytesIO` and then `getvalue()` copied the lot again, so a 120 MB batch
    briefly cost 240 MB *on top of* the source bytes it was built from. The
    archive is now written to a scratch file and streamed to Crafty from
    there, so a batch of any size costs one 8 MB upload chunk.
    """
    batches = _batch(items)
    if not batches:
        return
    scratch = config.CACHE_DIR / "batches"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
    except OSError:
        scratch = Path(tempfile.gettempdir())

    for i, batch in enumerate(batches, 1):
        zip_name = f".studio-batch-{int(time.time())}-{i}.zip"
        local = scratch / zip_name
        try:
            with zipfile.ZipFile(local, "w", zipfile.ZIP_STORED) as archive:
                for entry in batch:
                    entry.write_into(archive, source)
            size_mb = local.stat().st_size / 1048576
            pct = start_pct + (end_pct - start_pct) * ((i - 1) / len(batches))
            job.set_step(f"{label} ({i}/{len(batches)}, {size_mb:.0f} MB)", pct)

            await crafty.upload_path(server_id, ".", zip_name, local)
        finally:
            # The scratch copy has served its purpose the moment it is sent;
            # leaving it would double the disk cost of every install.
            try:
                local.unlink()
            except OSError:
                pass

        await crafty.unzip(server_id, zip_name)

        # Unzip runs on a background thread: wait for a member to appear.
        probe = batch[0].target
        if not await crafty.wait_for_path(server_id, probe, timeout=600):
            job.log_line(
                f"Timed out waiting for {probe} to appear after extraction", "warn"
            )
        # Give the extractor a moment to drain the rest of the archive.
        await asyncio.sleep(2)
        try:
            await crafty.delete_paths(server_id, [zip_name])
        except Exception:
            job.log_line(f"Could not remove temp archive {zip_name}", "warn")


# Crafty's create call points `execution_command` at the loader INSTALLER
# (`-jar forge-installer-1.20.1.jar --installServer`) and only rewrites it to
# the real launch command once its own installer thread has finished. So the
# presence of a command means nothing; the absence of this marker is what
# says the loader is actually installed.
INSTALLER_MARKER = "--installServer"
MODDED_LOADERS = ("forge-installer", "neoforge-installer")

# Crafty sleeps 3s before starting the download, then allows three retries
# with a 2/4/8s backoff -- about 17s to fail. Past this the jar is not coming.
LOADER_DOWNLOAD_GRACE = 75

# Crafty streams the jar straight to its final path, so a download in flight
# shows up as a file whose mtime keeps advancing. If nothing in the directory
# has moved for this long and the loader still is not installed, whatever was
# happening has stopped -- including the case where the connection died
# mid-stream and left a truncated jar behind, which "is there a .jar" cannot
# see on its own.
#
# Three minutes, not one: Crafty reports mtimes at minute resolution, so a
# perfectly healthy install can look motionless for a while.
LOADER_STALL = 180


def _loader_installed(loader_type: str, command: str, names: set[str],
                      executable: str) -> bool:
    if loader_type in MODDED_LOADERS:
        # Forge and NeoForge are done when Crafty has replaced the installer
        # command, which it only does after the install actually succeeded.
        return bool(command) and INSTALLER_MARKER not in command
    # Fabric, vanilla, Paper and friends run the downloaded jar directly.
    return bool(executable) and (executable in names
                                 or any(n.endswith(".jar") for n in names))


async def _wait_for_loader(job: Job, server_id: str, plan: PackPlan) -> None:
    """Block until Crafty has finished installing the loader -- or fix it.

    Crafty downloads the loader jar on a daemon thread and, when that download
    fails, logs a single line to its own console and stops. Nothing reaches the
    API: the create returned 201, the record still names an executable, and the
    server directory holds nothing but eula.txt and server.properties. Waiting
    politely for that instance means waiting forever, which is exactly what
    this function used to do.

    So: watch for the jar, and if it never arrives, fetch it from the same
    index Crafty uses and finish the install ourselves.
    """
    job.set_step("Waiting for Crafty to install the loader", 60)
    deadline = time.time() + config.SERVER_READY_TIMEOUT
    grace_until = time.time() + LOADER_DOWNLOAD_GRACE
    last_sig: dict[str, str] = {}
    last_change = time.time()
    repaired = False
    announced = False

    while time.time() < deadline:
        try:
            entries = await crafty.list_dir(server_id, ".")
            names = {k for k in entries if k != "root_path"}
            server = await crafty.get_server(server_id)
            executable = server.get("executable") or ""
            command = server.get("execution_command") or ""

            if _loader_installed(plan.crafty_loader, command, names, executable):
                job.log_line(f"Loader install complete ({len(names)} entries)")
                return

            jar_here = any(n.endswith(".jar") for n in names)
            if jar_here and not announced:
                announced = True
                job.set_step("Crafty is installing the loader", 62)

            # Names plus mtimes: a jar still being written moves, an abandoned
            # one does not.
            sig = {n: str((entries.get(n) or {}).get("modified", "")) for n in names}
            if sig != last_sig:
                last_sig = sig
                last_change = time.time()

            stalled = time.time() - last_change > LOADER_STALL
            # Either the download never started, or it started and stopped
            # without finishing. Both mean the loader is not coming.
            if (not jar_here and time.time() > grace_until) or stalled:
                if repaired:
                    raise RuntimeError(
                        "Crafty could not download the "
                        f"{plan.crafty_loader} jar for {plan.mc_version}, and "
                        "installing it directly did not work either. Its jar "
                        "mirror (jars.arcadiatech.org) may be down -- try again "
                        "in a few minutes."
                    )
                repaired = True
                await _repair_loader_jar(job, server_id, plan, executable)
                grace_until = time.time() + LOADER_DOWNLOAD_GRACE
                last_change = time.time()
                last_sig = {}
                continue
        except crafty.CraftyError:
            pass
        await asyncio.sleep(5)

    raise RuntimeError(
        f"Crafty never finished installing {plan.crafty_loader} for "
        f"{plan.mc_version} (waited {config.SERVER_READY_TIMEOUT}s). The "
        "instance exists but has no launcher; delete it in Crafty and retry."
    )


async def _repair_loader_jar(
    job: Job, server_id: str, plan: PackPlan, executable: str
) -> None:
    """Supply the loader jar Crafty failed to download, then install it.

    Uses Crafty's own jar index, so the file is byte-for-byte what Crafty
    would have fetched, sha256 included.
    """
    job.log_line(
        "Crafty's loader download failed (its jar mirror did not answer). "
        "Fetching the jar directly instead.",
        "warn",
    )
    job.set_step("Fetching the loader jar Crafty could not", 61)

    catalog = await crafty.jar_catalog()
    src = crafty.jar_source(catalog, "mc_java_servers", plan.crafty_loader,
                            plan.mc_version)
    if not src:
        raise RuntimeError(
            f"Crafty's jar index has no {plan.crafty_loader} build for "
            f"Minecraft {plan.mc_version}."
        )

    async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
        payload = await _get_with_retries(client, src["url"])

    if src.get("sha256"):
        digest = hashlib.sha256(payload).hexdigest()
        if digest != src["sha256"]:
            raise RuntimeError(
                "The loader jar downloaded from Crafty's mirror is corrupt "
                f"(sha256 {digest[:12]}… expected {src['sha256'][:12]}…)."
            )

    name = executable or f"{plan.crafty_loader}-{plan.mc_version}.jar"
    name = name.split("/")[-1]
    await crafty.upload_file(server_id, ".", name, payload)
    job.log_line(f"Uploaded {name} ({len(payload) / 1048576:.1f} MB)")

    if plan.crafty_loader in MODDED_LOADERS:
        await _run_loader_installer(job, server_id, plan)


async def _get_with_retries(client: httpx.AsyncClient, url: str,
                            attempts: int = 4) -> bytes:
    last: Exception | None = None
    for i in range(attempts):
        try:
            r = await client.get(url)
            r.raise_for_status()
            return r.content
        except Exception as e:      # noqa: BLE001 -- retried, then re-raised
            last = e
            await asyncio.sleep(2 ** i)
    raise RuntimeError(f"Could not download {url}: {last}")


async def _run_loader_installer(job: Job, server_id: str, plan: PackPlan) -> None:
    """Run the loader installer the way Crafty would have, then fix the command.

    Crafty's create call already left the instance pointing at
    `-jar <installer>.jar --installServer`, so starting the server *is* running
    the installer. What Crafty normally does afterwards -- rewrite `executable`
    and `execution_command` to the real launch line -- lives on the thread that
    died with the download, so it has to be done here.
    """
    job.set_step("Running the loader installer", 63)
    await crafty.server_action(server_id, "start_server")

    deadline = time.time() + min(900, config.SERVER_READY_TIMEOUT)
    while time.time() < deadline:
        await asyncio.sleep(5)
        try:
            entries = await crafty.list_dir(server_id, ".")
            names = {k for k in entries if k != "root_path"}
        except crafty.CraftyError:
            continue
        if "libraries" in names and ("run.sh" in names or "run.bat" in names):
            await asyncio.sleep(5)      # let the installer flush its last writes
            break
    else:
        raise RuntimeError(
            "The loader installer did not finish. Check the instance's console "
            "in Crafty for what it reported."
        )

    await _rewrite_modded_command(job, server_id, plan)


# Mirrors Crafty's own post-install rewrite (app/classes/installers/modded.py):
# read the run script the installer generated and turn it into the launch
# command. Getting this wrong means the next start re-runs the installer
# instead of the server.
_RUN_SCRIPT = re.compile(
    r"java @([a-zA-Z0-9_.]+) @([a-z./\-]+)([0-9.\-]+(?:-[a-zA-Z0-9]+)?)/([a-z_0-9]+\.txt)"
)


async def _rewrite_modded_command(job: Job, server_id: str, plan: PackPlan) -> None:
    script = ""
    for candidate in ("run.sh", "run.bat"):
        try:
            script = await crafty.read_file(server_id, candidate)
            if script:
                break
        except crafty.CraftyError:
            continue

    match = _RUN_SCRIPT.search(script or "")
    if not match:
        job.log_line(
            "The loader installed, but its run script could not be parsed -- "
            "the instance may need its launch command set by hand in Crafty.",
            "warn",
        )
        return

    args_file, lib_path, version, txt = match.groups()
    exec_path = f"{lib_path}{version}/"
    loader = "neoforge" if "neoforge" in plan.crafty_loader else "forge"
    await crafty.patch_server(server_id, {
        "executable": f"{exec_path}{loader}-{version}-server.jar",
        "execution_command": f"java @{args_file} @{exec_path}{txt} nogui",
    })
    job.log_line(f"Launch command set for {loader} {version}")


async def _apply_server_settings(
    job: Job, server_id: str, plan: PackPlan, mem_max_gb: int, motd: str | None,
    port: int | None = None,
) -> None:
    """EULA, RAM, the port, and a few server.properties niceties."""
    try:
        await crafty.write_file(server_id, "eula.txt", crafty.EULA_ACCEPTED)
    except Exception:
        job.log_line("Could not write eula.txt", "warn")

    # Crafty's crash watcher stats this directory when a server starts and
    # raises if it is missing, so make sure both exist up front.
    for d in ("crash-reports", "logs"):
        try:
            await crafty.ensure_dir(server_id, d)
        except Exception:
            pass

    # Forge/NeoForge read JVM args from user_jvm_args.txt.
    if plan.loader in ("forge", "neoforge"):
        args = (
            f"# Generated by Crafty Modpack Studio\n"
            f"-Xms{max(1, mem_max_gb // 2)}G\n"
            f"-Xmx{mem_max_gb}G\n"
        )
        try:
            await crafty.write_file(server_id, "user_jvm_args.txt", args)
        except Exception:
            job.log_line("Could not write user_jvm_args.txt", "warn")

    # The port the user typed at the install step has to be written HERE,
    # after the pack files have landed. Crafty is told the port at creation
    # time and writes it into a fresh server.properties, but the pack's own
    # overrides are laid over the top afterwards -- and a modpack that ships
    # a server.properties (most of them do) puts 25565 straight back. That is
    # why every instance ended up on 25565 no matter what was typed.
    #
    # Written through properties.set_port so Crafty's record and
    # server.properties can never disagree; one is what Minecraft binds, the
    # other is what Crafty polls for status.
    if port:
        try:
            result = await properties.set_port(server_id, port, force=True)
            job.log_line(f"Port set to {port} in server.properties and Crafty")
            for w in result.get("warnings", []):
                job.log_line(w, "warn")
        except Exception as e:
            job.log_line(f"Could not set the port to {port}: {e}", "warn")

    if motd:
        try:
            props = await crafty.read_file(server_id, "server.properties")
            lines = []
            seen = False
            for line in props.splitlines():
                if line.startswith("motd="):
                    lines.append(f"motd={motd}")
                    seen = True
                else:
                    lines.append(line)
            if not seen:
                lines.append(f"motd={motd}")
            await crafty.write_file(server_id, "server.properties", "\n".join(lines) + "\n")
        except Exception:
            job.log_line("Could not update server.properties", "warn")


async def install_modpack(
    job: Job,
    *,
    mod_id: int | None = None,
    file_id: int | None = None,
    upload_id: str | None = None,
    server_name: str,
    port: int,
    mem_min: int | None = None,
    mem_max: int | None = None,
    prefer_server_pack: bool = True,
    skip_client_only: bool = True,
    motd: str | None = None,
    existing_server_id: str | None = None,
    exclude_files: list[str] | None = None,
    disable_files: list[str] | None = None,
    client_reasons: dict[str, list[str]] | None = None,
    optimize: bool = True,
) -> dict:
    """Install a modpack into a new (or existing) Crafty instance.

    Takes either CurseForge ids or the id of an imported archive; the two
    differ only in where the zip comes from.
    """
    plan, zf, file_meta = await _load_plan(
        job, mod_id=mod_id, file_id=file_id, upload_id=upload_id,
        prefer_server_pack=prefer_server_pack,
    )
    excluded = set(exclude_files) if exclude_files is not None else None
    to_disable = set(disable_files) if disable_files is not None else None
    if to_disable is not None:
        job.log_line(
            f"Using your review decisions: {len(to_disable)} client-only mods "
            "will be installed but left disabled"
        )
    if excluded:
        job.log_line(f"{len(excluded)} mods will not be installed at all")

    if not plan.mc_version:
        raise RuntimeError(
            "Could not determine the Minecraft version for this pack. "
            "It may be an unusual archive layout."
        )
    if not plan.crafty_loader:
        raise RuntimeError(
            f"Loader '{plan.loader or 'unknown'}' is not available in Crafty's "
            "jar catalog. Fabric, Forge and NeoForge packs are supported."
        )
    for w in plan.warnings:
        job.log_line(w, "warn")

    ram_min, ram_max = packs.mem_from_ram_hint(
        plan.recommended_ram, config.DEFAULT_MEM_MIN, config.DEFAULT_MEM_MAX
    )
    ram_min = mem_min or ram_min
    ram_max = mem_max or ram_max

    # --- create the instance ------------------------------------------
    if existing_server_id:
        server_id = existing_server_id
        job.set_instance(server_id, server_name)
        job.log_line(f"Installing into existing instance {server_id}")
    else:
        job.set_step(
            f"Creating Crafty instance ({plan.crafty_loader} {plan.mc_version})", 55
        )
        server_id = await crafty.create_server(
            name=server_name,
            loader_type=plan.crafty_loader,
            mc_version=plan.mc_version,
            mem_min=ram_min,
            mem_max=ram_max,
            port=port,
        )
        job.log_line(f"Created instance {server_id}")
        job.set_instance(server_id, server_name)
        # Stamp the instance as soon as it exists, marked unfinished. If the
        # install dies after this point the instance is left behind on
        # purpose (so it can be retried into), and this is what lets the
        # fleet list say "half-finished" rather than showing it as a healthy
        # server with no mods.
        try:
            await crafty.write_studio_manifest(server_id, {
                "schema": 1,
                "complete": False,
                "started_at": time.time(),
                "pack": {"name": plan.name},
                "minecraft": plan.mc_version,
                "loader": plan.loader,
            })
        except Exception:
            pass
        job.emit("server_created", server_id, server_id=server_id)
        await _wait_for_loader(job, server_id, plan)

    # Crafty installs the catalog's newest loader build for that Minecraft
    # version, which may not be the exact build the pack was authored
    # against. That is normally fine within a major line, but worth saying.
    installed_note = ""
    if plan.loader_version:
        installed_note = (
            f"Pack targets {plan.loader} {plan.loader_version}; Crafty installs "
            f"its catalogue build for {plan.mc_version}."
        )
        job.log_line(installed_note)

    # --- gather the payload -------------------------------------------
    records: list[dict] = []
    problems: list[dict] = []
    payload: list[PackEntry] = []

    matched_exclusions: set[str] = set()
    disabled_mods: list[str] = []
    if plan.source == "manifest":
        mods, records, problems, matched, disabled = await _download_manifest_mods(
            job, plan, skip_client_only, excluded, to_disable, client_reasons
        )
        matched_exclusions.update(matched)
        disabled_mods.extend(disabled)
        for entry in mods:
            entry.target = f"mods/{entry.target}"
        payload.extend(mods)

    job.set_step("Preparing pack files", 58)
    overlay_dropped = 0
    reasons_by_file = client_reasons or {}
    for entry in plan.overlay_members:
        target = entry["target"]
        base = posixpath.basename(target)
        is_mod_jar = target.startswith("mods/") and target.endswith(".jar")
        if excluded is not None and is_mod_jar and base in excluded:
            overlay_dropped += 1
            matched_exclusions.add(base)
            continue

        client_only, why = False, []
        if is_mod_jar:
            if to_disable is not None and base in to_disable:
                client_only = True
                matched_exclusions.add(base)
                why = reasons_by_file.get(base) or [
                    "flagged as client-only in the pre-install review"
                ]
            elif to_disable is None and skip_client_only:
                # No review to defer to: judge the jar on its own metadata,
                # then on its name. Either way it is disabled, not deleted.
                # Only mod jars are read here, and only one at a time --
                # everything else in the overlay (configs, a world folder,
                # resource packs) is never materialised at all.
                try:
                    probe = zf.read(entry["member"])
                except Exception as e:
                    problems.append(
                        {"name": target, "reason": f"unreadable in archive: {e}"})
                    continue
                side = jarmeta.parse(probe, target).get("side")
                del probe
                if side == "client":
                    client_only, why = True, ["the jar declares environment=client"]
                elif packs.is_client_only_jar(target):
                    client_only = True
                    why = ["name matches a known client-only mod"]

        try:
            declared = zf.getinfo(entry["member"]).file_size
        except KeyError:
            problems.append({"name": target, "reason": "missing from the archive"})
            continue
        payload.append(PackEntry(
            target + (".disabled" if client_only else ""),
            member=entry["member"], size=declared,
        ))
        if is_mod_jar:
            if client_only:
                disabled_mods.append(base)
            records.append({
                # A jar in a client export's overrides/ was added by hand and
                # is in no catalogue, so it can never be version-checked --
                # saying where it came from is what stops the Mods tab
                # claiming it is simply "unidentified".
                "file": target,
                "source": "bundled" if plan.source == "manifest" else "server_pack",
                "name": posixpath.basename(target),
                # Dropped again by _identify_overlay once it has read the jar.
                "member": entry["member"],
                "client_only": client_only,
                "client_only_reasons": why,
            })
    if overlay_dropped:
        job.log_line(f"Removed {overlay_dropped} jars from the pack as requested",
                     "warn")

    # An exclusion that matches nothing means the caller and the pack disagree
    # about what is in it -- a review answered against a different version, or
    # a hand-made API call. Silently installing the mod anyway is the one
    # outcome nobody wants, so say it plainly.
    decided = (excluded or set()) | (to_disable or set())
    if decided:
        unmatched = decided - matched_exclusions
        if unmatched:
            job.log_line(
                f"{len(unmatched)} exclusion(s) matched no file in this pack and "
                "had no effect: "
                + ", ".join(sorted(unmatched)[:5])
                + ("..." if len(unmatched) > 5 else ""),
                "warn",
            )

    # Identify the overlay's jars now, while they are in a local archive.
    # Doing it here rather than leaving it to a later Identify pass means the
    # Mods list has real names and icons from the first time it is opened, and
    # nothing has to be downloaded back out of Crafty to find that out.
    await _identify_overlay(job, zf, records)

    total_mb = sum(e.size for e in payload) / 1048576
    uploaded_count = len(payload)
    job.log_line(f"Uploading {uploaded_count} files ({total_mb:.0f} MB) to the instance")

    await _push_batches(job, server_id, payload, 60, 92, "Uploading pack files",
                        source=zf)
    # Entries are references, not bytes, but dropping them still releases the
    # archive's member table for a pack with thousands of files. Counted
    # first: the summary is built much further down.
    payload.clear()

    # --- finishing touches --------------------------------------------
    job.set_step("Applying server settings", 94)
    await _apply_server_settings(job, server_id, plan, ram_max, motd, port)

    # Size the heap against the host, not just the pack's wishlist: a pack
    # asking for 8 GB on a box with 5 GB free dies at startup with no log.
    # Set when the loader keeps its JVM args in Crafty's launch command
    # rather than in a file; applied after the Java selection below, which
    # rewrites that same command and would otherwise undo it.
    pending_heap: tuple | None = None
    if optimize:
        try:
            host = specs.effective_host()
            memory = specs.recommend_memory(
                pack_recommended_mb=plan.recommended_ram,
                mod_count=len([r for r in records
                               if r.get("file", "").endswith(".jar")]),
                host=host,
            )
            heap = mem_max or memory["heap_gb"]
            flags = [f["flag"] for f in specs.build_flag_plan(
                heap_gb=heap, host=host, mc_version=plan.mc_version,
                loader=plan.loader) if f["enabled"]]
            if plan.loader in ("forge", "neoforge"):
                await crafty.write_file(
                    server_id, "user_jvm_args.txt",
                    specs.render_jvm_args(flags, heap),
                )
                job.log_line(
                    f"Tuned JVM: {heap:g} GB heap, {len(flags)} flags "
                    f"(host has {host.get('total_ram_gb')} GB, "
                    f"{host.get('cpu_count')} CPUs)"
                )
            else:
                # Fabric, vanilla, Paper: no user_jvm_args.txt exists, so the
                # heap lives in Crafty's launch command. This used to be
                # computed and then thrown away, leaving the instance on
                # whatever the pack asked for -- a pack requesting 6 GB got
                # 6 GB on a host with 11 GB total and Minecraft servers
                # already running on it.
                pending_heap = (heap, flags, host)
        except Exception as e:
            job.log_line(f"Could not apply performance tuning: {e}", "warn")

    # The host may default to a Java newer than the loader supports, which
    # kills the server at startup with no log at all. Pin it explicitly.
    try:
        java = await crafty.set_java_version(server_id, plan.mc_version)
        if plan.java_version and java.get("java_major") and (
            java["java_major"] != plan.java_version
        ):
            job.log_line(
                f"Note: the export was built against Java {plan.java_version}; "
                f"this instance runs Java {java['java_major']}, which is what "
                f"Minecraft {plan.mc_version} requires.",
            )
        if java.get("changed"):
            job.log_line(
                f"Set Java {java['java_major']} for this instance "
                f"({java['java_path']})"
            )
        elif java.get("reason") and "already" not in java["reason"]:
            job.log_line(f"Java selection: {java['reason']}", "warn")
    except Exception as e:
        job.log_line(f"Could not set the Java version: {e}", "warn")

    if pending_heap:
        heap, flags, host = pending_heap
        try:
            await optimizer.set_command_memory(server_id, heap, flags)
            job.log_line(
                f"Tuned JVM: {heap:g} GB heap, {len(flags)} flags "
                f"(host has {host.get('total_ram_gb')} GB, "
                f"{host.get('cpu_count')} CPUs)"
            )
        except Exception as e:
            job.log_line(f"Could not set the heap size: {e}", "warn")

    job.set_step("Recording install manifest", 97)
    manifest = {
        "schema": 1,
        # The counterpart to the stub written at creation: reaching here is
        # what makes an instance finished.
        "complete": True,
        "installed_at": time.time(),
        "pack": {
            "source": "upload" if upload_id else "curseforge",
            "project_id": mod_id,
            "file_id": file_id,
            # Kept so the Modpack tab can offer "re-import an updated export"
            # against the same archive, and so a later import can tell it is
            # updating this pack rather than replacing it with another.
            "upload_id": upload_id,
            "archive": file_meta.get("file_name") if upload_id else None,
            "name": plan.name,
            # An export usually carries no version of its own, and putting the
            # zip filename in this field would push it into every pack pill in
            # the UI. The archive name is recorded above instead.
            "version": plan.version or (
                "" if upload_id else file_meta.get("display_name")
            ),
            "install_source": plan.source,
        },
        "minecraft": plan.mc_version,
        "loader": plan.loader,
        "loader_version": plan.loader_version,
        "crafty_loader": plan.crafty_loader,
        # Kept so the Optimize tab can size the heap against what the pack
        # actually asked for, rather than guessing from the mod count.
        "recommended_ram_mb": plan.recommended_ram,
        "java_version": plan.java_version,
        "excluded_mods": sorted(excluded) if excluded else [],
        "disabled_mods": sorted(set(disabled_mods)),
        "mods": records,
        "problems": problems,
    }
    try:
        await crafty.write_studio_manifest(server_id, manifest)
    except Exception as e:
        job.log_line(f"Could not write install manifest: {e}", "warn")

    summary = {
        "server_id": server_id,
        "name": server_name,
        "pack": plan.name,
        "version": plan.version,
        "minecraft": plan.mc_version,
        "loader": plan.loader,
        "loader_version": plan.loader_version,
        "install_source": plan.source,
        "mods_installed": len([r for r in records if r.get("file", "").endswith(".jar")]),
        "client_only_disabled": sorted(set(disabled_mods)),
        "files_uploaded": uploaded_count,
        "port": port,
        "problems": problems,
        "note": installed_note,
    }
    job.log_line(
        f"Done: {summary['mods_installed']} mods installed into '{server_name}'"
        + (f", {len(summary['client_only_disabled'])} left disabled as client-only"
           if summary["client_only_disabled"] else "")
    )

    # An install is the moment the cache has just grown, and the one moment
    # nothing else is reading it. Failing to prune must never fail an install
    # that otherwise worked.
    try:
        result = cache.prune()
        if result["pruned"]:
            job.log_line(
                f"Cache trimmed: {result['pruned']} old files removed, "
                f"{result['freed'] / 1024**3:.1f} GB freed")
    except Exception as e:
        job.log_line(f"Could not trim the download cache: {e}", "warn")

    return summary


async def _identify_overlay(job: Job, zf, records: list[dict]) -> None:
    """Match a server pack's jars to catalogue projects, from the archive.

    A server pack lists no project ids, so without this every jar in it is
    "unidentified" until somebody presses Identify -- which then downloads all
    of them back out of Crafty to learn what the archive already knew.

    Two bulk calls for the whole pack: CurseForge by its own fingerprint, then
    Modrinth by SHA-1 for whatever is left.
    """
    todo = [r for r in records if not r.get("project_id") and r.get("member")]
    if not todo:
        return
    job.set_step(f"Identifying {len(todo)} jars", 58)

    by_fp: dict[int, dict] = {}
    by_sha: dict[str, dict] = {}
    for r in todo:
        try:
            blob = zf.read(r["member"])
        except Exception:
            continue
        try:
            by_fp[curseforge.fingerprint(blob)] = r
        except Exception:
            pass
        try:
            by_sha[modrinth.sha1(blob)] = r
        except Exception:
            pass
        del blob

    matched = 0
    try:
        for fp, match in (await curseforge.match_fingerprints(list(by_fp))).items():
            r = by_fp.get(fp)
            if not r:
                continue
            r.update(source="curseforge", project_id=match["mod_id"],
                     file_id=(match.get("file") or {}).get("file_id"),
                     version=(match.get("file") or {}).get("display_name"),
                     identified_at=time.time())
            matched += 1
    except Exception as e:
        job.log_line(f"CurseForge fingerprint lookup failed: {e}", "warn")

    left = {h: r for h, r in by_sha.items() if not r.get("project_id")}
    if left and config.MODRINTH_ENABLED:
        try:
            for h, version in (await modrinth.versions_from_hashes(list(left))).items():
                r = left.get(h)
                if not r:
                    continue
                r.update(source="modrinth", project_id=version.get("mod_id"),
                         file_id=version.get("file_id"),
                         version=version.get("version_number"),
                         identified_at=time.time())
                matched += 1
        except Exception as e:
            job.log_line(f"Modrinth hash lookup failed: {e}", "warn")

    # Names and icons for whatever was matched.
    cf_ids = [r["project_id"] for r in records
              if r.get("source") == "curseforge" and r.get("project_id")]
    mr_ids = [r["project_id"] for r in records
              if r.get("source") == "modrinth" and r.get("project_id")]
    try:
        cf = await curseforge.get_mods(cf_ids) if cf_ids else {}
        mr = await modrinth.get_projects(mr_ids) if mr_ids else {}
    except Exception:
        cf, mr = {}, {}
    for r in records:
        proj = (cf if r.get("source") == "curseforge" else mr).get(r.get("project_id"))
        if proj:
            r["name"] = proj.get("name") or r.get("name")
            r["logo"] = proj.get("logo")
            r.setdefault("summary", proj.get("summary"))
        r.pop("member", None)

    job.log_line(f"Identified {matched} of {len(todo)} pack jars against the catalogues")


async def switch_pack_version(
    job: Job,
    *,
    server_id: str,
    mod_id: int | None = None,
    file_id: int | None = None,
    upload_id: str | None = None,
    prefer_server_pack: bool = True,
    skip_client_only: bool = True,
    keep_world: bool = True,
    exclude_files: list[str] | None = None,
    disable_files: list[str] | None = None,
    client_reasons: dict[str, list[str]] | None = None,
) -> dict:
    """Move an existing instance to a different version of its modpack.

    Mods and config are replaced wholesale; the world is left alone by
    default because that is almost always what people want.

    A private pack has no release list to move between, so for those this is
    the re-import path instead: export the profile again from the CurseForge
    app, upload it, and the instance is rebuilt from the new archive with the
    world intact.
    """
    job.set_step("Reading current install", 2)
    current = await crafty.read_studio_manifest(server_id)
    server = await crafty.get_server(server_id)

    job.log_line(
        f"Current: {(current.get('pack') or {}).get('name','unknown')} "
        f"{(current.get('pack') or {}).get('version','')}"
    )

    # Clear out the old mods so removed ones do not linger and crash startup.
    job.set_step("Removing previous mods", 8)
    try:
        entries = await crafty.list_dir(server_id, "mods")
        stale = [
            f"mods/{n}" for n, meta in entries.items()
            if n != "root_path" and isinstance(meta, dict) and not meta.get("dir")
        ]
        if stale:
            await crafty.delete_paths(server_id, stale)
            job.log_line(f"Removed {len(stale)} old mod files")
    except crafty.CraftyError:
        job.log_line("No existing mods directory", "warn")

    if not keep_world:
        job.log_line("keep_world=false: the world folder will be replaced if the "
                     "pack ships one")

    result = await install_modpack(
        job,
        mod_id=mod_id,
        file_id=file_id,
        upload_id=upload_id,
        server_name=server.get("server_name", "server"),
        port=server.get("server_port", 25565),
        prefer_server_pack=prefer_server_pack,
        skip_client_only=skip_client_only,
        existing_server_id=server_id,
        exclude_files=exclude_files,
        disable_files=disable_files,
        client_reasons=client_reasons,
    )
    result["previous"] = current.get("pack")
    return result
