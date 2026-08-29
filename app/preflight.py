"""Pre-install analysis: decide what a pack contains before touching Crafty.

Stripping client-only mods automatically is right most of the time and wrong
occasionally -- some "client" mods are also libraries that server-side mods
link against, and dropping one turns a working pack into a missing-dependency
crash. So the decision is surfaced instead of taken silently: this module
works out which mods look client-only, says *why* for each one, flags any that
other mods in the same pack depend on, and hands the list to the user.

Evidence used, strongest first:
  1. Modrinth's `server_side: unsupported` -- an explicit statement by the author.
  2. The jar's own `fabric.mod.json` "environment": "client".
  3. A curated name list of well-known client mods.
Anything another mod declares as a dependency is downgraded to "keep",
whatever the other signals say.
"""
from __future__ import annotations

import asyncio
import io
import posixpath
import zipfile

import httpx

from app import config, curseforge, jarmeta, modrinth, packs, whitelist
from app.jobs import Job
from app.packs import PackPlan

# How many jars to actually download for metadata inspection during preflight.
# Downloading every mod twice would double install time, so only candidates
# flagged by cheaper signals get fetched.
MAX_INSPECT = 60


# Libraries that only exist on the client. A mod that hard-depends on one of
# these is client-oriented even when it claims otherwise -- which some mods
# do, and which is exactly the case that takes a server down at startup
# despite every declared field looking fine.
CLIENT_ONLY_LIBRARIES = {
    "modmenu", "yet_another_config_lib_v3", "yacl", "yet_another_config_lib",
    "iris", "sodium", "embeddium", "oculus", "optifine", "cloth-config-client",
    "fabric-screen-api-v1", "fabric-key-binding-api-v1", "midnightlib-client",
    "prism", "satin", "iceberg-client", "searchables",
}

# Entrypoint names that only ever run on a client.
CLIENT_ONLY_ENTRYPOINTS = {"modmenu", "client", "rei_client", "emi", "jei_client"}


def _reason_from_name(filename: str) -> str | None:
    if packs.is_client_only_jar(filename):
        return "name matches a known client-only mod"
    return None


def _soft_client_signals(info: dict) -> list[str]:
    """Weaker evidence that a mod is really client-side.

    Deliberately produces "review", never "remove": these signals are
    suggestive, not conclusive, and a wrong removal here costs more than a
    wrong keep.
    """
    signals = []
    deps = {str(d.get("id") or "").lower() for d in info.get("dependencies", [])}
    hits = deps & CLIENT_ONLY_LIBRARIES
    if hits:
        signals.append(
            "requires client-only " + ("libraries" if len(hits) > 1 else "library")
            + ": " + ", ".join(sorted(hits))
        )
    # A ModMenu entrypoint on its own means nothing -- a large share of
    # perfectly good server mods ship one. It only adds weight next to a
    # harder signal.
    entry = set(info.get("entrypoints") or [])
    if signals and "modmenu" in entry:
        signals.append("and ships a ModMenu integration")
    return signals


