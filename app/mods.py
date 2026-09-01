"""Per-instance mod management.

Disabling a mod renames it to `<name>.jar.disabled`, which every loader
ignores -- the same convention the Crafty and CurseForge UIs use, so a mod
disabled here looks disabled everywhere else too.

Mods installed through this app are recorded in `.modpack-studio.json`,
which is what makes "check for updates" and "switch version" possible.
Jars that predate the app (or came from a server pack) are identified on
demand by hashing them: CurseForge murmur2 fingerprints first, then
Modrinth SHA-1.
"""
from __future__ import annotations

import asyncio
import posixpath
import re
import time

from app import config, crafty, curseforge, deps, jarmeta, modrinth, packs
from app.jobs import Job

DISABLED_SUFFIX = ".disabled"

# The directories an instance keeps loadable jars in. `directory` arrives on
# the request body, and every path built from it is handed to Crafty to
# rename, delete or download -- so it is checked here rather than trusted.
# Crafty does reject a traversal, but it does it with a 500 and a traceback,
# and "the component downstream happens to refuse" is not a guard.
MOD_DIRS = {"mods", "plugins", "config", "kubejs", "datapacks", "shaderpacks"}


def guard_dir(directory: str) -> str:
    d = (directory or "mods").replace("\\", "/").strip().strip("/")
    if d not in MOD_DIRS:
        raise ValueError(
            f"{directory!r} is not a mod directory "
            f"(expected one of {sorted(MOD_DIRS)})"
        )
    return d


def guard_name(name: str) -> str:
    """A bare filename -- no directory part, no traversal, no absolute path."""
    n = (name or "").replace("\\", "/").strip()
    # No separator means no traversal; the all-dots check stops "." and ".."
    # from standing in for a directory on their own.
    if not n or "/" in n or "\x00" in n or set(n) <= {"."}:
        raise ValueError(f"{name!r} is not a valid file name")
    return n


def guard_names(names: list[str]) -> list[str]:
    if not isinstance(names, list):
        raise ValueError("files must be a list")
    return [guard_name(n) for n in names]


def _is_jar(name: str) -> bool:
    return name.lower().endswith(".jar") or name.lower().endswith(".jar" + DISABLED_SUFFIX)


def _base_name(name: str) -> str:
    return name[: -len(DISABLED_SUFFIX)] if name.endswith(DISABLED_SUFFIX) else name


async def list_mods(server_id: str, directory: str = "mods") -> dict:
    """List every jar in an instance, enriched with whatever we know."""
    directory = guard_dir(directory)
    try:
        entries = await crafty.list_dir(server_id, directory)
    except crafty.CraftyError:
        return {"directory": directory, "mods": [], "error": "no mods directory"}

    manifest = await crafty.read_studio_manifest(server_id)
    by_file = {
        posixpath.basename(r.get("file", "")): r for r in manifest.get("mods", [])
    }

    mods = []
    for name, meta in entries.items():
        if name == "root_path" or not isinstance(meta, dict) or meta.get("dir"):
            continue
        if not _is_jar(name):
            continue
        enabled = not name.endswith(DISABLED_SUFFIX)
        base = _base_name(name)
        record = by_file.get(base) or {}
        guess = jarmeta.guess_from_filename(base)
        mods.append({
            "file": name,
            "path": f"{directory}/{name}",
            "enabled": enabled,
            "size": meta.get("size"),
            "modified": meta.get("modified"),
            "name": record.get("name") or guess["name"],
            "version": record.get("version") or guess["version"],
            "source": record.get("source"),
            "project_id": record.get("project_id"),
            "file_id": record.get("file_id"),
            "logo": record.get("logo"),
            "required_by": record.get("required_by"),
            # Recorded at install time from the review's evidence. Distinct
            # from the guess below: this one is a decision that was made and
            # acted on, and it is why the jar sits there disabled.
            "client_only": bool(record.get("client_only")),
            "client_only_reasons": record.get("client_only_reasons") or [],
            "client_only_guess": packs.is_client_only_jar(base),
            "dependencies": record.get("dependencies") or [],
            "identified": bool(record.get("project_id")),
        })
    mods.sort(key=lambda m: (not m["enabled"], (m["name"] or "").lower()))
    return {
        "directory": directory,
        "count": len(mods),
        "enabled": sum(1 for m in mods if m["enabled"]),
        "client_only": sum(1 for m in mods if m["client_only"]),
        "mods": mods,
        "pack": manifest.get("pack"),
        "minecraft": manifest.get("minecraft"),
        "loader": manifest.get("loader"),
    }


