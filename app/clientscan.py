"""Deciding whether a jar has anything to do on a server.

The old detector asked three questions -- does Modrinth say `server_side:
unsupported`, does `fabric.mod.json` say `environment: client`, is the name on
a list -- and turned each into a yes/no. That works for Fabric mods and is
close to useless for Forge and NeoForge, which declare no side at all: roughly
half of every real pack was judged on its filename.

This is the same evidence plus four sources that do work for Forge, weighed
rather than switched:

  **package layout**   What fraction of a jar's classes live under a
                       `client` package. A mod that is 100% `.../client/...`
                       has nothing to run on a server, and this is true for
                       every loader. Costs nothing: only the zip's central
                       directory is read.
  **mixin targets**    Fabric and Forge both declare mixins per environment.
                       A jar whose every mixin config is client-only is
                       client-only, whatever its manifest says.
  **client libraries** A hard dependency on YACL, ModMenu, Sodium, Iris and
                       friends. A mod cannot be server-side and require a
                       library that only exists on a client.
  **content shape**    `assets/` with no `data/`, no recipes, no loot
                       tables. Weak on its own; useful next to the others.

Everything is scored on one axis, and the score is *explained*: each finding
carries the points it contributed and the sentence behind it, so a verdict can
be argued with rather than only obeyed. Three rules override the score
outright, because each is a fact rather than an inference:

  1. An operator decision (the allow/block list) wins. Always.
  2. A mod another *staying* mod hard-requires is never removed -- that is
     precisely how a tidy-up turns a working pack into a missing-dependency
     crash.
  3. The author saying `server_side: required` outranks every heuristic here.

Verdicts are `server`, `review` and `client`; only `client` is ticked by
default, and even then the jar is disabled rather than deleted.
"""
from __future__ import annotations

import asyncio
import io
import posixpath
import re
import zipfile
from typing import Any, Iterable

from app import config, crafty, curseforge, jarmeta, modrinth, whitelist
from app.jobs import Job

# Score at or above this is acted on; between the two it is surfaced for a
# human. Calibrated so that a single hard signal (a declared environment, an
# all-client package tree) reaches CLIENT on its own, and two soft ones reach
# REVIEW without reaching CLIENT.
CLIENT_AT = 70
REVIEW_AT = 30

# Libraries that exist only on a client. A mod that hard-depends on one is
# client-oriented even when it claims otherwise -- which some do, and which is
# exactly the case that takes a server down at startup with every declared
# field looking fine.
CLIENT_LIBRARIES = {
    "modmenu", "yet_another_config_lib_v3", "yet_another_config_lib", "yacl",
    "iris", "sodium", "embeddium", "oculus", "optifine", "rubidium",
    "cloth-config-client", "fabric-screen-api-v1", "fabric-key-binding-api-v1",
    "fabric-rendering-v1", "fabric-rendering-fluids-v1",
    "fabric-blockrenderlayer-v1", "midnightlib-client", "satin", "prism",
    "iceberg-client", "searchables", "resourcefullib-client", "puzzleslib-client",
    "mixinextras-client", "renderer", "immediatelyfast", "entityculling",
}

# Fabric entrypoint names that only ever run client-side.
CLIENT_ENTRYPOINTS = {
    "client", "modmenu", "emi", "rei_client", "jei_client", "clothconfig",
    "fabric-client-tags-api-v1", "prelaunchclient", "preLaunchClient",
    "client_init",
}
SERVER_ENTRYPOINTS = {"server", "main", "dedicated_server", "init"}

# CurseForge / Modrinth categories that lean client. Never decisive: plenty
# of "Map and Information" mods have a real server half.
# Deliberately *not* including "performance"/"optimization": Lithium,
# ModernFix and FerriteCore are filed there and are server mods, and a hint
# that fires on the whole performance category is worse than no hint.
CLIENT_CATEGORIES = {
    "cosmetic", "map and information", "shaders", "resource packs",
    "visual", "user interface",
}

_CLASS = re.compile(r"\.class$", re.I)
_CLIENT_PKG = re.compile(r"(^|/)client(/|$)", re.I)
_SERVER_PKG = re.compile(r"(^|/)(server|common|api|data|world|block|item|"
                         r"entity|recipe|network|command)(/|$)", re.I)


