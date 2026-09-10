"""Pre-install analysis: decide what a pack contains before touching Crafty.

Stripping client-only mods automatically is right most of the time and wrong
occasionally -- some "client" mods are also libraries that server-side mods
link against, and dropping one turns a working pack into a missing-dependency
crash. So the decision is surfaced instead of taken silently: this module
works out which mods look client-only, says *why* for each one, flags any that
other mods in the same pack depend on, and hands the list to the user.

The judgement itself lives in `app/clientscan.py` and is shared with the scan
that runs against an already-installed instance. That sharing is the point:
the two used to be separate implementations that disagreed, so a pack could
install cleanly and then be told, by the same application, that six of its
mods were client-only.

Both entry points here do the same four things:

  1. read every jar (cached, so the install afterwards re-uses the download);
  2. ask Modrinth about every jar at once, by SHA-1, before anything is
     flagged -- the mods that break servers are the ones nobody thought to
     put on a name list;
  3. score each one on the shared evidence axis;
  4. apply the operator's own allow/block decisions, then rescue anything a
     *staying* mod hard-requires.
"""
from __future__ import annotations

import asyncio
import posixpath
import zipfile

import httpx

from app import clientscan, config, curseforge, packs
from app.jobs import Job
from app.packs import PackPlan

# Verdict -> the word the installer and the UI have always spoken.
_RECOMMENDATION = {
    "client": "remove",
    "review": "review",
    "keep": "keep",
    "server": "keep",
}


def _as_candidate(item: dict) -> dict:
    """Shape one scored jar the way the install flow expects it."""
    out = clientscan.strip_internals(item)
    out["recommendation"] = _RECOMMENDATION[item["verdict"]]
    # `reasons` is what the review renders; keep the counter-evidence next to
    # it rather than dropping it, because "we thought client, but..." is the
    # most useful thing a review can say.
    out["reasons"] = item.get("reasons") or []
    out["confidence"] = item.get("confidence")
    return out


def _split(items: list[dict]) -> dict:
    candidates = [_as_candidate(i) for i in items if i["verdict"] != "server"]
    order = {"remove": 0, "review": 1, "keep": 2}
    candidates.sort(key=lambda c: (order[c["recommendation"]], -c["score"]))
    summary = clientscan.summarise(items)
    return {
        "total_mods": summary["total"],
        "candidates": candidates,
        "confirmed": summary["client"],
        "uncertain": summary["review"],
        "protected": summary["protected"],
        "server_mods": summary["server"],
        "scanned": sum(1 for i in items if i.get("readable")),
        "unreadable": [i["file_name"] for i in items if not i.get("readable")],
    }


async def analyse_manifest_pack(
    job: Job,
    plan: PackPlan,
    *,
    inspect_jars: bool = True,
    zf: zipfile.ZipFile | None = None,
) -> dict:
    """Classify every mod in a manifest pack as server-side or client-only.

    Pass `zf` for an imported export: those routinely carry hand-added jars in
    `overrides/mods/` that appear nowhere in the manifest, and a jar the
    review never saw is a jar nobody gets to veto.
    """
    entries = plan.manifest_files
    file_ids = [e["fileID"] for e in entries if e.get("fileID")]
    job.set_step(f"Reading {len(file_ids)} mods", 20)

    file_meta = await curseforge.get_files(file_ids)
    mod_ids = {m.get("mod_id") for m in file_meta.values() if m.get("mod_id")}
    projects = await curseforge.get_mods(mod_ids) if mod_ids else {}

    wanted: list[dict] = []
    for entry in entries:
        fid, pid = entry.get("fileID"), entry.get("projectID")
        meta = file_meta.get(fid)
        if not meta:
            continue
        fname = meta.get("file_name") or ""
        if not fname.lower().endswith(".jar"):
            continue
        project = projects.get(pid, {})
        wanted.append({
            "file_id": fid,
            "project_id": pid,
            "file_name": fname,
            "name": project.get("name") or fname,
            "logo": project.get("logo"),
            "summary": project.get("summary"),
            "url": project.get("url"),
            "categories": project.get("categories") or [],
            "size": meta.get("size"),
            "download_url": meta.get("download_url"),
            "required": entry.get("required", True),
        })

    items: list[dict] = []
    if inspect_jars and wanted:
        items = await _read_catalogue_jars(job, wanted)
    else:
        items = [
            clientscan.build_item(
                w["file_name"], None, name=w["name"],
                project_id=w["project_id"], categories=w["categories"],
                logo=w["logo"],
                extra={"file_id": w["file_id"], "size": w["size"],
                       "url": w.get("url"), "summary": w.get("summary"),
                       "required": w.get("required", True)},
            )
            for w in wanted
        ]

    # Jars shipped inside the archive itself. Added after the download pass
    # on purpose -- there is nothing to fetch for these, the bytes are already
    # here -- but classified by exactly the same rules.
    if zf is not None:
        bundled = _bundled_items(zf, plan)
        if bundled:
            job.log_line(
                f"{len(bundled)} jar(s) are bundled in the export's "
                "overrides/mods and are not listed on CurseForge")
            items.extend(bundled)

    await clientscan.score_all(job, items)
    result = _split(items)
    job.log_line(
        f"{result['candidates'] and len(result['candidates']) or 0} possible "
        f"client-only mods ({result['confirmed']} confirmed, "
        f"{result['uncertain']} uncertain, {result['protected']} protected as "
        f"dependencies)")
    return result