async def set_enabled(server_id: str, filename: str, enabled: bool,
                      directory: str = "mods") -> dict:
    """Toggle a mod by renaming it to/from .jar.disabled."""
    directory = guard_dir(directory)
    current = guard_name(filename)
    is_disabled = current.endswith(DISABLED_SUFFIX)
    if enabled and is_disabled:
        new_name = _base_name(current)
    elif not enabled and not is_disabled:
        new_name = current + DISABLED_SUFFIX
    else:
        return {"file": current, "enabled": enabled, "changed": False}

    try:
        await crafty.rename_path(server_id, f"{directory}/{current}", new_name)
    except crafty.CraftyError:
        # Callers name a mod by its enabled filename, because that is what the
        # UI shows and what a finding's `files` list carries. If it is already
        # in the state being asked for, the file under that exact name does not
        # exist and the rename fails -- so "disable these twelve" would report
        # nine errors the second time it ran. Asking what is actually on disk
        # turns that back into the no-op it is.
        try:
            entries = await crafty.list_dir(server_id, directory)
        except crafty.CraftyError:
            raise
        if new_name in entries and current not in entries:
            return {"file": new_name, "enabled": enabled, "changed": False,
                    "already": True}
        if current not in entries:
            # Neither name is on disk, so this is not an already-done toggle
            # but a mod that is not there. Say so in those terms: the raw
            # failure is a rename errno quoting Crafty's own filesystem path,
            # which tells the user nothing and puts server-side paths in a
            # browser response.
            raise ValueError(
                f"'{current}' is not in this server's {directory} folder")
        raise
    return {"file": new_name, "previous": current, "enabled": enabled, "changed": True}


async def delete_mods(server_id: str, filenames: list[str],
                      directory: str = "mods") -> dict:
    directory = guard_dir(directory)
    filenames = guard_names(filenames)
    paths = [f"{directory}/{f}" for f in filenames]
    await crafty.delete_paths(server_id, paths)
    await _forget_mods(server_id, filenames)
    return {"deleted": filenames, "count": len(filenames)}


async def _forget_mods(server_id: str, filenames: list[str]) -> None:
    manifest = await crafty.read_studio_manifest(server_id)
    if not manifest.get("mods"):
        return
    drop = {_base_name(f) for f in filenames}
    manifest["mods"] = [
        r for r in manifest["mods"]
        if posixpath.basename(r.get("file", "")) not in drop
    ]
    try:
        await crafty.write_studio_manifest(server_id, manifest)
    except Exception:
        pass


async def _remember_mod(server_id: str, record: dict) -> None:
    manifest = await crafty.read_studio_manifest(server_id)
    manifest.setdefault("mods", [])
    base = posixpath.basename(record.get("file", ""))
    manifest["mods"] = [
        r for r in manifest["mods"]
        if posixpath.basename(r.get("file", "")) != base
    ]
    manifest["mods"].append(record)
    manifest.setdefault("schema", 1)
    try:
        await crafty.write_studio_manifest(server_id, manifest)
    except Exception:
        pass


async def add_mod(
    server_id: str,
    *,
    source: str,
    project_id: str | int,
    file_id: str | int,
    directory: str = "mods",
    replace_file: str | None = None,
    required_by: str | None = None,
    dependency_files: list[str] | None = None,
) -> dict:
    """Install a single mod from CurseForge or Modrinth into an instance."""
    directory = guard_dir(directory)
    if replace_file:
        replace_file = guard_name(replace_file)
    if source == "curseforge":
        meta = await curseforge.get_file(int(project_id), int(file_id))
        blob = await curseforge.download(meta)
        project = await curseforge.get_mod(int(project_id))
        display_name = project.get("name") or meta.get("file_name")
        version = meta.get("display_name")
        filename = meta.get("file_name")
        logo = project.get("logo")
    elif source == "modrinth":
        version_meta = await modrinth.get_version(str(file_id))
        if not version_meta:
            raise ValueError(f"Modrinth version {file_id} not found")
        blob = await modrinth.download(version_meta)
        project = await modrinth.get_project(str(project_id)) or {}
        display_name = project.get("name") or version_meta.get("file_name")
        version = version_meta.get("version_number")
        filename = version_meta.get("file_name")
        logo = project.get("logo")
    else:
        raise ValueError(f"unknown source '{source}'")

    if not filename:
        raise ValueError("could not determine a filename for this mod")

    # Replacing = version switch: drop the old jar first so both do not load.
    if replace_file:
        try:
            await crafty.delete_paths(server_id, [f"{directory}/{replace_file}"])
        except crafty.CraftyError:
            pass

    await crafty.ensure_dir(server_id, directory)
    await crafty.upload_file(server_id, directory, filename, blob)

    record = {
        "file": f"{directory}/{filename}",
        "source": source,
        "project_id": project_id,
        "file_id": file_id,
        "name": display_name,
        "version": version,
        "logo": logo,
        "added_at": time.time(),
    }
    if required_by:
        record["required_by"] = required_by
    if dependency_files:
        record["dependencies"] = sorted(set(dependency_files))
    await _remember_mod(server_id, record)
    return {
        "installed": filename,
        "replaced": replace_file,
        "name": display_name,
        "version": version,
        "logo": logo,
        "size": len(blob),
    }


