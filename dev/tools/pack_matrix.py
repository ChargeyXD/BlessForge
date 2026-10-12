#!/usr/bin/env python3
"""Install and boot a list of real modpacks on a node agent, and say why each
one that fails, fails.

The acceptance test for "a pack runs on boot with no manual setup": every
pack goes through exactly what the UI does -- POST /api/install/modpack
(CurseForge ids) or an uploaded CurseForge export -- and is then booted with
the smoke test. Nothing is touched by hand in between. A pack that does not
reach "Done" gets its crash attributed (GET /crash-review) and the result
written down, pack by pack, so a long run that dies half-way still leaves a
report.

    python dev/tools/pack_matrix.py --docker --mem 5 925200 296062 ./export.zip
    python dev/tools/pack_matrix.py --docker --keep 663737   # leave it installed

Needs your CurseForge key (environment or the gitignored .env; never
printed), the network, and a lot of time: a big pack is gigabytes and
minutes to boot.
"""
import argparse
import asyncio
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from _agentstack import follow, free_port, secrets_from_dotenv, stack  # noqa: E402


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


async def source_for(c, spec: str) -> tuple[dict, str]:
    """The install body for a pack spec: a CurseForge id, or a local export."""
    path = pathlib.Path(spec)
    if path.suffix.lower() == ".zip" and path.exists():
        r = await c.post("/api/uploads/modpack",
                         files={"file": (path.name, path.read_bytes(), "application/zip")})
        r.raise_for_status()
        up = r.json()
        return {"upload_id": up.get("upload_id") or up.get("id")}, path.stem
    pack = int(spec)
    files = (await c.get(f"/api/modpacks/{pack}/files")).json()
    newest = (files.get("items") or [{}])[0]
    return {"mod_id": pack, "file_id": newest["file_id"]}, newest.get("display_name") or spec