async def analyse_manifest_pack(
    job: Job,
    plan: PackPlan,
    *,
    inspect_jars: bool = True,
    zf: zipfile.ZipFile | None = None,
) -> dict:
    """Classify every mod in a manifest pack as server-side or client-only.

    Pass `zf` for an imported export: those routinely carry hand-added jars
    in `overrides/mods/` that appear nowhere in the manifest, and a jar the
    review never saw is a jar nobody gets to veto.
    """
    entries = plan.manifest_files
    file_ids = [e["fileID"] for e in entries if e.get("fileID")]
    job.set_step(f"Inspecting {len(file_ids)} mods", 20)

    file_meta = await curseforge.get_files(file_ids)
    mod_ids = {m.get("mod_id") for m in file_meta.values() if m.get("mod_id")}
    projects = await curseforge.get_mods(mod_ids) if mod_ids else {}

    all_mods: list[dict] = []
    for entry in entries:
        fid = entry.get("fileID")
        pid = entry.get("projectID")
        meta = file_meta.get(fid)
        if not meta:
            continue
        fname = meta.get("file_name") or ""
        if not fname.lower().endswith(".jar"):
            continue
        project = projects.get(pid, {})
        item = {
            "project_id": pid,
            "file_id": fid,
            "file_name": fname,
            "name": project.get("name") or fname,
            "logo": project.get("logo"),
            "summary": project.get("summary"),
            "url": project.get("url"),
            "size": meta.get("size"),
            "download_url": meta.get("download_url"),
            "required": entry.get("required", True),
            "reasons": [],
            "confidence": "none",
        }
        reason = _reason_from_name(fname)
        if reason:
            item["reasons"].append(reason)
            item["confidence"] = "name"
        all_mods.append(item)

    # Read EVERY jar, not just the ones whose names look suspicious. A name
    # list only ever confirms what it already guessed, and the mods that
    # actually break servers are usually the ones nobody thought to list --
    # the jar's own "environment": "client" is the authoritative signal.
    # Jars are cached, so the install afterwards reuses these downloads.
    if inspect_jars:
        job.set_step(f"Reading {len(all_mods)} mod jars", 30)
        await _augment_with_jar_metadata(job, all_mods)

    # Jars shipped inside the archive itself. Added after the download pass
    # on purpose: there is nothing to fetch for these, the bytes are already
    # here, but they are classified by exactly the same rules.
    if zf is not None:
        bundled = _scan_bundled_jars(zf, plan)
        if bundled:
            job.log_line(
                f"{len(bundled)} jar(s) are bundled in the export's "
                "overrides/mods and are not listed on CurseForge"
            )
            all_mods.extend(bundled)

    # Ask Modrinth about every mod by hash BEFORE splitting the list. The
    # order is the whole point: a mod nothing else suspected can still be
    # declared client-only by its author, and that is the case that used to
    # sail through the review and crash the server on first boot.
    matched = await _augment_with_modrinth_by_hash(job, all_mods)
    if matched:
        job.log_line(f"Modrinth identified {matched} of {len(all_mods)} jars by "
                     "file hash and stated which side each one runs on")

    flagged = [m for m in all_mods if m["reasons"]]
    keep = [m for m in all_mods if not m["reasons"]]

    # Slug-based fallback for the jars Modrinth could not identify by hash --
    # CurseForge-exclusive mods, mostly. Only ever applied to mods something
    # else already flagged, because a guessed slug can land on the wrong
    # project.
    job.set_step("Checking mod side information", 60)
    await _augment_with_modrinth(flagged)

    cleared = [m for m in flagged if m["confidence"] == "cleared"]
    candidates = [m for m in flagged if m["confidence"] != "cleared"]
    keep.extend(cleared)
    if cleared:
        job.log_line(
            f"{len(cleared)} flagged mods were cleared by Modrinth as "
            "server-required"
        )

    # A mod other mods depend on must never be stripped, however client it
    # looks -- that is exactly how a "tidy up the client mods" pass turns a
    # working pack into a missing-dependency crash. Two sources of truth:
    # CurseForge's declared relations, and the mod ids the jars themselves
    # require.
    depended_on = await _dependency_targets(file_meta)
    needed_mod_ids: dict[str, set[str]] = {}
    for mod in all_mods:
        for dep_id in mod.get("dependencies") or []:
            if dep_id:
                needed_mod_ids.setdefault(dep_id.lower(), set()).add(mod["name"])

    for item in candidates:
        protectors: set[str] = set()
        if item["project_id"] in depended_on:
            protectors |= depended_on[item["project_id"]]
        own_id = (item.get("mod_id") or "").lower()
        if own_id and own_id in needed_mod_ids:
            protectors |= needed_mod_ids[own_id] - {item["name"]}

        if protectors:
            item["required_by_others"] = sorted(protectors)[:5]
            item["reasons"].append(
                "another mod in this pack requires it, so removing it would "
                "break that mod"
            )
            item["recommendation"] = "keep"
        elif item["confidence"] == "contradicted":
            item["recommendation"] = "review"
        else:
            item["recommendation"] = (
                "remove" if item["confidence"] in ("declared", "jar") else "review"
            )

    strong = [c for c in candidates if c["recommendation"] == "remove"]
    review = [c for c in candidates if c["recommendation"] == "review"]
    protected = [c for c in candidates if c["recommendation"] == "keep"]

    job.log_line(
        f"{len(candidates)} possible client-only mods "
        f"({len(strong)} confirmed, {len(review)} uncertain, "
        f"{len(protected)} protected as dependencies)"
    )
    return {
        "total_mods": len(candidates) + len(keep),
        "candidates": strong + review + protected,
        "confirmed": len(strong),
        "uncertain": len(review),
        "protected": len(protected),
        "server_mods": len(keep),
    }