async def add_mod_with_dependencies(
    job: Job,
    server_id: str,
    *,
    source: str,
    project_id: str | int,
    file_id: str | int,
    directory: str = "mods",
    replace_file: str | None = None,
    include_dependencies: bool = True,
    skip_projects: list | None = None,
) -> dict:
    """Install a mod and everything it requires.

    A mod without its libraries is the most common way to break a working
    server, so dependencies are resolved first and installed alongside.
    `skip_projects` lets the user deselect individual dependencies in the
    preview before confirming.
    """
    directory = guard_dir(directory)
    if replace_file:
        replace_file = guard_name(replace_file)
    job.set_step("Resolving dependencies", 5)
    plan = await deps.resolve_for_instance(
        server_id, source=source, project_id=project_id, file_id=file_id
    )
    if not plan.get("root"):
        raise RuntimeError("could not resolve this mod on its source")

    skip = {str(s) for s in (skip_projects or [])}
    queue = [plan["root"]]
    if include_dependencies:
        queue += [d for d in plan["dependencies"]
                  if str(d["project_id"]) not in skip]

    for warning in plan.get("warnings", []):
        job.log_line(warning, "warn")
    if plan.get("conflicts"):
        for c in plan["conflicts"]:
            job.log_line(
                f"Conflict: project {c['project_id']} -- {c['reason']}", "warn")

    installed, failed = [], []
    # Filenames the root mod pulled in, recorded on its manifest entry so the
    # Mods tab can show "this brought in these three" without re-resolving
    # the graph over the network every time the page opens.
    dependency_files: list[str] = []
    total = len(queue)
    for index, entry in enumerate(queue, 1):
        label = entry.get("name") or entry.get("file_name")
        job.set_step(f"Installing {label} ({index}/{total})",
                     5 + 90 * (index - 1) / max(total, 1))
        try:
            is_root = entry is plan["root"]
            result = await add_mod(
                server_id,
                source=entry["source"],
                project_id=entry["project_id"],
                file_id=entry["file_id"],
                directory=directory,
                replace_file=replace_file if is_root else None,
                required_by=entry.get("required_by"),
            )
            if not is_root:
                dependency_files.append(result["installed"])
            installed.append(result)
            suffix = (f" (required by {entry['required_by']})"
                      if entry.get("required_by") else "")
            job.log_line(f"Installed {result['installed']}{suffix}")
        except Exception as e:
            failed.append({"name": label, "error": str(e)})
            job.log_line(f"Failed to install {label}: {e}", "error")

    if dependency_files and installed:
        # Re-save the root's record now that the full list is known.
        root_file = installed[0].get("installed")
        if root_file:
            manifest = await crafty.read_studio_manifest(server_id)
            for rec in manifest.get("mods", []):
                if posixpath.basename(rec.get("file", "")) == root_file:
                    rec["dependencies"] = sorted(set(dependency_files))
                    try:
                        await crafty.write_studio_manifest(server_id, manifest)
                    except Exception:
                        pass
                    break

    if plan.get("already_satisfied"):
        job.log_line(
            f"{len(plan['already_satisfied'])} dependencies were already present"
        )
    return {
        "installed": installed,
        "failed": failed,
        "dependencies_installed": max(0, len(installed) - 1),
        "already_satisfied": len(plan.get("already_satisfied", [])),
        "warnings": plan.get("warnings", []),
    }