def _finding(points: int, why: str, source: str) -> dict:
    return {"points": points, "why": why, "source": source}


# --- the cheap structural pass -----------------------------------------


def inspect_archive(blob: bytes) -> dict:
    """Structural facts about a jar, from its central directory and mixins.

    Deliberately separate from `jarmeta.parse`: that reads what the mod
    *declares*, this reads what the jar *contains*, and the interesting mods
    are the ones where those two disagree.
    """
    out: dict[str, Any] = {
        "classes": 0, "client_classes": 0, "client_ratio": 0.0,
        "has_assets": False, "has_data": False, "has_recipes": False,
        "has_loot": False, "mixin_configs": [], "client_only_mixins": None,
        "readable": False, "shades_client_lib": False,
    }
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except Exception:
        return out
    with z:
        try:
            names = z.namelist()
        except Exception:
            return out
        out["readable"] = True
        mixin_names = []
        for name in names:
            lowered = name.lower()
            if _CLASS.search(lowered):
                # A shaded library's classes are not this mod's own code and
                # would otherwise drown the signal in a fat jar.
                if lowered.startswith(("com/google/", "org/apache/",
                                       "kotlin/", "com/mojang/",
                                       "org/jetbrains/", "javax/", "org/slf4j/")):
                    continue
                out["classes"] += 1
                if _CLIENT_PKG.search(posixpath.dirname(lowered)):
                    out["client_classes"] += 1
                continue
            if lowered.startswith("assets/"):
                out["has_assets"] = True
            elif lowered.startswith("data/"):
                out["has_data"] = True
                if "/recipes/" in lowered or "/recipe/" in lowered:
                    out["has_recipes"] = True
                if "/loot_table" in lowered:
                    out["has_loot"] = True
            if lowered.endswith(".mixins.json") or (
                "mixin" in lowered and lowered.endswith(".json")
                and "/" not in name
            ):
                mixin_names.append(name)

        if out["classes"]:
            out["client_ratio"] = out["client_classes"] / out["classes"]

        configs = []
        for name in mixin_names[:12]:
            try:
                import json
                raw = z.read(name).decode("utf-8", "replace")
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except Exception:
                continue
            common = [m for m in (data.get("mixins") or []) if m]
            client = [m for m in (data.get("client") or []) if m]
            server = [m for m in (data.get("server") or []) if m]
            configs.append({
                "file": name, "common": len(common), "client": len(client),
                "server": len(server),
            })
        out["mixin_configs"] = configs
        if configs:
            total = sum(c["common"] + c["client"] + c["server"] for c in configs)
            client_only = sum(c["client"] for c in configs)
            out["client_only_mixins"] = bool(total) and client_only == total
    return out


# --- scoring -----------------------------------------------------------


