"""The install pipeline: modpack -> a server that starts.

A pack reaches this module from one of two places, and everything after step
1 is identical for both:

  * the **CurseForge catalogue**, addressed by (mod_id, file_id); or
  * an **imported archive** the user uploaded, addressed by upload_id --
    a profile export from the CurseForge app, which has no catalogue ids
    because it was never published.

Flow, whichever archive shape we get:

  1. Fetch and analyse the pack archive (server pack preferred, client
     manifest as fallback) to learn loader + Minecraft version.
  2. Create the server and install the loader the pack names -- the exact
     build when its project still publishes it -- through the ladder in
     app/provision.py, which also reads the Forge/NeoForge launch arguments
     back out of what the installer wrote. Those arguments are the single
     most error-prone part of a modded server, so they are read, not typed.
  3. Lay the pack's files over the top in bounded-size batches (zip ->
     upload -> the node extracts), so memory stays flat regardless of pack
     size.
  4. Record exactly what was installed in .blessforge.json for later
     version switching, update checks and diagnostics.
"""
from __future__ import annotations

import asyncio
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

from app import (blocked, cache, config, engine, curseforge, jarmeta, modrinth,
                 optimizer, packs, properties, provision, uploads)
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
# batch occupies and how often we round-trip to the node -- so it can be
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

    # Streamed, not buffered. The docstring above has always said "deliberately
    # a path, not bytes", but the body used to fetch the whole archive into
    # memory and then write it out -- which needs the archive's size twice over
    # and, at 617 MB for Tensura Evolutions' server pack, walked straight into
    # the container's 1 GB cap. The process was SIGKILLed with no traceback,
    # the in-memory job registry went with it, and the install simply vanished
    # from the drawer.
    def progress(got: int) -> None:
        if expected:
            job.set_step(
                f"Downloading server pack ({got // 1048576} of "
                f"{expected // 1048576} MB)", 5 + 3 * min(got / expected, 1.0))

    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        await curseforge.download_to(meta, path, on_progress=progress)
    except OSError as e:
        job.log_line(f"Could not cache the archive: {e}", "warn")
        fallback = Path(tempfile.gettempdir()) / path.name
        await curseforge.download_to(meta, fallback, on_progress=progress)
        path = fallback
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
    host = await specs.host_for()
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
        "server_loader": plan.server_loader,
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
                if not (plan.mc_version and plan.server_loader):
                    await _fill_from_client_manifest(job, plan, mod_id, file_id)
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