async def enrich_icons(server_id: str, directory: str = "mods") -> dict:
    """Fill in missing mod logos for identified mods, and cache them.

    Icons come from the project record, so this only works for mods we have
    identified. Results are written back into the instance manifest, making
    every later page load free.
    """
    manifest = await crafty.read_studio_manifest(server_id)
    records = manifest.get("mods", [])
    need_cf = [r for r in records
               if r.get("source") == "curseforge" and r.get("project_id")
               and not r.get("logo")]
    need_mr = [r for r in records
               if r.get("source") == "modrinth" and r.get("project_id")
               and not r.get("logo")]
    if not need_cf and not need_mr:
        return {"updated": 0}

    updated = 0
    if need_cf:
        try:
            projects = await curseforge.get_mods(
                [int(r["project_id"]) for r in need_cf]
            )
            for r in need_cf:
                p = projects.get(int(r["project_id"]))
                if p and p.get("logo"):
                    r["logo"] = p["logo"]
                    updated += 1
        except Exception:
            pass
    if need_mr:
        try:
            projects = await modrinth.get_projects(
                [str(r["project_id"]) for r in need_mr]
            )
            for r in need_mr:
                p = projects.get(str(r["project_id"]))
                if p and p.get("logo"):
                    r["logo"] = p["logo"]
                    updated += 1
        except Exception:
            pass

    if updated:
        manifest["mods"] = records
        try:
            await crafty.write_studio_manifest(server_id, manifest)
        except Exception:
            pass
    return {"updated": updated}


async def list_mod_versions(
    source: str, project_id: str | int, *, game_version: str | None = None,
    loader: str | None = None,
) -> list[dict]:
    """Versions of a single mod, for the version-switch dropdown."""
    if source == "curseforge":
        return await curseforge.list_files(
            int(project_id), game_version=game_version, mod_loader=loader, page_size=50
        )
    if source == "modrinth":
        return await modrinth.list_versions(
            str(project_id), game_version=game_version, loader=loader
        )
    raise ValueError(f"unknown source '{source}'")