def evaluate(*, file_name: str, meta: dict | None = None,
             archive: dict | None = None,
             modrinth_side: str | None = None,
             modrinth_client_side: str | None = None,
             categories: Iterable[str] | None = None,
             exact_hash_match: bool = False,
             name_listed: bool = False) -> dict:
    """Score one jar. Returns `{score, verdict, findings, ...}`.

    `meta` is `jarmeta.parse` output; `archive` is `inspect_archive` output.
    Either may be missing -- a jar that could not be downloaded is judged on
    whatever is left, and says so rather than being quietly called safe.
    """
    meta = meta or {}
    archive = archive or {}
    findings: list[dict] = []
    score = 0
    decisive: str | None = None

    how = "" if exact_hash_match else " (matched by name, not by file hash)"

    # 1. What the author says on Modrinth. The strongest evidence we have,
    #    in both directions.
    if modrinth_side == "unsupported":
        score += 100
        findings.append(_finding(
            100, f"its author lists server_side: unsupported{how}", "modrinth"))
    elif modrinth_side == "required":
        decisive = "server"
        findings.append(_finding(
            -100, f"its author lists server_side: required — this mod belongs "
                  f"on the server{how}", "modrinth"))
    elif modrinth_side == "optional":
        if modrinth_client_side == "required":
            score += 30
            findings.append(_finding(
                30, f"its author lists server_side: optional and "
                    f"client_side: required{how}", "modrinth"))
        else:
            score += 10
            findings.append(_finding(
                10, f"its author lists server_side: optional{how}", "modrinth"))

    # 2. What the jar declares about itself.
    side = meta.get("side")
    if side == "client":
        score += 100
        findings.append(_finding(100, "the jar declares environment=client",
                                 "jar"))
    elif side == "server":
        decisive = decisive or "server"
        findings.append(_finding(-100, "the jar declares environment=server",
                                 "jar"))
    elif side == "both":
        score -= 25
        findings.append(_finding(
            -25, "the jar declares environment=* (both sides)", "jar"))

    # 3. Entrypoints -- what the loader is actually asked to run.
    entry = {e.lower() for e in (meta.get("entrypoints") or [])}
    if entry:
        client_entries = entry & {e.lower() for e in CLIENT_ENTRYPOINTS}
        server_entries = entry & SERVER_ENTRYPOINTS
        if client_entries and not server_entries:
            score += 60
            findings.append(_finding(
                60, "every entrypoint it registers is a client one ("
                    + ", ".join(sorted(client_entries)) + ")", "jar"))
        elif server_entries:
            score -= 40
            findings.append(_finding(
                -40, "it registers a server entrypoint ("
                     + ", ".join(sorted(server_entries)) + ")", "jar"))

    # 4. Package layout. Works on every loader, which is the point.
    classes = archive.get("classes") or 0
    ratio = archive.get("client_ratio") or 0.0
    if classes >= 8:
        if ratio >= 0.95:
            score += 75
            findings.append(_finding(
                75, f"all {classes} of its classes live under a client "
                    f"package — there is no server-side code in this jar",
                "package-layout"))
        elif ratio >= 0.75:
            score += 40
            findings.append(_finding(
                40, f"{ratio * 100:.0f}% of its classes are under a client "
                    f"package", "package-layout"))
        elif ratio <= 0.15:
            score -= 25
            findings.append(_finding(
                -25, f"only {ratio * 100:.0f}% of its classes are client "
                     f"code, so most of it runs on both sides",
                "package-layout"))

    # 5. Mixins -- where a mod actually patches the game.
    if archive.get("client_only_mixins"):
        score += 45
        findings.append(_finding(
            45, "every mixin it applies is registered client-only", "mixins"))
    elif archive.get("mixin_configs"):
        server_mixins = sum(c["common"] + c["server"]
                            for c in archive["mixin_configs"])
        if server_mixins:
            score -= 30
            findings.append(_finding(
                -30, f"it applies {server_mixins} mixin(s) that run on the "
                     f"server", "mixins"))

    # 6. Hard dependencies on client-only libraries.
    deps = {str(d.get("id") or "").lower()
            for d in (meta.get("dependencies") or [])
            if d.get("mandatory", True)}
    hits = deps & CLIENT_LIBRARIES
    if hits:
        score += 40
        findings.append(_finding(
            40, "it requires client-only "
                + ("libraries" if len(hits) > 1 else "library") + ": "
                + ", ".join(sorted(hits)), "dependencies"))

    # 7. Content shape. Weak, and treated as weak.
    if archive.get("readable"):
        if archive.get("has_data") or archive.get("has_recipes"):
            score -= 15
            findings.append(_finding(
                -15, "it ships datapack content (recipes or loot tables), "
                     "which only the server reads", "content"))
        elif archive.get("has_assets") and classes:
            score += 8
            findings.append(_finding(
                8, "it ships assets but no datapack content", "content"))

    # 8. The catalogue's own filing, and the curated name list. Both are
    #    hints about a project rather than facts about a jar.
    cats = {str(c).lower() for c in (categories or [])}
    leaning = cats & CLIENT_CATEGORIES
    if leaning:
        score += 12
        findings.append(_finding(
            12, "it is filed under " + ", ".join(sorted(leaning)),
            "catalogue"))
    if name_listed:
        # Exactly enough on its own to reach REVIEW and no further: the list
        # is a curated set of well-known client mods, which makes it worth
        # surfacing and never worth acting on unasked.
        score += REVIEW_AT
        findings.append(_finding(
            REVIEW_AT, "its name matches a known client-only mod",
            "name-list"))

    # 9. A jar we could not read at all is not evidence of anything.
    if not meta and not archive.get("readable"):
        findings.append(_finding(
            0, "this jar could not be downloaded or opened, so only its name "
               "and catalogue entry were available", "unreadable"))

    if decisive == "server":
        verdict, score = "server", min(score, 0)
    elif score >= CLIENT_AT:
        verdict = "client"
    elif score >= REVIEW_AT:
        verdict = "review"
    else:
        verdict = "server"

    findings.sort(key=lambda f: -abs(f["points"]))
    return {
        "score": score,
        "verdict": verdict,
        "confidence": _confidence(score, findings),
        "findings": findings,
        "reasons": [f["why"] for f in findings if f["points"] > 0],
        "counter_reasons": [f["why"] for f in findings if f["points"] < 0],
    }


