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
import io
import json
import posixpath
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from app import config, crafty, curseforge, jarmeta, modrinth, packs, uploads
from app import preflight as preflight_mod
from app import specs
from app.jobs import Job
from app.packs import PackPlan

# Upload batches are capped so a 3 GB pack never lands in memory at once.
BATCH_BYTES = 120 * 1024 * 1024
BATCH_FILES = 400


def _cache_path(file_id: int, file_name: str) -> Path:
    safe = re.sub(r"[^\w.\-]+", "_", file_name or f"{file_id}.zip")
    return config.CACHE_DIR / f"{file_id}-{safe}"


async def fetch_pack_archive(job: Job, mod_id: int, file_id: int) -> tuple[bytes, dict]:
    """Download a pack archive, reusing the on-disk copy when we have it.

    Preflight and install both need the same archive, and these are hundreds
    of megabytes -- caching turns the review step from a second download into
    a disk read.
    """
    meta = await curseforge.get_file(mod_id, file_id)
    path = _cache_path(file_id, meta.get("file_name", ""))
    expected = meta.get("size") or 0

    if path.exists() and (not expected or abs(path.stat().st_size - expected) < 1024):
        job.log_line(f"Using cached {meta.get('file_name')}")
        return path.read_bytes(), meta

    size_mb = expected / (1024 * 1024)
    job.log_line(f"Downloading {meta.get('file_name')} ({size_mb:.0f} MB)")
    data = await curseforge.download(meta)
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError as e:
        job.log_line(f"Could not cache the archive: {e}", "warn")
    return data, meta


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
            data, sp_meta = await fetch_pack_archive(job, mod_id, server_pack_id)
            zf = zipfile.ZipFile(io.BytesIO(data))
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
    data, _ = await fetch_pack_archive(job, mod_id, file_id)
    zf = zipfile.ZipFile(io.BytesIO(data))
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
) -> tuple[list[tuple[str, bytes]], list[dict], list[dict], list[str]]:
    """Resolve and fetch every mod the manifest lists.

    Returns (files, records, problems, skipped) where files are (name, bytes)
    pairs ready to be zipped into the overlay, and skipped names the jars an
    exclusion actually matched.
    """
    entries = plan.manifest_files
    file_ids = [e["fileID"] for e in entries if e.get("fileID")]
    job.set_step(f"Resolving {len(file_ids)} mod files", 12)
    file_meta = await curseforge.get_files(file_ids)

    mod_ids = {m.get("mod_id") for m in file_meta.values() if m.get("mod_id")}
    projects = await curseforge.get_mods(mod_ids) if mod_ids else {}

    results: list[tuple[str, bytes]] = []
    records: list[dict] = []
    problems: list[dict] = []
    skipped_client: list[str] = []

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
                    # An explicit exclusion list from the review step always
                    # wins -- the user has already seen the evidence and made
                    # the call, so no heuristic should second-guess it.
                    if exclude_files is not None:
                        if fname in exclude_files:
                            skipped_client.append(fname)
                            return
                    elif skip_client_only and packs.is_client_only_jar(fname):
                        skipped_client.append(fname)
                        return
                    # Non-jar manifest entries are resource packs etc.
                    if not fname.lower().endswith(".jar"):
                        return
                    # Reuses whatever the review step already downloaded.
                    blob = await curseforge.download_cached(meta, client)
                    # With no explicit list, fall back to the jar's own
                    # declaration: Fabric/Quilt state their side outright, and
                    # a client-only mod left in place takes the server down.
                    if exclude_files is None and skip_client_only:
                        info = jarmeta.parse(blob, fname)
                        if info.get("side") == "client":
                            skipped_client.append(fname)
                            return
                    results.append((fname, blob))
                    records.append({
                        "file": f"mods/{fname}",
                        "source": "curseforge",
                        "project_id": pid,
                        "file_id": fid,
                        "name": project.get("name") or fname,
                        "version": meta.get("display_name"),
                        "required": entry.get("required", True),
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

    if skipped_client:
        job.log_line(
            f"Skipped {len(skipped_client)} client-only mods: "
            + ", ".join(sorted(skipped_client)[:8])
            + ("..." if len(skipped_client) > 8 else ""),
            "warn",
        )
    if problems:
        job.log_line(f"{len(problems)} mods could not be downloaded", "warn")
        for p in problems[:10]:
            job.log_line(f"  - {p['name']}: {p['reason']}", "warn")
    return results, records, problems, skipped_client


def _batch(items: list[tuple[str, bytes]]) -> list[list[tuple[str, bytes]]]:
    """Split (path, bytes) pairs into upload batches bounded by size/count."""
    batches: list[list[tuple[str, bytes]]] = []
    current: list[tuple[str, bytes]] = []
    size = 0
    for name, blob in items:
        if current and (size + len(blob) > BATCH_BYTES or len(current) >= BATCH_FILES):
            batches.append(current)
            current, size = [], 0
        current.append((name, blob))
        size += len(blob)
    if current:
        batches.append(current)
    return batches


async def _push_batches(
    job: Job, server_id: str, items: list[tuple[str, bytes]],
    start_pct: float, end_pct: float, label: str,
) -> None:
    """Zip -> upload -> unzip, in chunks, so memory stays bounded."""
    batches = _batch(items)
    if not batches:
        return
    for i, batch in enumerate(batches, 1):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for name, blob in batch:
                zf.writestr(name, blob)
        payload = buf.getvalue()
        zip_name = f".studio-batch-{int(time.time())}-{i}.zip"
        pct = start_pct + (end_pct - start_pct) * ((i - 1) / len(batches))
        job.set_step(
            f"{label} ({i}/{len(batches)}, {len(payload)/1048576:.0f} MB)", pct
        )
        await crafty.upload_file(server_id, ".", zip_name, payload)
        await crafty.unzip(server_id, zip_name)

        # Unzip runs on a background thread: wait for a member to appear.
        probe = batch[0][0]
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


async def _wait_for_loader(job: Job, server_id: str, plan: PackPlan) -> None:
    """Block until Crafty has finished installing the loader.

    Crafty rewrites `executable` and `execution_command` on the server record
    only when its installer thread finishes. Waiting for the *record* to
    settle -- not just for files to appear -- matters, because anything we
    write to the launch command before that point gets overwritten.
    """
    job.set_step("Waiting for Crafty to install the loader", 60)
    deadline = time.time() + config.SERVER_READY_TIMEOUT
    last_sig = None
    stable = 0
    while time.time() < deadline:
        try:
            entries = await crafty.list_dir(server_id, ".")
            names = {k for k in entries if k != "root_path"}
            has_jar = any(n.endswith(".jar") for n in names)
            has_libs = "libraries" in names

            server = await crafty.get_server(server_id)
            executable = server.get("executable") or ""
            command = server.get("execution_command") or ""

            sig = (len(names), executable, command)
            if sig == last_sig:
                stable += 1
            else:
                stable = 0
                last_sig = sig

            # Forge/NeoForge point `executable` at the built server jar under
            # libraries/; fabric and vanilla just use a jar in the root.
            record_ready = bool(executable and command)
            files_ready = has_jar or has_libs
            if record_ready and files_ready and stable >= 2:
                job.log_line(f"Loader install complete ({len(names)} entries)")
                return
        except crafty.CraftyError:
            pass
        await asyncio.sleep(5)
    job.log_line(
        "Timed out waiting for the loader install; continuing anyway.", "warn"
    )


async def _apply_server_settings(
    job: Job, server_id: str, plan: PackPlan, mem_max_gb: int, motd: str | None
) -> None:
    """EULA, RAM and a few server.properties niceties."""
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
    if excluded is not None:
        job.log_line(
            f"Using your review decisions: {len(excluded)} mods will be skipped"
        )

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
    payload: list[tuple[str, bytes]] = []

    matched_exclusions: set[str] = set()
    if plan.source == "manifest":
        mods, records, problems, skipped = await _download_manifest_mods(
            job, plan, skip_client_only, excluded
        )
        matched_exclusions.update(skipped)
        payload.extend((f"mods/{name}", blob) for name, blob in mods)

    job.set_step("Preparing pack files", 58)
    overlay_skipped = 0
    for entry in plan.overlay_members:
        target = entry["target"]
        base = posixpath.basename(target)
        is_mod_jar = target.startswith("mods/") and target.endswith(".jar")
        if excluded is not None:
            if is_mod_jar and base in excluded:
                overlay_skipped += 1
                matched_exclusions.add(base)
                continue
        elif (
            skip_client_only
            and target.startswith("mods/")
            and packs.is_client_only_jar(target)
        ):
            overlay_skipped += 1
            continue
        try:
            blob = zf.read(entry["member"])
        except Exception as e:
            problems.append({"name": target, "reason": f"unreadable in archive: {e}"})
            continue
        if (
            excluded is None
            and skip_client_only
            and is_mod_jar
            and jarmeta.parse(blob, target).get("side") == "client"
        ):
            overlay_skipped += 1
            continue
        payload.append((target, blob))
        if is_mod_jar:
            records.append({
                # A jar in a client export's overrides/ was added by hand and
                # is in no catalogue, so it can never be version-checked --
                # saying where it came from is what stops the Mods tab
                # claiming it is simply "unidentified".
                "file": target,
                "source": "bundled" if plan.source == "manifest" else "server_pack",
                "name": posixpath.basename(target),
            })
    if overlay_skipped:
        job.log_line(f"Skipped {overlay_skipped} client-only jars from the pack", "warn")

    # An exclusion that matches nothing means the caller and the pack disagree
    # about what is in it -- a review answered against a different version, or
    # a hand-made API call. Silently installing the mod anyway is the one
    # outcome nobody wants, so say it plainly.
    if excluded:
        unmatched = excluded - matched_exclusions
        if unmatched:
            job.log_line(
                f"{len(unmatched)} exclusion(s) matched no file in this pack and "
                "had no effect: "
                + ", ".join(sorted(unmatched)[:5])
                + ("..." if len(unmatched) > 5 else ""),
                "warn",
            )

    total_mb = sum(len(b) for _, b in payload) / 1048576
    job.log_line(f"Uploading {len(payload)} files ({total_mb:.0f} MB) to the instance")

    await _push_batches(job, server_id, payload, 60, 92, "Uploading pack files")

    # --- finishing touches --------------------------------------------
    job.set_step("Applying server settings", 94)
    await _apply_server_settings(job, server_id, plan, ram_max, motd)

    # Size the heap against the host, not just the pack's wishlist: a pack
    # asking for 8 GB on a box with 5 GB free dies at startup with no log.
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
            for w in memory.get("warnings", []):
                job.log_line(w, "warn")
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

    job.set_step("Recording install manifest", 97)
    manifest = {
        "schema": 1,
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
        "files_uploaded": len(payload),
        "problems": problems,
        "note": installed_note,
    }
    job.log_line(
        f"Done: {summary['mods_installed']} mods installed into '{server_name}'"
    )
    return summary


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
    )
    result["previous"] = current.get("pack")
    return result