async def _augment_with_modrinth_by_hash(job: Job, items: list[dict]) -> int:
    """Ask Modrinth about EVERY mod, by file hash, before anything is flagged.

    This is the check that was missing, and it is the reason a client-only
    mod like Figura installed cleanly and then took the server down on its
    first start. The old flow only consulted Modrinth for mods some *other*
    signal had already flagged -- so the single most authoritative source we
    have (the author stating `server_side: unsupported`) was never asked
    about the mods nobody had thought to put on a name list.

    Matching is by SHA-1 of the exact jar, not by a slug guessed from a
    display name, so it cannot land on the wrong project. Two bulk requests
    cover a 300-mod pack.
    """
    if not config.MODRINTH_ENABLED:
        return 0
    by_hash = {i["sha1"]: i for i in items if i.get("sha1")}
    if not by_hash:
        return 0

    job.set_step(f"Checking {len(by_hash)} mods against Modrinth", 58)
    hashes = list(by_hash)
    versions: dict[str, dict] = {}
    for start in range(0, len(hashes), 250):
        try:
            versions.update(
                await modrinth.versions_from_hashes(hashes[start: start + 250])
            )
        except Exception:
            continue
    if not versions:
        return 0

    project_ids = {v.get("mod_id") for v in versions.values() if v.get("mod_id")}
    try:
        projects = await modrinth.get_projects(project_ids)
    except Exception:
        return 0

    matched = 0
    for sha, version in versions.items():
        item = by_hash.get(sha)
        project = projects.get(version.get("mod_id"))
        if not item or not project:
            continue
        matched += 1
        item["modrinth_url"] = f"https://modrinth.com/mod/{project.get('slug')}"
        _apply_modrinth_side(item, project.get("server_side"), exact=True)
    return matched


def _apply_modrinth_side(item: dict, side: str | None, *, exact: bool) -> None:
    """Fold Modrinth's declared server support into a candidate's evidence."""
    how = "" if exact else " (matched by name)"
    if side == "unsupported":
        item["reasons"].append(
            f"Modrinth lists this mod as server_side: unsupported{how}")
        item["confidence"] = "declared"
    elif side == "required":
        # The author says the server needs it. That outranks every heuristic
        # we have, so clear the flags entirely -- but only for a mod
        # something actually flagged. Marking all 190 healthy server mods
        # "cleared" would be true and useless, and it made the job log claim
        # it had rescued 169 mods from a review none of them were in.
        if item["reasons"]:
            item["reasons"] = [
                f"Modrinth lists server_side: required — this mod belongs on "
                f"the server{how}"
            ]
            item["confidence"] = "cleared"
    elif side == "optional" and item["reasons"]:
        item["reasons"].append(
            f"Modrinth lists server_side: optional — it may still be needed{how}")
        if item["confidence"] != "declared":
            item["confidence"] = "contradicted"


async def _augment_with_modrinth(candidates: list[dict]) -> None:
    """Ask Modrinth about each candidate by slug; it states the side outright."""
    if not config.MODRINTH_ENABLED or not candidates:
        return
    sem = asyncio.Semaphore(6)

    async def one(item: dict) -> None:
        # A hash match is exact; a slug guessed from a display name is not,
        # so it must never overrule one.
        if item.get("modrinth_url"):
            return
        async with sem:
            # Derive a plausible slug from the display name.
            slug = (item["name"] or "").lower()
            slug = "".join(ch if ch.isalnum() else "-" for ch in slug).strip("-")
            slug = "-".join(p for p in slug.split("-") if p)
            if not slug:
                return
            try:
                project = await modrinth.get_project(slug)
            except Exception:
                return
            if not project:
                return
            _apply_modrinth_side(item, project.get("server_side"), exact=False)

    await asyncio.gather(*(one(c) for c in candidates))


