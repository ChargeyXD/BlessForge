#!/usr/bin/env python3
"""Rewrite the loader id in roulette exports written before the fix.

Exports built before this carried a bare family name ('neoforge') in
manifest.json, which the CurseForge app rejects with
MinecraftUnsupportedModLoader. The rest of the archive is fine, so the repair
is a one-field edit rather than a re-roll: resolve the real build for the
Minecraft version the manifest already names, and rewrite that one entry.

    python3 dev/tools/repair_export_loaders.py [--apply]

Without --apply it only reports. Repairs are written to a temporary file and
moved into place, so an interrupted run cannot leave a half-written zip.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from app import config, curseforge

VERSIONED = re.compile(r"^(forge|neoforge|fabric|quilt)-\d")


async def repair(path: pathlib.Path, apply: bool) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "manifest.json" not in names:
            return "no manifest.json"
        manifest = json.loads(z.read("manifest.json"))
        blobs = {n: z.read(n) for n in names}

    mc = (manifest.get("minecraft") or {}).get("version", "")
    loaders = (manifest.get("minecraft") or {}).get("modLoaders") or []
    if not loaders:
        return "no modLoaders entry"
    current = loaders[0].get("id", "")
    if VERSIONED.match(current):
        return f"already versioned ({current})"

    family = current.split("-")[0] or ""
    fixed = await curseforge.loader_build_id(mc, family)
    if not fixed:
        return f"no {family} build listed for {mc}; left alone"
    if not apply:
        return f"would rewrite {current} -> {fixed}"

    loaders[0]["id"] = fixed
    blobs["manifest.json"] = json.dumps(manifest, indent=2).encode()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            for n in names:
                out.writestr(n, blobs[n])
        shutil.move(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return f"rewrote {current} -> {fixed}"


async def main() -> int:
    apply = "--apply" in sys.argv
    root = config.DATA_DIR / "roulette-exports"
    zips = sorted(root.glob("*.zip"))
    if not zips:
        print(f"no exports in {root}")
        return 0
    print(f"{len(zips)} export(s) in {root}{'' if apply else '  (dry run)'}")
    for z in zips:
        print(f"  {z.name:34} {await repair(z, apply)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