def _confidence(score: int, findings: list[dict]) -> str:
    hard = [f for f in findings if abs(f["points"]) >= 60]
    if hard:
        return "high"
    if abs(score) >= 45 and len(findings) >= 2:
        return "medium"
    return "low"


# --- protection and operator decisions ---------------------------------


def apply_overrides(items: list[dict], *, server_id: str | None = None) -> None:
    """Fold in the allow/block list and dependency protection, in place.

    Order matters and is the whole of the correctness here:

      1. A block makes it client, whatever the score.
      2. An allow makes it server, whatever the score -- including over a
         block-shaped score, because the operator has seen this mod run.
      3. Protection then rescues anything a *staying* mod requires.

    Only jars that are staying get a vote in step 3. One client-only mod
    requiring another is not a reason to keep either: EMF and ETF are both
    confirmed client-side renderers whose only dependents are other
    client-side mods being disabled in the same pass, and protecting them
    locked on the two mods the user most wanted off.
    """
    for item in items:
        decision = whitelist.decide(
            item["file_name"], mod_id=item.get("mod_id"),
            project_id=item.get("project_id") or item.get("modrinth_id"),
            server_id=server_id,
        )
        if not decision:
            continue
        item["operator"] = decision
        if decision["verdict"] == whitelist.BLOCK:
            item["verdict"] = "client"
            item["confidence"] = "high"
            item["reasons"] = [
                "you marked this mod as client-only, so it is always disabled"
            ] + item.get("reasons", [])
        else:
            item["verdict"] = "server"
            item["whitelisted"] = True
            item["reasons"] = [
                "you marked this mod as safe on a server, so the review "
                "leaves it enabled"
            ]

    staying = [i for i in items if i["verdict"] == "server"]
    provided_by_staying: dict[str, set[str]] = {}
    for mod in staying:
        for dep in (mod.get("hard_dependencies") or []):
            provided_by_staying.setdefault(str(dep).lower(), set()).add(
                mod.get("name") or mod["file_name"])

    # A jar satisfies its own mod id and everything it declares under
    # `provides` or ships nested, which is how modern packs carry libraries.
    for item in items:
        if item["verdict"] == "server" or item.get("whitelisted"):
            continue
        ids = {str(i).lower() for i in
               ([item.get("mod_id")] + list(item.get("provides") or []))
               if i}
        protectors: set[str] = set()
        for mid in ids:
            protectors |= provided_by_staying.get(mid, set())
        protectors -= {item.get("name") or item["file_name"]}
        if not protectors:
            continue
        item["required_by_others"] = sorted(protectors)[:5]
        item["verdict"] = "keep"
        item["reasons"] = [
            f"{', '.join(sorted(protectors)[:3])} in this pack requires it, "
            "so removing it would break "
            + ("them" if len(protectors) > 1 else "that mod")
        ] + item.get("reasons", [])


def summarise(items: list[dict]) -> dict:
    by = {"client": 0, "review": 0, "keep": 0, "server": 0}
    for i in items:
        by[i["verdict"]] = by.get(i["verdict"], 0) + 1
    return {
        "total": len(items),
        "client": by["client"], "review": by["review"],
        "protected": by["keep"], "server": by["server"],
        "candidates": by["client"] + by["review"] + by["keep"],
    }


# --- running it over a set of jars -------------------------------------


