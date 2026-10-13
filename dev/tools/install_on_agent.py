#!/usr/bin/env python3
"""CurseForge modpacks and mods, installed and booted on a node agent.

CurseForge is the primary source, so this is the test that has to pass
against the real thing: real packs, real mod downloads, real servers. Not
part of the offline loop -- it needs your CurseForge key (from the
environment or the gitignored .env, never printed), the network, and time.

    python dev/tools/install_on_agent.py                # the default packs
    python dev/tools/install_on_agent.py --docker       # on real containers
    python dev/tools/install_on_agent.py --pack 885460  # just this one

For each pack: install its newest file through POST /api/install/modpack and
boot it with the smoke test. The default set covers the three CurseForge
loaders and both install paths -- an official server pack, and a manifest
whose mods are downloaded one by one, with client-only mods to strip.

On the first pack, also:
  * add a CurseForge mod that has a required dependency through
    POST /api/instances/{id}/mods/add, check both jars landed, and boot again;
  * export the server as a CurseForge pack, upload that archive as an import,
    install it as a second server, and boot that.
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

DEFAULT_PACKS = [
    1237007,    # Just Create SMP -- NeoForge 1.21.1, ships a server pack
    919384,     # Create: Perfect World -- Forge, ships a server pack
    1168081,    # Vanilla Perfected -- Fabric, manifest only, client-only mods to strip
]
# A server-side mod with a required dependency: Waystones needs Balm.
MOD = {"project_id": 245755, "dependency_slug": "balm", "name": "Waystones"}


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
        row["log"] = [ln["message"] for ln in (snap.get("log") or [])][-12:]
    else:
        for key in ("loader", "minecraft", "mods_installed", "source",
                    "files_fetched_by_node", "files_uploaded"):
            if key in result:
                row[key] = result[key]
    return row


async def add_curseforge_mod(c, sid: str, mc: str, loader: str) -> dict:
    """Add one CurseForge mod, dependencies included, and look for both jars."""
    versions = (await c.get(f"/api/mods/curseforge/{MOD['project_id']}/versions",
                            params={"game_version": mc, "loader": loader})).json()
    items = versions.get("items") or []
    if not items:
        return {"added": False, "why": f"no {MOD['name']} build for {loader} {mc}"}
    file_id = items[0].get("file_id") or items[0].get("id")
    r = await c.post(f"/api/instances/{sid}/mods/add", json={
        "source": "curseforge", "project_id": MOD["project_id"], "file_id": file_id,
        "name": MOD["name"]})
    snap = await follow(c, r.json()["job_id"], 900)
    listing = (await c.get(f"/api/instances/{sid}/files", params={"path": "mods"})).json()
    names = [e.get("name", "") for e in listing.get("entries") or []]
    got_mod = any(MOD["name"].lower() in n.lower() for n in names)
    got_dep = any(MOD["dependency_slug"] in n.lower() for n in names)
    return {"added": snap.get("status") == "done" and got_mod and got_dep,
            "job": snap.get("status"), "mod_jar": got_mod, "dependency_jar": got_dep,
            **({} if snap.get("status") == "done" else {"error": snap.get("error")})}


async def round_trip(c, sid: str, pack: int) -> dict:
    """Export the server as a CurseForge pack, import it back, install, boot."""
    r = await c.post(f"/api/instances/{sid}/export",
                     json={"include_disabled": False, "bundle_others": True})
    exp = await follow(c, r.json()["job_id"], 1800)
    filename = (exp.get("result") or {}).get("filename")
    out: dict = {"export": exp.get("status"), "filename": filename}
    if exp.get("status") != "done" or not filename:
        out["error"] = exp.get("error")
        return out
    archive = (await c.get(f"/api/exports/{filename}")).content
    up = (await c.post("/api/uploads/modpack",
                       files={"file": (filename, archive, "application/zip")})).json()
    upload_id = up.get("upload_id") or up.get("id")
    second = await install(c, {"upload_id": upload_id}, f"Re-import {pack}")
    out["install"] = second
    if second["install"] == "done":
        second["boot"] = await boot(c, second["server_id"])
        await c.request("DELETE", f"/api/instances/{second['server_id']}")
    return out


def _moved(inst: dict) -> str:
    """How the files reached the node: fetched by it from the mod sites
    (fetch_many), or zipped and uploaded by the panel."""
    return (f"{inst.get('files_fetched_by_node')} fetched by the node, "
            f"{inst.get('files_uploaded')} uploaded")


async def one_pack(c, pack: int, first: bool) -> dict:
    files = (await c.get(f"/api/modpacks/{pack}/files")).json()
    newest = (files.get("items") or [{}])[0]
    row: dict = {"pack": pack, "file": newest.get("display_name"),
                 "file_id": newest.get("file_id")}
    print(f"-- pack {pack}: {row['file']}", flush=True)
    inst = await install(c, {"mod_id": pack, "file_id": newest["file_id"]}, f"Pack {pack}")
    row.update(inst)
    print(f"   install: {inst['install']} in {inst['seconds']}s "
          f"({inst.get('loader')} {inst.get('minecraft')}, "
          f"{inst.get('mods_installed')} mods; {_moved(inst)}) "
          f"{inst.get('error') or ''}", flush=True)
    if inst["install"] != "done":
        for line in inst.get("log") or []:
            print("     | " + str(line)[:200], flush=True)
        return row
    sid = inst["server_id"]
    row["boot"] = await boot(c, sid)
    print(f"   boot: {row['boot']['outcome']} in {row['boot']['seconds']}s", flush=True)
    if first and row["boot"]["outcome"] == "ready":
        row["mod"] = await add_curseforge_mod(c, sid, inst.get("minecraft") or "",
                                              inst.get("loader") or "")
        print(f"   add {MOD['name']} from CurseForge: {row['mod']}", flush=True)
        if row["mod"]["added"]:
            row["boot_with_mod"] = await boot(c, sid)
            print(f"   boot with it: {row['boot_with_mod']['outcome']} in "
                  f"{row['boot_with_mod']['seconds']}s", flush=True)
        row["round_trip"] = await round_trip(c, sid, pack)
        rt = row["round_trip"]
        print(f"   export -> import: {rt.get('export')}, install "
              f"{(rt.get('install') or {}).get('install')} "
              f"({(rt.get('install') or {}).get('mods_installed')} mods; "
              f"{_moved(rt.get('install') or {})}), boot "
              f"{((rt.get('install') or {}).get('boot') or {}).get('outcome')}", flush=True)
    elif row["boot"]["outcome"] != "ready":
        print("     | " + row["boot"].get("log_tail", "")[-800:].replace("\n", "\n     | "),
              flush=True)
    await c.request("DELETE", f"/api/instances/{sid}")
    return row


def passed(row: dict) -> bool:
    ok = row.get("install") == "done" and (row.get("boot") or {}).get("outcome") == "ready"
    if "mod" in row:
        ok = ok and row["mod"]["added"] and \
            (row.get("boot_with_mod") or {}).get("outcome") == "ready"
    if "round_trip" in row:
        rt = row["round_trip"].get("install") or {}
        ok = ok and (rt.get("boot") or {}).get("outcome") == "ready"
    return ok


async def main(packs: list[int], runtime: str) -> bool:
    env = secrets_from_dotenv(("CURSEFORGE_API_KEY", "CURSEFORGE_API_KEY_B64"))
    if not (env or os.environ.get("CURSEFORGE_API_KEY")
            or os.environ.get("CURSEFORGE_API_KEY_B64")):
        print("no CurseForge key in the environment or .env")
        return False
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bf-pack-"))
    report = []
    async with stack(tmp, extra_env=env, runtime=runtime) as s:
        print(f"runtime: {runtime}", flush=True)
        for i, pack in enumerate(packs):
            try:
                report.append(await one_pack(s.client, pack, first=(i == 0)))
            except Exception as e:      # noqa: BLE001 -- reported, next pack runs
                report.append({"pack": pack, "install": "crashed", "error": repr(e)})
                print(f"   crashed: {e!r}", flush=True)
    out = tmp / "pack-report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    good = sum(1 for r in report if passed(r))
    print(f"\n{good}/{len(report)} packs passed. Report: {out}")
    return good == len(report)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--pack", type=int, action="append",
                   help="a CurseForge modpack project id (repeatable)")
    p.add_argument("--docker", action="store_true", help="the agent's Docker runtime")
    a = p.parse_args()
    sys.exit(0 if asyncio.run(main(a.pack or DEFAULT_PACKS,
                                   "docker" if a.docker else "process")) else 1)