async def _augment_with_jar_metadata(job: Job, items: list[dict]) -> None:
    """Download each jar and read the side it declares."""
    sem = asyncio.Semaphore(config.DOWNLOAD_CONCURRENCY)
    done = 0
    total = len(items)
    lock = asyncio.Lock()

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        async def one(item: dict) -> None:
            nonlocal done
            async with sem:
                try:
                    meta = {"file_id": item["file_id"],
                            "file_name": item["file_name"],
                            "download_url": item.get("download_url"),
                            "size": item.get("size")}
                    # Cache to disk first, then read one jar at a time. The
                    # install afterwards reuses the same file without ever
                    # holding it, so nothing here scales with pack size.
                    cached = await curseforge.cache_jar(meta, client)
                    blob = (cached.read_bytes() if cached
                            else await curseforge.download_cached(meta, client))
                    try:
                        item["sha1"] = modrinth.sha1(blob)
                        _apply_jar_info(item, jarmeta.parse(blob, item["file_name"]))
                    finally:
                        del blob
                except Exception:
                    pass
                finally:
                    async with lock:
                        done += 1
                        if done % 25 == 0 or done == total:
                            job.set_step(
                                f"Reading mod jars ({done}/{total})",
                                30 + 28 * done / max(total, 1),
                            )

        await asyncio.gather(*(one(i) for i in items))


def _apply_jar_info(item: dict, info: dict) -> None:
    """Fold a jar's own metadata into a candidate's evidence."""
    item["mod_id"] = info.get("mod_id")
    item["declares"] = info.get("side")
    item["dependencies"] = [
        d.get("id") for d in info.get("dependencies", []) if d.get("mandatory", True)
    ]
    if info.get("side") == "client":
        item["reasons"].append("the jar declares environment=client")
        item["confidence"] = "jar"
    elif info.get("side_inferred") == "client":
        item["reasons"].append(
            info.get("side_reason") or "only client entrypoints")
        item["confidence"] = "jar"
    else:
        soft = _soft_client_signals(info)
        if soft:
            item["reasons"].extend(soft)
            # Never strong enough to auto-remove: surface it.
            if item["confidence"] in ("none", "name"):
                item["confidence"] = "suspect"
        if info.get("side") in ("both", "server") and item["reasons"]:
            item["reasons"].append(
                f"though the jar claims environment={info['side']}")
            if item["confidence"] == "name":
                item["confidence"] = "contradicted"


def _scan_bundled_jars(zf: zipfile.ZipFile, plan: PackPlan) -> list[dict]:
    """Classify the jars an export carries in `overrides/mods/`.

    These have no project id, so nothing downstream can look them up, check
    them for updates, or ask Modrinth about them by anything but a guessed
    name. The jar's own manifest is all the evidence there is -- which is
    still the strongest signal we use anywhere.
    """
    items: list[dict] = []
    for entry in packs.overlay_jars(plan):
        name = posixpath.basename(entry["target"])
        item = {
            "project_id": None,
            "file_id": None,
            "file_name": name,
            "name": name,
            "bundled": True,
            "size": None,
            "reasons": [],
            "confidence": "none",
        }
        if _reason_from_name(name):
            item["reasons"].append("name matches a known client-only mod")
            item["confidence"] = "name"
        try:
            blob = zf.read(entry["member"])
            item["size"] = len(blob)
            item["sha1"] = modrinth.sha1(blob)
            info = jarmeta.parse(blob, name)
            item["name"] = info.get("name") or name
            _apply_jar_info(item, info)
        except Exception:
            pass
        items.append(item)
    return items


async def _dependency_targets(file_meta: dict) -> dict[int, set[str]]:
    """Map project id -> names of mods in this pack that require it."""
    targets: dict[int, set[str]] = {}
    for meta in file_meta.values():
        for dep in meta.get("dependencies") or []:
            if dep.get("relationType") == 3 and dep.get("modId"):
                targets.setdefault(int(dep["modId"]), set()).add(
                    meta.get("file_name") or str(meta.get("mod_id"))
                )
    return targets