async def _modrinth_by_hash(job: Job | None, items: list[dict]) -> int:
    """Ask Modrinth about every jar at once, by SHA-1 of the exact file.

    Before anything is flagged, and for every jar rather than only the
    suspicious ones -- the mods that take a server down are the ones nobody
    thought to put on a name list. Matching by hash cannot land on the wrong
    project the way a slug guessed from a display name can.
    """
    if not config.MODRINTH_ENABLED:
        return 0
    by_hash = {i["sha1"]: i for i in items if i.get("sha1")}
    if not by_hash:
        return 0
    if job:
        job.set_step(f"Asking Modrinth about {len(by_hash)} jars", 62)
    hashes = list(by_hash)
    versions: dict[str, dict] = {}
    for start in range(0, len(hashes), 250):
        try:
            versions.update(
                await modrinth.versions_from_hashes(hashes[start:start + 250]))
        except Exception:
            continue
    if not versions:
        return 0
    try:
        projects = await modrinth.get_projects(
            {v.get("mod_id") for v in versions.values() if v.get("mod_id")})
    except Exception:
        return 0
    matched = 0
    for sha, version in versions.items():
        item = by_hash.get(sha)
        project = projects.get(version.get("mod_id"))
        if not item or not project:
            continue
        matched += 1
        item["modrinth_id"] = project.get("id")
        item["modrinth_side"] = project.get("server_side")
        item["modrinth_client_side"] = project.get("client_side")
        item["modrinth_url"] = (
            f"https://modrinth.com/mod/{project.get('slug')}")
        item["exact_hash_match"] = True
        if not item.get("logo"):
            item["logo"] = project.get("logo")
    return matched


def build_item(file_name: str, blob: bytes | None, *,
               name: str | None = None, project_id: Any = None,
               categories: Iterable[str] | None = None,
               logo: str | None = None, extra: dict | None = None) -> dict:
    """Turn one jar into a scored candidate."""
    from app import packs

    meta: dict = {}
    archive: dict = {}
    sha = None
    if blob:
        sha = modrinth.sha1(blob)
        try:
            meta = jarmeta.parse(blob, file_name)
        except Exception:
            meta = {}
        try:
            archive = inspect_archive(blob)
        except Exception:
            archive = {}

    item = {
        "file_name": file_name,
        "name": name or meta.get("name") or file_name,
        "project_id": project_id,
        "mod_id": meta.get("mod_id"),
        "provides": meta.get("provides") or [],
        "hard_dependencies": [
            d.get("id") for d in (meta.get("dependencies") or [])
            if d.get("mandatory", True) and d.get("id")
        ],
        "loader": meta.get("loader"),
        "version": meta.get("version"),
        "declares": meta.get("side"),
        "sha1": sha,
        "size": len(blob) if blob else None,
        "logo": logo,
        "categories": list(categories or []),
        "classes": archive.get("classes"),
        "client_ratio": round(archive.get("client_ratio") or 0.0, 3),
        "readable": bool(archive.get("readable")),
        "name_listed": packs.is_client_only_jar(file_name),
        "_meta": meta,
        "_archive": archive,
        **(extra or {}),
    }
    return item


def score_item(item: dict) -> dict:
    result = evaluate(
        file_name=item["file_name"],
        meta=item.get("_meta"),
        archive=item.get("_archive"),
        modrinth_side=item.get("modrinth_side"),
        modrinth_client_side=item.get("modrinth_client_side"),
        categories=item.get("categories"),
        exact_hash_match=bool(item.get("exact_hash_match")),
        name_listed=bool(item.get("name_listed")),
    )
    item.update(result)
    return item