async def one(c, spec: str, mem: int, boot_timeout: int, keep: bool,
              reboot: bool = False) -> dict:
    row: dict = {"spec": spec}
    try:
        body, label = await source_for(c, spec)
    except Exception as e:      # noqa: BLE001
        return {**row, "stage": "source", "error": repr(e)}
    row["name"] = label
    log(f"== {label}")
    t0 = time.time()
    r = await c.post("/api/install/modpack", json={
        **body, "server_name": label[:60], "port": free_port(), "mem_max": mem})
    if r.status_code != 200:
        return {**row, "stage": "install", "error": r.text[:400]}
    snap = await follow(c, r.json()["job_id"], 7200)
    result = snap.get("result") or {}
    sid = result.get("server_id") or snap.get("server_id")
    row.update(install=snap.get("status"), install_seconds=int(time.time() - t0),
               server_id=sid,
               **{k: result.get(k) for k in ("loader", "minecraft", "mods_installed",
                                             "source", "repairs", "boot") if k in result})
    row["install_log"] = [ln["message"] for ln in (snap.get("log") or [])
                          if ln.get("level") in ("warn", "error")][-25:]
    log(f"   install {row['install']} in {row['install_seconds']}s: "
        f"{row.get('loader')} {row.get('minecraft')}, {row.get('mods_installed')} mods"
        + (f" -- {snap.get('error')}" if snap.get("error") else ""))
    if snap.get("status") != "done" or not sid:
        row["error"] = snap.get("error")
        return row

    # The install ends with a first boot and its repairs (app/repair.py); that
    # verdict is the one that counts. --reboot boots once more from cold, as
    # an operator pressing Start would.
    first = result.get("boot") or {}
    for rep in first.get("repairs") or []:
        log(f"   repair: {rep['do']} {rep.get('file') or rep.get('name') or ''} -- "
            f"{rep['why'][:150]}")
    if first and not reboot:
        row.update(boot=first.get("outcome"), boot_seconds=first.get("seconds"),
                   boot_rounds=first.get("rounds"), boot_why=first.get("why"),
                   failure_line=first.get("failure_line"))
        log(f"   first boot {row['boot']} after {row['boot_rounds']} attempt(s)"
            + (f" in {row['boot_seconds']}s" if row["boot"] == "ready" else
               f": {first.get('why')}"))
    else:
        t1 = time.time()
        r = await c.post(f"/api/instances/{sid}/smoke-test", json={"timeout": boot_timeout})
        boot = await follow(c, r.json()["job_id"], boot_timeout + 300)
        br = boot.get("result") or {}
        row.update(boot=br.get("outcome") or boot.get("status"),
                   boot_seconds=br.get("seconds") or int(time.time() - t1),
                   failure_line=br.get("failure_line"),
                   findings=[{k: f.get(k) for k in ("severity", "title")}
                             for f in br.get("findings") or []])
        log(f"   boot {row['boot']} in {row['boot_seconds']}s")
    br = {"log_tail": ""}
    if row["boot"] != "ready":
        review = (await c.get(f"/api/instances/{sid}/crash-review")).json()
        row["culprits"] = [{k: x.get(k) for k in ("file", "confidence", "why")}
                           for x in review.get("culprits") or []][:6]
        row["crash_findings"] = [{k: f.get(k) for k in ("severity", "title", "detail")}
                                 for f in review.get("findings") or []][:6]
        ctx = (await c.get(f"/api/instances/{sid}/console", params={"source": "file"})).json()
        row["log_tail"] = "\n".join((ctx.get("lines") or [])[-60:])[-4000:]
        for x in row["culprits"]:
            log(f"   culprit [{x['confidence']}] {x['file']}: {x['why'][:140]}")
        for f in row["crash_findings"]:
            log(f"   finding [{f['severity']}] {f['title']}")
    diag = (await c.get(f"/api/instances/{sid}/diagnose")).json()
    row["health"] = [{k: f.get(k) for k in ("severity", "title")}
                     for f in diag.get("findings") or []][:12]
    # A pack that booted is removed to free the disk; one that did not is
    # kept, folder and all, because its full log and crash report are the
    # evidence for the next fix.
    if not keep and row.get("boot") == "ready":
        await c.request("DELETE", f"/api/instances/{sid}")
    elif row.get("boot") != "ready":
        row["kept_folder"] = sid
        log(f"   kept for inspection: {sid}")
    return row


async def main(a) -> bool:
    env = secrets_from_dotenv(("CURSEFORGE_API_KEY", "CURSEFORGE_API_KEY_B64"))
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bf-matrix-"))
    out = pathlib.Path(a.report or tmp / "matrix-report.json")
    report = []
    async with stack(tmp, extra_env=env, runtime="docker" if a.docker else "process") as s:
        log(f"panel {s.api}, agent {s.agent}, report {out}")
        for spec in a.packs:
            try:
                row = await one(s.client, spec, a.mem, a.boot_timeout, a.keep, a.reboot)
            except Exception as e:      # noqa: BLE001 -- recorded, next pack runs
                row = {"spec": spec, "stage": "crashed", "error": repr(e)}
                log(f"   crashed: {e!r}")
            report.append(row)
            out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if a.keep:
            log("--keep: servers left installed; Ctrl-C (or kill) to stop the stack")
            while True:
                await asyncio.sleep(3600)
    good = sum(1 for r in report if r.get("boot") == "ready")
    log(f"{good}/{len(report)} packs booted. Report: {out}")
    return good == len(report)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("packs", nargs="+", help="CurseForge modpack ids, or export .zip paths")
    p.add_argument("--docker", action="store_true")
    p.add_argument("--mem", type=int, default=5,
                   help="max heap, GB; 0 lets BlessForge size it from the node")
    p.add_argument("--boot-timeout", type=int, default=2400)
    p.add_argument("--keep", action="store_true")
    p.add_argument("--reboot", action="store_true", help="boot again from cold after install")
    p.add_argument("--report")
    sys.exit(0 if asyncio.run(main(p.parse_args())) else 1)