async def identify_mods(
    job: Job, server_id: str, directory: str = "mods", limit: int = 400
) -> dict:
    """Hash unidentified jars and match them against CurseForge/Modrinth.

    Each jar is pulled back out of Crafty, so this is deliberately an
    explicit action rather than something that runs on every page load.
    """
    listing = await list_mods(server_id, directory)
    unknown = [m for m in listing["mods"] if not m["identified"]][:limit]
    if not unknown:
        return {"identified": 0, "checked": 0, "mods": []}

    job.set_step(f"Downloading {len(unknown)} jars for identification", 5)
    sem = asyncio.Semaphore(4)
    blobs: dict[str, bytes] = {}
    done = 0
    lock = asyncio.Lock()

    async def fetch(m):
        nonlocal done
        async with sem:
            try:
                blobs[m["file"]] = await crafty.download_file(server_id, m["path"])
            except Exception as e:
                job.log_line(f"Could not read {m['file']}: {e}", "warn")
            finally:
                async with lock:
                    done += 1
                    job.set_step(
                        f"Reading jars ({done}/{len(unknown)})",
                        5 + 45 * done / len(unknown),
                    )

    await asyncio.gather(*(fetch(m) for m in unknown))

    job.set_step("Matching against CurseForge", 55)
    fingerprints = {}
    for fname, blob in blobs.items():
        try:
            fingerprints[curseforge.fingerprint(blob)] = fname
        except Exception:
            pass

    matched: dict[str, dict] = {}
    try:
        cf_matches = await curseforge.match_fingerprints(list(fingerprints))
        for fp, match in cf_matches.items():
            fname = fingerprints.get(fp)
            if fname:
                matched[fname] = {
                    "source": "curseforge",
                    "project_id": match["mod_id"],
                    "file_id": match["file"].get("file_id"),
                    "version": match["file"].get("display_name"),
                }
    except Exception as e:
        job.log_line(f"CurseForge fingerprint lookup failed: {e}", "warn")

    # Anything CurseForge did not recognise may still be a Modrinth mod.
    job.set_step("Matching against Modrinth", 75)
    leftover = {f: b for f, b in blobs.items() if f not in matched}
    if leftover and config.MODRINTH_ENABLED:
        hashes = {modrinth.sha1(b): f for f, b in leftover.items()}
        try:
            mr = await modrinth.versions_from_hashes(list(hashes))
            for h, version in mr.items():
                fname = hashes.get(h)
                if fname:
                    matched[fname] = {
                        "source": "modrinth",
                        "project_id": version.get("mod_id"),
                        "file_id": version.get("file_id"),
                        "version": version.get("version_number"),
                    }
        except Exception as e:
            job.log_line(f"Modrinth hash lookup failed: {e}", "warn")

    # Fall back to reading the jar's own metadata for anything still unknown.
    job.set_step("Reading jar metadata", 88)
    local_meta = {}
    for fname, blob in blobs.items():
        if fname in matched:
            continue
        info = jarmeta.parse(blob, fname)
        if info.get("name") or info.get("mod_id"):
            local_meta[fname] = info

    # Resolve display names for everything we matched.
    cf_ids = [m["project_id"] for m in matched.values()
              if m["source"] == "curseforge" and m.get("project_id")]
    mr_ids = [m["project_id"] for m in matched.values()
              if m["source"] == "modrinth" and m.get("project_id")]
    cf_projects = await curseforge.get_mods(cf_ids) if cf_ids else {}
    mr_projects = await modrinth.get_projects(mr_ids) if mr_ids else {}

    manifest = await crafty.read_studio_manifest(server_id)
    manifest.setdefault("mods", [])
    existing = {posixpath.basename(r.get("file", "")): r for r in manifest["mods"]}

    results = []
    for fname, match in matched.items():
        base = _base_name(fname)
        if match["source"] == "curseforge":
            project = cf_projects.get(match["project_id"], {}) or {}
        else:
            project = mr_projects.get(match["project_id"], {}) or {}
        name = project.get("name") or jarmeta.guess_from_filename(base)["name"]
        record = {
            "file": f"{directory}/{base}",
            "source": match["source"],
            "project_id": match["project_id"],
            "file_id": match["file_id"],
            "name": name,
            "version": match["version"],
            "logo": project.get("logo"),
            "identified_at": time.time(),
        }
        existing[base] = record
        results.append({"file": fname, **record})

    for fname, info in local_meta.items():
        base = _base_name(fname)
        if base in existing:
            continue
        existing[base] = {
            "file": f"{directory}/{base}",
            "source": "local",
            "name": info.get("name") or jarmeta.guess_from_filename(base)["name"],
            "version": info.get("version"),
            "mod_id": info.get("mod_id"),
            "loader": info.get("loader"),
            "side": info.get("side"),
        }

    manifest["mods"] = list(existing.values())
    try:
        await crafty.write_studio_manifest(server_id, manifest)
    except Exception as e:
        job.log_line(f"Could not save identification results: {e}", "warn")

    job.log_line(
        f"Identified {len(results)} of {len(unknown)} unknown mods "
        f"({len(local_meta)} read from jar metadata only)"
    )
    return {
        "checked": len(unknown),
        "identified": len(results),
        "from_jar_metadata": len(local_meta),
        "mods": results,
    }