def strip_internals(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


async def score_all(job: Job | None, items: list[dict], *,
                    server_id: str | None = None) -> list[dict]:
    """Hash-match, score, then apply overrides. The whole pipeline."""
    matched = await _modrinth_by_hash(job, items)
    if job and matched:
        job.log_line(
            f"Modrinth identified {matched} of {len(items)} jars by file hash "
            "and stated which side each one runs on")
    for item in items:
        score_item(item)
    apply_overrides(items, server_id=server_id)
    return items


# --- scanning a live instance ------------------------------------------


async def scan_instance(job: Job, server_id: str, directory: str = "mods"
                        ) -> dict:
    """Read every jar in an installed instance and judge it.

    The pre-install review only ever sees a pack the moment it is installed.
    This is the same analysis pointed at what is actually on disk now --
    which is where it matters most, because that set includes jars added by
    hand, jars from an import, and jars installed by a previous version of
    this app before any of these checks existed.
    """
    from app import mods as modmgr

    directory = modmgr.guard_dir(directory)
    job.set_step("Listing jars", 4)
    listing = await modmgr.list_mods(server_id, directory)
    entries = [m for m in listing.get("mods", [])
               if m["file"].lower().endswith((".jar", ".jar.disabled"))]
    if not entries:
        return {"directory": directory, "count": 0, "items": [],
                "summary": summarise([]),
                "note": f"There are no jars in {directory}/."}

    manifest = await crafty.read_studio_manifest(server_id)
    known = {posixpath.basename(r.get("file", "")): r
             for r in (manifest.get("mods") or [])}

    sem = asyncio.Semaphore(6)
    items: list[dict] = []
    done = 0
    lock = asyncio.Lock()
    total = len(entries)

    async def one(entry: dict) -> None:
        nonlocal done
        base = entry["file"]
        record = known.get(base) or known.get(
            base[:-len(".disabled")] if base.endswith(".disabled") else base
        ) or {}
        blob = None
        async with sem:
            try:
                blob = await crafty.download_file(
                    server_id, f"{directory}/{base}")
            except Exception:
                blob = None
            finally:
                async with lock:
                    done += 1
                    if done % 10 == 0 or done == total:
                        job.set_step(f"Reading jars ({done}/{total})",
                                     8 + 50 * done / total)
        item = build_item(
            base, blob,
            name=record.get("name") or entry.get("name"),
            project_id=record.get("project_id"),
            logo=record.get("logo") or entry.get("logo"),
            extra={
                "enabled": entry.get("enabled", True),
                "source": record.get("source"),
                "size_on_disk": entry.get("size"),
            },
        )
        items.append(item)
        if blob is not None:
            del blob

    await asyncio.gather(*(one(e) for e in entries))
    job.set_step("Scoring", 60)
    await score_all(job, items, server_id=server_id)

    # A jar already disabled and judged client-only is not a finding; it is
    # the previous run of this working. Sorting reflects that.
    for item in items:
        item["already_handled"] = (
            item["verdict"] == "client" and not item.get("enabled", True)
        )
    items.sort(key=lambda i: (
        {"client": 0, "review": 1, "keep": 2, "server": 3}[i["verdict"]],
        i.get("already_handled", False),
        -i["score"],
    ))

    summary = summarise(items)
    actionable = [i for i in items
                  if i["verdict"] == "client" and i.get("enabled", True)]
    unread = [i for i in items if not i["readable"]]
    job.set_step("Done", 100)
    job.log_line(
        f"{summary['client']} client-only, {summary['review']} to review, "
        f"{summary['protected']} protected as dependencies, "
        f"{summary['server']} fine"
    )
    return {
        "directory": directory,
        "count": len(items),
        "items": [strip_internals(i) for i in items],
        "summary": summary,
        "actionable": [i["file_name"] for i in actionable],
        "unreadable": [i["file_name"] for i in unread],
        "note": (
            f"{len(actionable)} enabled jar(s) look client-only. Disabling a "
            "jar renames it to .jar.disabled — nothing is deleted, and it is "
            "one click back."
            if actionable else
            "Nothing enabled in this instance looks client-only."
        ) + (
            f" {len(unread)} jar(s) could not be read and were judged on "
            "their name alone." if unread else ""
        ),
    }


async def apply_scan(server_id: str, files: list[str], *, enabled: bool = False,
                     directory: str = "mods") -> dict:
    """Act on a scan: disable (or re-enable) the jars the user ticked."""
    from app import backups
    from app import mods as modmgr

    directory = modmgr.guard_dir(directory)
    files = modmgr.guard_names(files or [])
    if not files:
        raise ValueError("nothing was selected")
    snap = None
    if len(files) > 1:
        snap = await backups.snapshot(
            server_id,
            f"before {'enabling' if enabled else 'disabling'} "
            f"{len(files)} mods from a client-only scan")
    changed, failed = [], []
    for name in files:
        try:
            changed.append(await modmgr.set_enabled(server_id, name, enabled,
                                                    directory))
        except Exception as e:
            failed.append({"file": name, "error": str(e)})
    return {"changed": changed, "failed": failed,
            "snapshot": (snap or {}).get("id"),
            "note": (
                f"{len(changed)} jar(s) {'enabled' if enabled else 'disabled'}. "
                "Restart the server for the change to take effect."
            )}