async def _fill_from_client_manifest(job: Job, plan: PackPlan, mod_id: int,
                                     file_id: int) -> None:
    """Complete a server pack's plan from its client file's manifest.json.

    Plenty of server packs are just the folder -- mods, config, a start
    script -- with nothing that names the loader or the Minecraft version.
    The client file of the same release always says both (CurseForge cannot
    import it otherwise), and it is a few hundred KB, so it is read for
    exactly the fields the server pack left blank. The server pack's own
    files are still what gets installed.
    """
    try:
        archive, _ = await fetch_pack_archive(job, mod_id, file_id)
        client = packs.analyse_client_pack(zipfile.ZipFile(archive))
    except Exception as e:      # noqa: BLE001 -- the caller reports a plan still blank
        job.log_line(f"Could not read the pack's own manifest either ({e})", "warn")
        return
    filled = []
    for name in ("mc_version", "loader", "loader_version", "server_loader",
                 "recommended_ram", "java_version"):
        if not getattr(plan, name) and getattr(client, name):
            setattr(plan, name, getattr(client, name))
            filled.append(name)
    if filled:
        job.log_line(
            "The server pack does not say which loader or version it is for; "
            f"read {', '.join(filled)} from the pack's manifest "
            f"({plan.loader} {plan.loader_version}, Minecraft {plan.mc_version})")


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
) -> tuple[list[PackEntry], list[dict], list[dict], list[str], list[str],
           list[dict]]:
    """Resolve and fetch every mod the manifest lists.

    Returns (files, records, problems, matched, disabled, blocked).

    `blocked` is the files whose authors turned off third-party
    distribution and that Modrinth does not carry either. They are never
    downloaded -- see app/blocked.py -- and the operator is asked to upload
    them by hand.

    Client-only mods are installed *disabled* rather than dropped. Stripping
    them made the instance a lie: the pack no longer matched what the user
    exported, nothing on the Mods tab explained where a mod had gone, and
    re-enabling one meant finding and downloading it by hand. A jar written
    as `<name>.jar.disabled` is inert to every loader -- the same convention
    the CurseForge app and every server panel use -- so the server still
    boots clean, but
    the mod is present, labelled, and one click from coming back.
    """
    entries = plan.manifest_files
    file_ids = [e["fileID"] for e in entries if e.get("fileID")]
    job.set_step(f"Resolving {len(file_ids)} mod files", 12)
    file_meta = await curseforge.get_files(file_ids)

    mod_ids = {m.get("mod_id") for m in file_meta.values() if m.get("mod_id")}
    projects = await curseforge.get_mods(mod_ids) if mod_ids else {}

    # Every blocked file is settled up front, in bulk: one hash lookup for
    # the lot, then a slug lookup only for what that did not find.
    substitutes, blocked_list = await blocked.substitutes(
        list(file_meta.values()), projects,
        mc_version=plan.mc_version or "", loader=plan.loader or "")
    blocked_ids = {b.get("file_id") for b in blocked_list}
    if substitutes:
        job.log_line(
            f"{len(substitutes)} mod(s) block third-party downloads on "
            "CurseForge and will be fetched from Modrinth instead")

    results: list[PackEntry] = []
    records: list[dict] = []
    problems: list[dict] = []
    matched_names: list[str] = []
    disabled_client: list[str] = []
    wrong_loader_mods: list[str] = []
    switched_off: list[str] = []
    blocked_out: list[dict] = []
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

                    # The author turned off third-party downloads and Modrinth
                    # does not have it either: listed, never fetched.
                    if fid in blocked_ids:
                        blocked_out.append({
                            **next(b for b in blocked_list
                                   if b.get("file_id") == fid),
                            "required": entry.get("required", True),
                        })
                        return
                    sub = substitutes.get(fid)
                    disk_name = (sub or {}).get("file_name") or fname
                    if sub:
                        cached = None
                        blob = await modrinth.download(sub)
                    else:
                        # Reuses whatever the review step already downloaded,
                        # and keeps it on disk: holding 300 jars in memory
                        # until the last batch uploads is what made this
                        # app's footprint scale with pack size.
                        cached = await curseforge.cache_jar(meta, client)
                        blob = None if cached else await curseforge.download_cached(
                            meta, client)

                    # With no review to go on, judge the jar on the same
                    # evidence the review would have used. It used to be a
                    # two-line fallback (environment=client, then the name
                    # list), which meant an unreviewed install and a reviewed
                    # one could disagree about the same jar.
                    if not client_only and disable_files is None and skip_client_only:
                        probe = blob if blob is not None else cached.read_bytes()
                        verdict = preflight_mod.quick_verdict(fname, probe)
                        del probe
                        if verdict["verdict"] == "client":
                            client_only = True
                            why = verdict.get("reasons") or [
                                "judged client-only from the jar's contents"]

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

                    # "required": false is how a CurseForge export records a
                    # mod its owner switched OFF in their profile. Installing it
                    # enabled is installing a mod the pack does not run --
                    # 2BROKE4LORE marks Ars Elemental so because Ars Nouveau is
                    # not in the pack, and enabling it crashed the server.
                    switched_off_here = not entry.get("required", True)
                    if switched_off_here:
                        switched_off.append(disk_name)
                    target = disk_name + (
                        ".disabled" if client_only or wrong_loader or switched_off_here
                        else "")
                    if client_only:
                        disabled_client.append(disk_name)
                    if wrong_loader:
                        wrong_loader_mods.append(disk_name)
                        # Rides the same "installed but disabled" list so the
                        # Mods tab, the manifest and the summary all already
                        # know how to show it.
                        disabled_client.append(disk_name)
                    results.append(PackEntry(
                        target, path=cached, blob=blob,
                        size=(cached.stat().st_size if cached else len(blob or b"")),
                    ))
                    origin = ({
                        "source": "modrinth",
                        "project_id": sub.get("mod_id"),
                        "file_id": sub.get("file_id"),
                        "version": sub.get("version_number"),
                        "substituted_for": {
                            "source": "curseforge", "project_id": pid,
                            "file_id": fid, "file_name": fname,
                            "via": sub.get("substitute_via"),
                            "why": blocked.substitute_note(sub),
                        },
                    } if sub else {
                        "source": "curseforge",
                        "project_id": pid,
                        "file_id": fid,
                        "version": meta.get("display_name"),
                    })
                    records.append({
                        # Recorded under the enabled name whatever its state
                        # on disk, so toggling a mod never orphans its record.
                        "file": f"mods/{disk_name}",
                        **origin,
                        "name": project.get("name") or fname,
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
                        "switched_off_in_pack": switched_off_here,
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

    # Sinytra Connector loads Fabric mods on Forge and NeoForge. Which jars a
    # pack ships is only known once they are all in, so the Fabric jars set
    # aside as wrong-loader are put back here when Connector is among them.
    back = connector_pass(plan.loader, records, results)
    if back:
        wrong_loader_mods = [m for m in wrong_loader_mods if m not in back]
        disabled_client = [m for m in disabled_client if m not in back]
        job.log_line(f"Sinytra Connector is in the pack: {len(back)} Fabric mod(s) it "
                     "runs on this server stay enabled.")

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
    if switched_off:
        job.log_line(
            f"{len(switched_off)} mod(s) are switched off in the pack itself and were "
            "installed disabled, as the pack has them: " + ", ".join(sorted(switched_off)[:8])
            + ("..." if len(switched_off) > 8 else ""))
    if problems:
        job.log_line(f"{len(problems)} mods could not be downloaded", "warn")
        for p in problems[:10]:
            job.log_line(f"  - {p['name']}: {p['reason']}", "warn")
    if blocked_out:
        job.log_line(
            f"{len(blocked_out)} mod(s) were not downloaded because their "
            "authors do not allow third-party downloads and Modrinth does not "
            "carry them. Upload them yourself from the Mods tab: "
            + ", ".join(sorted(b.get("name") or "" for b in blocked_out)[:8])
            + ("..." if len(blocked_out) > 8 else ""),
            "warn",
        )
    return (results, records, problems, matched_names, disabled_client,
            blocked_out)


def connector_pass(loader: str, records: list[dict], results: list) -> set[str]:
    """Re-enable the Fabric jars a Forge/NeoForge pack runs through Sinytra
    Connector. Returns the names put back (empty when there is no Connector).
    BMC4 ships BCLib, Better End and Better Nether this way; installed
    disabled as "built for fabric", its datapack lost their world presets
    and the world never loaded."""
    if (loader or "").lower() not in ("forge", "neoforge"):
        return set()
    if not any(jarmeta.is_connector_jar(r.get("file") or "") and not r.get("wrong_loader")
               and not r.get("client_only") and not r.get("switched_off_in_pack")
               for r in records):
        return set()
    back: set[str] = set()
    for rec in records:
        reason = (rec.get("wrong_loader_reason") or "").split(",")[0]
        if rec.get("wrong_loader") and "fabric" in reason \
                and not rec.get("switched_off_in_pack") and not rec.get("client_only"):
            rec["wrong_loader"] = False
            rec["wrong_loader_reason"] = None
            rec["via_connector"] = True
            back.add(posixpath.basename(rec["file"]))
    for entry in results:
        if entry.target.endswith(".disabled") and entry.target[:-9] in back:
            entry.target = entry.target[:-9]
    return back


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
    archive is now written to a scratch file and streamed to the node from
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
        def build(path=local, entries=batch) -> None:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
                for entry in entries:
                    entry.write_into(archive, source)

        try:
            # Decompressing and writing up to 64 MB is real work; on a thread
            # it no longer stalls every other request the API is answering.
            await asyncio.to_thread(build)
            size_mb = local.stat().st_size / 1048576
            pct = start_pct + (end_pct - start_pct) * ((i - 1) / len(batches))
            job.set_step(f"{label} ({i}/{len(batches)}, {size_mb:.0f} MB)", pct)

            await engine.upload_path(server_id, ".", zip_name, local)
        finally:
            # The scratch copy has served its purpose the moment it is sent;
            # leaving it would double the disk cost of every install.
            try:
                local.unlink()
            except OSError:
                pass

        # Extraction is finished when this returns, so the archive can go
        # straight away -- there is no background unzip to wait out.
        await engine.extract(server_id, zip_name)
        try:
            await engine.delete_paths(server_id, [zip_name])
        except Exception:
            job.log_line(f"Could not remove temp archive {zip_name}", "warn")


# The loader ladder -- "the server exists" is not the same as "this server
# can start" -- lives in app/provision.py, because a blank server needs
# exactly the same steps and a second copy of them would drift. This is the
# thin adapter that turns a PackPlan into the arguments it wants.


async def _wait_for_loader(job: Job, server_id: str, plan: PackPlan) -> dict:
    return await provision.ensure_loader(
        job, server_id, family=plan.loader or plan.server_loader,
        mc_version=plan.mc_version, loader_version=plan.loader_version or "",
    )


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


async def _apply_server_settings(
    job: Job, server_id: str, plan: PackPlan, mem_max_gb: int, motd: str | None,
    port: int | None = None,
) -> None:
    """EULA, RAM, the port, and a few server.properties niceties."""
    try:
        await engine.write_file(server_id, "eula.txt", engine.EULA_ACCEPTED)
    except Exception:
        job.log_line("Could not write eula.txt", "warn")

    # Diagnose and the crash attribution look in both; a loader that has
    # never started has created neither, so make sure they exist up front.
    for d in ("crash-reports", "logs"):
        try:
            await engine.ensure_dir(server_id, d)
        except Exception:
            pass

    # Forge/NeoForge read JVM args from user_jvm_args.txt.
    if plan.loader in ("forge", "neoforge"):
        args = (
            f"# Generated by BlessForge\n"
            f"-Xms{max(1, mem_max_gb // 2)}G\n"
            f"-Xmx{mem_max_gb}G\n"
        )
        try:
            await engine.write_file(server_id, "user_jvm_args.txt", args)
        except Exception:
            job.log_line("Could not write user_jvm_args.txt", "warn")

    # The port the user typed at the install step has to be written HERE,
    # after the pack files have landed. The pack's own overrides are laid over
    # the folder, and a modpack that ships a server.properties (most of them
    # do) puts 25565 straight back. That is why every server once ended up on
    # 25565 no matter what was typed.
    #
    # Written through properties.set_port so the server record and
    # server.properties can never disagree; one is what Minecraft binds, the
    # other is what the node publishes and polls for status.
    if port:
        try:
            result = await properties.set_port(server_id, port, force=True)
            job.log_line(f"Port set to {port} in server.properties and the server record")
            for w in result.get("warnings", []):
                job.log_line(w, "warn")
        except Exception as e:
            job.log_line(f"Could not set the port to {port}: {e}", "warn")

    if motd:
        try:
            # Through properties.save, which starts from an empty file on a
            # server that has never run -- Minecraft writes the rest itself.
            await properties.save(server_id, {"motd": motd})
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
    boot_check: bool = True,
) -> dict:
    """Install a modpack into a new (or existing) server.

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
    if not plan.server_loader:
        raise RuntimeError(
            f"Loader '{plan.loader or 'unknown'}' cannot be installed "
            "automatically. Fabric, Forge and NeoForge packs are supported."
        )
    for w in plan.warnings:
        job.log_line(w, "warn")

    ram_min, ram_max = packs.mem_from_ram_hint(
        plan.recommended_ram, config.DEFAULT_MEM_MIN, config.DEFAULT_MEM_MAX
    )
    ram_min = mem_min or ram_min
    ram_max = mem_max or ram_max

    # --- create the instance ------------------------------------------
    installed_loader = ""
    if existing_server_id:
        server_id = existing_server_id
        job.set_instance(server_id, server_name)
        job.log_line(f"Installing into existing instance {server_id}")
    else:
        job.set_step(
            f"Creating the server ({plan.server_loader} {plan.mc_version})", 55
        )
        server_id = await engine.create_server(
            name=server_name,
            port=port,
            memory_mb=specs.container_memory_mb(ram_max),
            mc_version=plan.mc_version,
            loader=plan.server_loader,
        )
        job.log_line(f"Created server {server_id}")
        job.set_instance(server_id, server_name)
        # Stamp the instance as soon as it exists, marked unfinished. If the
        # install dies after this point the instance is left behind on
        # purpose (so it can be retried into), and this is what lets the
        # fleet list say "half-finished" rather than showing it as a healthy
        # server with no mods.
        try:
            await engine.write_manifest(server_id, {
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
        loader = await _wait_for_loader(job, server_id, plan)
        installed_loader = loader.get("loader_version") or ""

    # The exact build the pack names is installed when its project still
    # publishes it. When it does not, the newest build for that Minecraft
    # version stands in -- normally fine within a line, and worth saying.
    installed_note = ""
    if (plan.loader_version and not existing_server_id
            and installed_loader and plan.loader_version not in (
                installed_loader, f"{plan.mc_version}-{installed_loader}")):
        installed_note = (
            f"Pack targets {plan.loader} {plan.loader_version}; that build is "
            f"not published any more, so {installed_loader} was installed."
        )
        job.log_line(installed_note, "warn")

    # --- gather the payload -------------------------------------------
    records: list[dict] = []
    problems: list[dict] = []
    payload: list[PackEntry] = []

    matched_exclusions: set[str] = set()
    disabled_mods: list[str] = []
    blocked_by_author: list[dict] = []
    if plan.source == "manifest":
        (mods, records, problems, matched, disabled,
         blocked_by_author) = await _download_manifest_mods(
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
            elif to_disable is None and skip_client_only and plan.source != "server_pack":
                # A server pack is installed as its author shipped it: a
                # client-looking jar in one is often there because a server
                # mod needs it, and disabling it on a guess broke packs that
                # would have booted. The first boot decides (app/repair.py).
                #
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
    # nothing has to be downloaded back off the node to find that out.
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
    # Set when the loader keeps its JVM args in the startup command rather
    # than in a file; applied last, so nothing after it rewrites the command
    # and undoes it.
    pending_heap: tuple | None = None
    if optimize:
        try:
            host = await specs.host_for(server_id)
            memory = specs.recommend_memory(
                pack_recommended_mb=plan.recommended_ram,
                mod_count=len([r for r in records
                               if r.get("file", "").endswith(".jar")]),
                host=host,
            )
            heap = mem_max or memory["heap_gb"]
            # The container has to hold the heap plus the JVM's own
            # overhead, or the kernel kills it the first time it is busy.
            # Until plans fix a server's size (WP-08), its size follows the
            # heap rather than the other way round.
            mod_count = len([r for r in records if r.get("file", "").endswith(".jar")])
            await engine.update_server(
                server_id, {"memory_mb": specs.container_memory_mb(heap, mod_count)})
            flags = [f["flag"] for f in specs.build_flag_plan(
                heap_gb=heap, host=host, mc_version=plan.mc_version,
                loader=plan.loader) if f["enabled"]]
            if plan.loader in ("forge", "neoforge"):
                await engine.write_file(
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
                # heap lives in the startup command. This used to be
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
        java = await engine.set_runtime(server_id, plan.mc_version)
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
        "server_loader": plan.server_loader,
        # Kept so the Optimize tab can size the heap against what the pack
        # actually asked for, rather than guessing from the mod count.
        "recommended_ram_mb": plan.recommended_ram,
        "java_version": plan.java_version,
        "excluded_mods": sorted(excluded) if excluded else [],
        "disabled_mods": sorted(set(disabled_mods)),
        "mods": records,
        "problems": problems,
        # Not downloaded, on purpose: their authors turned off third-party
        # distribution. The Mods tab lists them with an upload prompt.
        "blocked_by_author": blocked_by_author,
    }
    try:
        await engine.write_manifest(server_id, manifest)
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
        "blocked_by_author": blocked_by_author,
        "note": installed_note,
    }
    job.log_line(
        f"Done: {summary['mods_installed']} mods installed into '{server_name}'"
        + (f", {len(summary['client_only_disabled'])} left disabled as client-only"
           if summary["client_only_disabled"] else "")
    )

    # Installed is not running. Boot it once and fix what that boot proves
    # broken -- a missing library, a client mod a server pack should not have
    # shipped -- so the operator gets a server that starts, not a list of
    # things to do first (app/repair.py).
    if boot_check:
        from app import repair
        job.set_step("First boot", 97)
        try:
            boot = await repair.boot_and_repair(job, server_id)
        except Exception as e:      # noqa: BLE001 -- the install itself succeeded
            boot = {"outcome": "error", "why": str(e), "repairs": []}
        summary["boot"] = boot
        summary["repairs"] = boot.get("repairs") or []
        try:
            manifest = await engine.read_manifest(server_id)
            manifest["boot_check"] = {k: boot.get(k) for k in
                                      ("outcome", "rounds", "why", "seconds")} | {
                "at": time.time()}
            await engine.write_manifest(server_id, manifest)
        except Exception:       # noqa: BLE001
            pass
        if boot["outcome"] == "ready":
            job.log_line(f"First boot: up in {boot.get('seconds')}s"
                         + (f" after {len(summary['repairs'])} repair(s)"
                            if summary["repairs"] else ""))
        else:
            job.log_line(f"First boot: {boot['outcome']} -- {boot.get('why')}", "warn")

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
    of them back off the node to learn what the archive already knew.

    Two bulk calls for the whole pack: CurseForge by its own fingerprint, then
    Modrinth by SHA-1 for whatever is left.
    """
    todo = [r for r in records if not r.get("project_id") and r.get("member")]
    if not todo:
        return
    job.set_step(f"Identifying {len(todo)} jars", 58)

    by_fp: dict[int, dict] = {}
    by_sha: dict[str, dict] = {}

    def read_members(members: list[str]) -> list[bytes | None]:
        out = []
        for member in members:
            try:
                out.append(zf.read(member))
            except Exception:       # noqa: BLE001
                out.append(None)
        return out

    # In batches, off the event loop: decompression in a thread, CurseForge's
    # fingerprint in worker processes. Both used to run inline, and on ATM10
    # or Integrated MC that froze the whole API for minutes -- a client
    # polling the install got a read timeout. A batch at a time keeps memory
    # flat on a gigabyte pack.
    batch = 16
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        job.set_step(f"Identifying jars ({min(i + batch, len(todo))}/{len(todo)})",
                     58 + 1.5 * i / max(len(todo), 1))
        blobs = await asyncio.to_thread(read_members, [r["member"] for r in chunk])
        fps = await curseforge.fingerprint_many([b or b"" for b in blobs])
        for r, blob, fp in zip(chunk, blobs, fps):
            if blob is None:
                continue
            if fp is not None:
                by_fp[fp] = r
            try:
                by_sha[modrinth.sha1(blob)] = r
            except Exception:       # noqa: BLE001
                pass
        del blobs

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
    current = await engine.read_manifest(server_id)
    server = await engine.get_server(server_id)

    job.log_line(
        f"Current: {(current.get('pack') or {}).get('name','unknown')} "
        f"{(current.get('pack') or {}).get('version','')}"
    )

    # Clear out the old mods so removed ones do not linger and crash startup.
    job.set_step("Removing previous mods", 8)
    try:
        entries = await engine.list_dir(server_id, "mods")
        stale = [
            f"mods/{n}" for n, meta in entries.items()
            if n != "root_path" and isinstance(meta, dict) and not meta.get("dir")
        ]
        if stale:
            await engine.delete_paths(server_id, stale)
            job.log_line(f"Removed {len(stale)} old mod files")
    except engine.EngineError:
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