async def check_updates(server_id: str, directory: str = "mods") -> dict:
    """For every identified mod, report whether a newer file exists."""
    manifest = await crafty.read_studio_manifest(server_id)
    mc = manifest.get("minecraft")
    loader = manifest.get("loader")
    records = [r for r in manifest.get("mods", []) if r.get("project_id")]
    if not records:
        return {"updates": [], "checked": 0,
                "note": "No identified mods -- run Identify first."}

    updates = []

    # One request per mod is 225 requests for an ordinary pack and the best
    # part of a minute. CurseForge will answer for the whole set at once, so
    # ask that way first and keep the per-mod path only for what it could not
    # resolve -- typically a handful of files that declare no loader.
    bulk: dict[int, int] = {}
    cf_records = [r for r in records if r["source"] == "curseforge"]
    if cf_records and mc and loader:
        try:
            bulk = await curseforge.latest_file_ids(
                [int(r["project_id"]) for r in cf_records], mc, loader)
        except Exception:
            bulk = {}

    def note(rec, latest_id, latest_name=None, latest_date=None):
        if str(latest_id) == str(rec.get("file_id")):
            return
        updates.append({
            "file": posixpath.basename(rec.get("file", "")),
            "name": rec.get("name"),
            "source": rec["source"],
            "project_id": rec["project_id"],
            "current_version": rec.get("version"),
            "current_file_id": rec.get("file_id"),
            "latest_version": latest_name,
            "latest_file_id": latest_id,
            "latest_date": latest_date,
        })

    slow: list[dict] = []
    for rec in records:
        if rec["source"] == "curseforge":
            hit = bulk.get(int(rec["project_id"])) if rec.get("project_id") else None
            if hit:
                note(rec, hit)
                continue
        slow.append(rec)

    sem = asyncio.Semaphore(6)

    async def check(rec):
        async with sem:
            try:
                versions = await list_mod_versions(
                    rec["source"], rec["project_id"],
                    game_version=mc, loader=loader,
                )
            except Exception:
                return
            if not versions:
                return
            latest = versions[0]
            current_id = str(rec.get("file_id"))
            if str(latest.get("file_id")) != current_id:
                updates.append({
                    "file": posixpath.basename(rec.get("file", "")),
                    "name": rec.get("name"),
                    "source": rec["source"],
                    "project_id": rec["project_id"],
                    "current_version": rec.get("version"),
                    "current_file_id": rec.get("file_id"),
                    "latest_version": latest.get("display_name")
                    or latest.get("version_number"),
                    "latest_file_id": latest.get("file_id"),
                    "latest_date": latest.get("date"),
                })

    await asyncio.gather(*(check(r) for r in slow))

    # The bulk index gives a file id and nothing else, so name and date are
    # filled in afterwards -- for the handful that actually have an update,
    # not for every mod on the server.
    unnamed = [u for u in updates if u["latest_version"] is None
               and u["source"] == "curseforge" and u["latest_file_id"]]
    if unnamed:
        try:
            meta = await curseforge.get_files(
                [int(u["latest_file_id"]) for u in unnamed])
        except Exception:
            meta = {}
        for u in unnamed:
            f = meta.get(int(u["latest_file_id"]))
            if f:
                u["latest_version"] = f.get("display_name") or f.get("file_name")
                u["latest_date"] = f.get("date")

    updates.sort(key=lambda u: (u["name"] or "").lower())
    return {"checked": len(records), "updates": updates,
            "minecraft": mc, "loader": loader,
            "resolved_in_bulk": len(records) - len(slow)}


async def dependency_map(server_id: str, directory: str = "mods") -> dict:
    """Which mods pulled in which, for the Mods tab's dependency view.

    Adding a mod no longer stops to show its dependency list before
    installing -- that was three clicks and a wait to answer a question most
    people did not have. The information itself is still worth having, so it
    is recorded as mods are added and shown here on demand instead.

    Everything comes from the instance manifest, so this is a couple of file
    reads and no network calls at all.
    """
    listing = await list_mods(server_id, directory)
    by_file = {m["file"]: m for m in listing["mods"]}
    by_base = {_base_name(f): m for f, m in by_file.items()}

    def brief(name: str) -> dict:
        mod = by_base.get(_base_name(name))
        return {
            "file": mod["file"] if mod else name,
            "name": (mod or {}).get("name") or name,
            "logo": (mod or {}).get("logo"),
            "enabled": (mod or {}).get("enabled", True),
            "present": bool(mod),
        }

    parents: list[dict] = []
    child_of: dict[str, list[str]] = {}
    for mod in listing["mods"]:
        for dep in mod.get("dependencies") or []:
            child_of.setdefault(_base_name(dep), []).append(mod["name"] or mod["file"])
        if mod.get("dependencies"):
            parents.append({
                **brief(mod["file"]),
                "dependencies": [brief(d) for d in mod["dependencies"]],
            })

    # Mods installed as somebody's dependency but whose parent never recorded
    # the link -- an older install, or a dependency added on its own.
    orphans = [
        {**brief(m["file"]), "required_by": m["required_by"]}
        for m in listing["mods"]
        if m.get("required_by") and not child_of.get(_base_name(m["file"]))
    ]

    standalone = sum(
        1 for m in listing["mods"]
        if not m.get("dependencies") and not m.get("required_by")
    )
    parents.sort(key=lambda p: (p["name"] or "").lower())
    return {
        "count": listing["count"],
        "parents": parents,
        "orphans": orphans,
        "standalone": standalone,
        "note": None if parents or orphans else (
            "Nothing here records a dependency yet. Mods added from now on "
            "note what they pulled in; mods that arrived with the modpack "
            "were resolved by the pack author instead."
        ),
    }
