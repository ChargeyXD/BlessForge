"""Turn any installed server back into a CurseForge modpack archive.

The roulette could already write one of these, but only for a hand it had
just dealt -- so the five servers somebody actually plays on could not be
shared, backed up as a pack, or moved to another machine. Everything needed
was already recorded: the install manifest names every mod's project and file
id, which is exactly what a CurseForge manifest is made of.

Mods with no CurseForge id (Modrinth, or a jar somebody dropped in by hand)
cannot go in `files`, so their jars travel inside `overrides/mods/` the way
the CurseForge app itself does it. Those have to come back off the node,
which is the slow part, so it is optional and off by default.
"""
from __future__ import annotations

import io
import json
import posixpath
import re
import time
import zipfile

from app import engine, curseforge, mods as modmgr
from app.jobs import Job

DISABLED_SUFFIX = ".disabled"

# The pack's own settings -- what makes it this pack and not just its mod
# list -- carried in overrides/ the way a CurseForge pack carries them. Never
# the world, logs, the loader's libraries or the mods folder.
CONFIG_FOLDERS = ("config", "defaultconfigs", "kubejs", "global_packs", "openloader",
                  "scripts", "paxi")
_CONFIGS_ZIP = ".blessforge-export-configs.zip"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip() or "pack"


async def export_instance(job: Job, server_id: str, *,
                          include_disabled: bool = False,
                          bundle_others: bool = True,
                          include_configs: bool = True) -> dict:
    """Build a CurseForge modpack zip from what is installed."""
    job.set_step("Reading the install manifest", 5)
    manifest = await engine.read_manifest(server_id)
    mc = manifest.get("minecraft") or ""
    loader = manifest.get("loader") or ""
    pack = manifest.get("pack") or {}
    name = pack.get("name") or "BlessForge export"
    if not mc or not loader:
        raise ValueError(
            "this server has no BlessForge install manifest, so there is "
            "nothing to describe it with -- only servers installed through "
            "BlessForge can be exported")

    listing = await modmgr.list_mods(server_id)
    on_disk = {m["file"]: m for m in listing.get("mods", [])}
    records = manifest.get("mods", [])

    job.set_step("Matching mods to catalogue entries", 20)
    cf_files: list[dict] = []
    others: list[dict] = []
    skipped: list[str] = []

    for rec in records:
        fname = posixpath.basename(rec.get("file", ""))
        if not fname.lower().endswith(".jar"):
            continue
        entry = on_disk.get(fname) or on_disk.get(fname + DISABLED_SUFFIX)
        enabled = bool(entry and entry.get("enabled"))
        if entry is None:
            skipped.append(fname)      # recorded once, no longer on disk
            continue
        if not enabled and not include_disabled:
            continue
        if rec.get("source") == "curseforge" and rec.get("project_id") and rec.get("file_id"):
            cf_files.append({
                "projectID": int(rec["project_id"]),
                "fileID": int(rec["file_id"]),
                "required": True,
            })
        else:
            others.append({"file": fname, "name": rec.get("name") or fname,
                           "source": rec.get("source")})

    # The manifest must name a real loader build, not just the family, or the
    # CurseForge app refuses the import outright.
    loader_id = await curseforge.loader_build_id(mc, loader)
    if not loader_id:
        job.log_line(
            f"No {loader} build listed for {mc}; the export will name the "
            "loader without a version, which BlessForge can import but the "
            "CurseForge app cannot.", "warn")

    bundled: dict[str, bytes] = {}
    if others and bundle_others:
        job.set_step(f"Fetching {len(others)} non-CurseForge jars", 40)
        for i, other in enumerate(others):
            try:
                blob = await engine.download_file(server_id, f"mods/{other['file']}")
                bundled[other["file"]] = blob
            except Exception as e:
                job.log_line(f"Could not fetch {other['file']}: {e}", "warn")
            job.set_step(f"Fetching jars ({i + 1}/{len(others)})",
                         40 + 40 * (i + 1) / len(others))

    # Zipped on the node in one go and fetched as one file: a big pack's
    # config/ is thousands of small files, and one request each was the
    # slow way.
    configs: dict[str, bytes] = {}
    if include_configs:
        try:
            top = await engine.list_dir(server_id, ".")
        except Exception:       # noqa: BLE001
            top = {}
        present = [f for f in CONFIG_FOLDERS if (top.get(f) or {}).get("dir")]
        if present:
            job.set_step(f"Packing {', '.join(present)}", 82)
            try:
                await engine.compress(server_id, present, _CONFIGS_ZIP)
                blob = await engine.download_file(server_id, _CONFIGS_ZIP)
                with zipfile.ZipFile(io.BytesIO(blob)) as src:
                    for info in src.infolist():
                        if not info.is_dir() and info.filename.split("/")[0] in present:
                            configs[info.filename] = src.read(info)
            except Exception as e:      # noqa: BLE001 -- the mod list still exports
                job.log_line(f"Could not pack the configs: {e}", "warn")
            finally:
                try:
                    await engine.delete_paths(server_id, [_CONFIGS_ZIP])
                except Exception:       # noqa: BLE001
                    pass

    job.set_step("Writing the archive", 85)
    doc = {
        "minecraft": {
            "version": mc,
            "modLoaders": [{"id": loader_id or loader, "primary": True}],
        },
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": name,
        "version": str(pack.get("version") or "1.0.0"),
        "author": "BlessForge",
        "files": cf_files,
        "overrides": "overrides",
    }

    rows = "\n".join(
        f"<li>{r.get('name') or posixpath.basename(r.get('file', ''))}</li>"
        for r in records
    )
    readme = (
        f"{name}\n{'=' * len(name)}\n\n"
        f"Exported from BlessForge on {time.strftime('%Y-%m-%d')}.\n\n"
        f"  minecraft    {mc}\n"
        f"  loader       {loader_id or loader}\n"
        f"  mods         {len(cf_files)} from CurseForge"
        + (f", {len(bundled)} bundled in overrides" if bundled else "")
        + (f"\n  configs      {len(configs)} files from "
           + ", ".join(sorted({p.split('/')[0] + '/' for p in configs})) if configs else "")
        + ("\n  disabled     included\n" if include_disabled else "\n")
        + (f"  not exported {len(skipped)} recorded but no longer on disk\n"
           if skipped else "")
        + "\nImport through BlessForge's Discover screen, or through the "
          "CurseForge app to play it.\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(doc, indent=2))
        z.writestr("modlist.html", f"<ul>\n{rows}\n</ul>\n")
        z.writestr("README.txt", readme)
        for fname, blob in bundled.items():
            z.writestr(f"overrides/mods/{fname}", blob)
        for rel, blob in configs.items():
            z.writestr(f"overrides/{rel}", blob)

    archive = buf.getvalue()
    job.log_line(
        f"Export written: {len(cf_files)} CurseForge mods"
        + (f", {len(bundled)} bundled" if bundled else "")
        + (f", {len(configs)} config files" if configs else "")
        + f", {len(archive) / 1048576:.1f} MB"
    )
    return {
        "filename": f"{_safe(name)}-{int(time.time())}.zip",
        "bytes": archive,
        "size": len(archive),
        "minecraft": mc,
        "loader": loader_id or loader,
        "curseforge_mods": len(cf_files),
        "bundled": sorted(bundled),
        "config_files": len(configs),
        "not_bundled": [o["file"] for o in others if o["file"] not in bundled],
        "skipped": skipped,
    }