async def _read_catalogue_jars(job: Job, wanted: list[dict]) -> list[dict]:
    """Download (and cache) every jar, then build a scored item from each.

    Every jar, not just the ones whose names look suspicious: a name list only
    ever confirms what it already guessed. Jars are cached to disk and read
    one at a time, so the install afterwards reuses these downloads and
    nothing here scales with pack size.
    """
    sem = asyncio.Semaphore(config.DOWNLOAD_CONCURRENCY)
    items: list[dict] = []
    lock = asyncio.Lock()
    done = 0
    total = len(wanted)
    job.set_step(f"Reading {total} mod jars", 30)

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        async def one(w: dict) -> None:
            nonlocal done
            blob = None
            async with sem:
                try:
                    meta = {"file_id": w["file_id"],
                            "file_name": w["file_name"],
                            "download_url": w.get("download_url"),
                            "size": w.get("size")}
                    cached = await curseforge.cache_jar(meta, client)
                    blob = (cached.read_bytes() if cached
                            else await curseforge.download_cached(meta, client))
                except Exception:
                    blob = None
                finally:
                    async with lock:
                        done += 1
                        if done % 25 == 0 or done == total:
                            job.set_step(
                                f"Reading mod jars ({done}/{total})",
                                30 + 28 * done / max(total, 1))
            try:
                item = clientscan.build_item(
                    w["file_name"], blob, name=w["name"],
                    project_id=w["project_id"], categories=w["categories"],
                    logo=w["logo"],
                    extra={"file_id": w["file_id"], "size": w["size"],
                           "url": w.get("url"), "summary": w.get("summary"),
                           "required": w.get("required", True)},
                )
                items.append(item)
            finally:
                if blob is not None:
                    del blob

        await asyncio.gather(*(one(w) for w in wanted))
    return items


def _bundled_items(zf: zipfile.ZipFile, plan: PackPlan) -> list[dict]:
    """Classify the jars an export carries in `overrides/mods/`.

    These have no project id, so nothing downstream can look them up or check
    them for updates. The jar's own contents are all the evidence there is --
    which is still the strongest signal used anywhere here.
    """
    out: list[dict] = []
    for entry in packs.overlay_jars(plan):
        name = posixpath.basename(entry["target"])
        blob = None
        try:
            blob = zf.read(entry["member"])
        except Exception:
            blob = None
        out.append(clientscan.build_item(
            name, blob, extra={"bundled": True, "member": entry["member"],
                               "size": len(blob) if blob else None}))
        if blob is not None:
            del blob
    return out


async def analyse_server_pack_jars(job: Job, zf: zipfile.ZipFile,
                                   plan: PackPlan) -> dict:
    """Same analysis for a server pack, where the jars are already in hand."""
    items: list[dict] = []
    total = 0
    for entry in plan.overlay_members:
        target = entry["target"]
        if not (target.startswith("mods/") and target.endswith(".jar")):
            continue
        total += 1
        name = target.split("/")[-1]
        blob = None
        try:
            blob = zf.read(entry["member"])
        except Exception:
            blob = None
        items.append(clientscan.build_item(
            name, blob, extra={"member": entry["member"],
                               "size": len(blob) if blob else None}))
        if blob is not None:
            del blob
        if total % 40 == 0:
            job.set_step(f"Reading server-pack jars ({total})",
                         min(58, 30 + total / 8))

    await clientscan.score_all(job, items)
    result = _split(items)
    job.log_line(
        f"{len(result['candidates'])} possible client-only jars in the server "
        f"pack"
        + (f" ({result['protected']} held back as dependencies)"
           if result["protected"] else ""))
    return result


# Kept because the installer imports it by name for the no-review path, where
# it judges a single jar with nothing else to compare against.
def quick_verdict(file_name: str, blob: bytes | None) -> dict:
    item = clientscan.build_item(file_name, blob)
    clientscan.score_item(item)
    clientscan.apply_overrides([item])
    return clientscan.strip_internals(item)
