#!/usr/bin/env python3
"""A CurseForge modpack, and a pack exported and imported back, on a node agent.

Not part of the offline loop: it needs your CurseForge key (from the
environment or the gitignored .env -- never printed), the network, and a
few minutes.

    python dev/tools/install_on_agent.py                 # Just Create SMP
    python dev/tools/install_on_agent.py --pack 885460   # another pack

1. Installs the newest file of a CurseForge modpack through
   POST /api/install/modpack, on the agent engine (dev/tools/_agentstack.py).
2. Boots it with the smoke test.
3. Exports that server as a CurseForge pack (POST /api/instances/{id}/export),
   uploads the archive as an import (POST /api/uploads/modpack), installs
   it as a second server from the upload, and boots that too.

So both halves of WP-06's "one CurseForge pack and one imported CurseForge
export install end to end" run on the node, through the API the UI uses.
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from _agentstack import follow, free_port, secrets_from_dotenv, stack  # noqa: E402

DEFAULT_PACK = 1237007          # Just Create SMP: NeoForge, small, has a server pack


async def boot(c, sid: str) -> dict:
    r = await c.post(f"/api/instances/{sid}/smoke-test", json={"timeout": 900})
    snap = await follow(c, r.json()["job_id"], 1200)
    result = snap.get("result") or {}
    return {"outcome": result.get("outcome") or snap.get("status"),
            "seconds": result.get("seconds"),
            "findings": len(result.get("findings") or []),
            **({} if result.get("outcome") == "ready"
               else {"log_tail": (result.get("log_tail") or "")[-1500:]})}


async def install(c, body: dict, label: str) -> dict:
    t0 = time.time()
    r = await c.post("/api/install/modpack", json={
        **body, "server_name": label, "port": free_port(), "mem_max": 4})
    if r.status_code != 200:
        return {"install": "refused", "error": r.text[:300]}
    snap = await follow(c, r.json()["job_id"], 3600)
    result = snap.get("result") or {}
    row = {"install": snap.get("status"), "seconds": int(time.time() - t0),
           "server_id": result.get("server_id") or snap.get("server_id")}
    if snap.get("status") != "done":
        row["error"] = snap.get("error")
        row["log"] = [ln["message"] for ln in (snap.get("log") or [])][-10:]
    else:
        for key in ("loader", "minecraft", "mods_installed", "mod_count", "source"):
            if key in result:
                row[key] = result[key]
    return row


async def main(pack: int) -> bool:
    env = secrets_from_dotenv(("CURSEFORGE_API_KEY", "CURSEFORGE_API_KEY_B64"))
    if not (env or os.environ.get("CURSEFORGE_API_KEY")
            or os.environ.get("CURSEFORGE_API_KEY_B64")):
        print("no CurseForge key in the environment or .env")
        return False
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bf-pack-"))
    report: dict = {"pack": pack}
    async with stack(tmp, extra_env=env) as s:
        c = s.client
        files = (await c.get(f"/api/modpacks/{pack}/files")).json()
        items = files.get("items") if isinstance(files, dict) else files
        newest = items[0]
        report["file"] = {k: newest.get(k) for k in ("file_id", "display_name", "file_name",
                                                      "minecraft", "loaders")
                          if k in newest}
        print("pack file:", report["file"], flush=True)

        first = await install(c, {"mod_id": pack, "file_id": newest["file_id"]},
                              f"Pack {pack}")
        print("install:", {k: v for k, v in first.items() if k != "log"}, flush=True)
        report["curseforge"] = first
        if first["install"] != "done":
            print("\n".join(first.get("log") or []))
            return _done(tmp, report)
        first["boot"] = await boot(c, first["server_id"])
        print("boot:", first["boot"].get("outcome"), first["boot"].get("seconds"), "s",
              flush=True)

        r = await c.post(f"/api/instances/{first['server_id']}/export",
                         json={"include_disabled": False, "bundle_others": True})
        exp = await follow(c, r.json()["job_id"], 1800)
        filename = (exp.get("result") or {}).get("filename")
        report["export"] = {"status": exp.get("status"), "filename": filename,
                            "error": exp.get("error")}
        print("export:", report["export"], flush=True)
        if exp.get("status") != "done" or not filename:
            return _done(tmp, report)
        archive = (await c.get(f"/api/exports/{filename}")).content
        up = (await c.post("/api/uploads/modpack",
                           files={"file": (filename, archive, "application/zip")})).json()
        upload_id = up.get("upload_id") or up.get("id")
        report["upload"] = {k: up.get(k) for k in ("id", "upload_id", "loader",
                                                   "minecraft", "mod_count", "kind")
                            if k in up}
        print("upload:", report["upload"], flush=True)
        await c.request("DELETE", f"/api/instances/{first['server_id']}")

        second = await install(c, {"upload_id": upload_id}, f"Re-import {pack}")
        print("re-import install:", {k: v for k, v in second.items() if k != "log"},
              flush=True)
        report["imported_export"] = second
        if second["install"] == "done":
            second["boot"] = await boot(c, second["server_id"])
            print("re-import boot:", second["boot"].get("outcome"),
                  second["boot"].get("seconds"), "s", flush=True)
            await c.request("DELETE", f"/api/instances/{second['server_id']}")
        else:
            print("\n".join(second.get("log") or []))
    return _done(tmp, report)


def _done(tmp: pathlib.Path, report: dict) -> bool:
    out = tmp / "pack-report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    ok = all((report.get(k) or {}).get("boot", {}).get("outcome") == "ready"
             for k in ("curseforge", "imported_export"))
    print(f"\n{'BOTH BOOTED' if ok else 'NOT BOTH BOOTED'}. Report: {out}")
    return ok


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--pack", type=int, default=DEFAULT_PACK)
    sys.exit(0 if asyncio.run(main(p.parse_args().pack)) else 1)