def decide_with_protection(candidates: list[dict], all_jars: list[dict]) -> None:
    """Turn flags into recommendations, sparing anything depended on.

    A mod other mods depend on must never be stripped, however client it
    looks -- that is exactly how a "tidy up the client mods" pass turns a
    working pack into a missing-dependency crash. The manifest path had this
    and the server-pack path did not, and the gap is not theoretical: Athena
    is declared client-only by its own author and is a hard requirement of
    Chipped, which is not, so a confident "remove" took a working pack to
    "requires version 4.0.0 or later of athena, which is missing".

    Only jars that are staying get a vote. One client-only mod requiring
    another is not a reason to keep either of them.
    """
    flagged = {c["file_name"] for c in candidates}
    needed: dict[str, set[str]] = {}
    for mod in all_jars:
        if mod["file_name"] in flagged:
            continue
        for dep_id in mod.get("dependencies") or []:
            needed.setdefault(str(dep_id).lower(), set()).add(mod["name"])

    allowed = whitelist.allowed_set()
    for item in candidates:
        # A decision the operator already made about this mod, on any server.
        # "server_side: unsupported" often means "adds nothing on a server",
        # not "breaks one", and there has to be a way to say so once.
        if whitelist._key(item["file_name"]) in allowed:
            item["recommendation"] = "keep"
            item["whitelisted"] = True
            item["reasons"].append(
                "you marked this mod as safe on a server, so the review leaves "
                "it enabled"
            )
            continue
        own_id = (item.get("mod_id") or "").lower()
        protectors = needed.get(own_id, set()) - {item["name"]}
        if protectors:
            item["required_by_others"] = sorted(protectors)[:5]
            item["reasons"].append(
                "another mod in this pack requires it, so removing it would "
                "break that mod"
            )
            item["recommendation"] = "keep"
        elif item["confidence"] == "contradicted":
            item["recommendation"] = "review"
        else:
            item["recommendation"] = (
                "remove" if item["confidence"] in ("declared", "jar") else "review"
            )


async def analyse_server_pack_jars(job: Job, zf: zipfile.ZipFile, plan: PackPlan
                                   ) -> dict:
    """Same idea for a server pack, where the jars are already in hand."""
    all_jars: list[dict] = []
    total = 0
    for entry in plan.overlay_members:
        target = entry["target"]
        if not (target.startswith("mods/") and target.endswith(".jar")):
            continue
        total += 1
        name = target.split("/")[-1]
        reasons = []
        confidence = "none"
        sha = None
        try:
            blob = zf.read(entry["member"])
            sha = modrinth.sha1(blob)
            info = jarmeta.parse(blob, name)
            if info.get("side") == "client":
                reasons.append("the jar declares environment=client")
                confidence = "jar"
        except Exception:
            info = {}
        if _reason_from_name(name):
            reasons.append("name matches a known client-only mod")
            confidence = confidence or "name"
        # Held for the hash pass below whether or not anything flagged it:
        # the author's own statement is what catches the ones no heuristic
        # spotted.
        all_jars.append({
            "file_name": name,
            "name": info.get("name") or name,
            "mod_id": info.get("mod_id"),
            "sha1": sha,
            "reasons": reasons,
            "confidence": confidence,
            "size": None,
            # The ids this jar says it needs, straight out of fabric.mod.json
            # or neoforge.mods.toml. A server pack has no CurseForge relations
            # to consult, so the jars' own statements are the only source.
            "dependencies": [
                d.get("id") for d in (info.get("dependencies") or [])
                if d.get("mandatory", True) and d.get("id")
            ],
        })

    matched = await _augment_with_modrinth_by_hash(job, all_jars)
    if matched:
        job.log_line(f"Modrinth identified {matched} of {total} jars by file "
                     "hash and stated which side each one runs on")

    candidates = [
        item for item in all_jars
        if item["reasons"] and item["confidence"] != "cleared"
    ]

    decide_with_protection(candidates, all_jars)

    protected = sum(1 for c in candidates if c["recommendation"] == "keep")
    job.log_line(
        f"{len(candidates)} possible client-only jars in the server pack"
        + (f" ({protected} held back as dependencies)" if protected else "")
    )
    return {
        "total_mods": total,
        "candidates": candidates,
        "confirmed": sum(1 for c in candidates if c["recommendation"] == "remove"),
        "uncertain": sum(1 for c in candidates if c["recommendation"] == "review"),
        "protected": protected,
        "server_mods": total - len(candidates),
    }
