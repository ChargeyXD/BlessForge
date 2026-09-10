"""Plugins for Paper-family servers, from Modrinth.

CurseForge has no plugin section at all -- its Minecraft catalogue is mods,
modpacks and resource packs -- so a Paper server had nowhere to shop from in
this app. Modrinth does carry plugins, and needs no API key, which makes it
the right and only source here.

Two things about Modrinth's plugin catalogue are easy to get wrong:

  * **A plugin searches as `plugin` and reads back as `mod`.** The search
    facet `project_type:plugin` is the one that works; the hits it returns
    still describe themselves as `project_type: mod`, and combining
    `project_type:mod` with plugin loader categories matches nothing at all.
    Getting that backwards returns an empty catalogue and looks like an
    outage.
  * **Almost nobody tags all five.** A plugin that says `paper` often does
    not say `spigot`, and vice versa, so the loaders have to be ORed. Facets
    AND across groups and OR within one; getting that backwards returns an
    empty catalogue and looks like an outage.

Compatibility is then checked properly rather than assumed: Paper runs
Spigot and Bukkit plugins, Purpur runs everything Paper runs, and Folia runs
only what explicitly claims Folia -- a plugin that does not is not merely
untested there, it will fail at load.
"""
from __future__ import annotations

import asyncio
from typing import Any

from app import config, crafty, loaders, modrinth
from app import mods as modmgr
from app.jobs import Job

# What each server family can actually load. Ordered widest-first so the
# version picker prefers a build that names the exact server over a generic
# Bukkit one.
COMPATIBLE_LOADERS = {
    "paper": ["paper", "spigot", "bukkit", "folia"],
    "purpur": ["purpur", "paper", "spigot", "bukkit"],
    # Folia's threading model breaks plugins written for a single main
    # thread, so anything that does not claim Folia is not a candidate.
    "folia": ["folia"],
    "spigot": ["spigot", "bukkit"],
    "bukkit": ["bukkit"],
}

# Proxies, listed so someone running one is not told plugins are unavailable.
PROXY_LOADERS = {"velocity": ["velocity"], "waterfall": ["waterfall",
                                                        "bungeecord"],
                 "bungeecord": ["bungeecord"]}

ALL_PLUGIN_LOADERS = ["paper", "spigot", "bukkit", "purpur", "folia"]

# Modrinth's own category vocabulary for plugins, minus the ones that only
# make sense for client mods.
CATEGORIES = [
    {"key": "adventure", "title": "Adventure"},
    {"key": "economy", "title": "Economy"},
    {"key": "equipment", "title": "Equipment"},
    {"key": "game-mechanics", "title": "Game mechanics"},
    {"key": "management", "title": "Management"},
    {"key": "minigame", "title": "Minigames"},
    {"key": "mobs", "title": "Mobs"},
    {"key": "optimization", "title": "Performance"},
    {"key": "social", "title": "Social & chat"},
    {"key": "storage", "title": "Storage"},
    {"key": "transportation", "title": "Transport"},
    {"key": "utility", "title": "Utility"},
    {"key": "world-management", "title": "World management"},
]

# The handful every new Paper server ends up with. Offered as a starting
# point on the create screen rather than left for the user to rediscover.
STARTER_PACK = [
    {"slug": "luckperms", "why": "Permissions. Almost every other plugin "
                                 "assumes you have one."},
    {"slug": "essentialsx", "why": "/home, /spawn, /tpa, kits, warps — the "
                                   "commands players expect to exist."},
    {"slug": "coreprotect", "why": "Block logging and rollback. The only "
                                   "real answer to griefing."},
    {"slug": "spark", "why": "A profiler for when the server is lagging and "
                             "nobody knows why."},
    {"slug": "worldedit", "why": "Bulk terrain and building edits."},
    {"slug": "vault", "why": "The bridge economy and permission plugins use "
                             "to talk to each other."},
    {"slug": "chunky", "why": "Pre-generate the world so players are not "
                              "generating it for you at 4 TPS."},
    {"slug": "viaversion", "why": "Let newer clients join an older server."},
]


class PluginError(ValueError):
    pass


def compatible_loaders(family: str) -> list[str]:
    fam = loaders.family_of(family)
    return (COMPATIBLE_LOADERS.get(fam) or PROXY_LOADERS.get(fam)
            or ALL_PLUGIN_LOADERS)


def _compat_note(family: str, plugin_loaders: list[str]) -> tuple[str, str]:
    """`(level, note)` -- how well this plugin fits this server."""
    fam = loaders.family_of(family)
    have = {l.lower() for l in (plugin_loaders or [])}
    if fam in have:
        return "exact", f"Built for {fam}."
    if fam == "folia":
        return "blocked", (
            "This plugin does not declare Folia support. Folia's regionised "
            "threading breaks plugins written for a single main thread, so "
            "it will most likely fail to load."
        )
    if fam in ("paper", "purpur") and ("spigot" in have or "bukkit" in have):
        return "good", (
            f"Built for {'Spigot' if 'spigot' in have else 'Bukkit'}; "
            f"{fam.title()} runs those unchanged."
        )
    if fam == "purpur" and "paper" in have:
        return "good", "Built for Paper; Purpur runs every Paper plugin."
    if not have:
        return "unknown", ("This build does not say which server software it "
                           "targets.")
    return "risky", (
        f"Declares {', '.join(sorted(have))} but not {fam}. It may still "
        "work, but nothing here can promise it."
    )


async def search(*, query: str = "", family: str = "paper",
                 game_version: str | None = None, category: str | None = None,
                 index: int = 0, page_size: int = 30,
                 sort: str = "relevance") -> dict:
    """Search Modrinth for plugins this server could run."""
    if not config.MODRINTH_ENABLED:
        raise PluginError(
            "Modrinth is disabled (MODRINTH_ENABLED=false), and it is the "
            "only source of plugins — CurseForge does not carry them."
        )
    wanted = compatible_loaders(family)
    result = await modrinth.search(
        query=query, project_type="plugin", game_version=game_version,
        loaders=wanted, categories=[category] if category else None,
        index=index, page_size=page_size, sort=sort,
    )
    for hit in result["items"]:
        cats = [c.lower() for c in (hit.get("categories") or [])]
        hit["plugin_loaders"] = [c for c in cats
                                 if c in set(ALL_PLUGIN_LOADERS)
                                 | set(PROXY_LOADERS)]
        hit["display_categories"] = [c for c in (hit.get("categories") or [])
                                     if c.lower() not in
                                     set(ALL_PLUGIN_LOADERS)
                                     | set(PROXY_LOADERS)]
        level, note = _compat_note(family, hit["plugin_loaders"])
        hit["compat"] = level
        hit["compat_note"] = note
        hit["kind"] = "plugin"
    result["family"] = loaders.family_of(family)
    result["compatible_loaders"] = wanted
    return result


async def versions(project_id: str, *, family: str = "paper",
                   game_version: str | None = None) -> dict:
    """Builds of one plugin, newest first, each judged for this server."""
    wanted = compatible_loaders(family)
    items = await modrinth.list_versions(
        project_id, game_version=game_version, loaders=wanted)
    if not items:
        # Fall back to every build so the UI can say "there are builds, none
        # for your version" rather than "this plugin does not exist".
        items = await modrinth.list_versions(project_id)
        for v in items:
            v["off_version"] = True
    for v in items:
        level, note = _compat_note(family, v.get("loaders") or [])
        v["compat"] = level
        v["compat_note"] = note
        v["fits_version"] = (
            not game_version or game_version in (v.get("game_versions") or [])
        )
    return {"items": items, "family": loaders.family_of(family),
            "game_version": game_version}


async def _best_version(project_id: str, family: str, game_version: str | None
                        ) -> dict | None:
    """The build to install: right version, right server, stable if possible.

    `versions()` falls back to listing *every* build when the filtered query
    is empty, so the UI can say "there are builds, none for your server"
    rather than "this plugin does not exist". Those fallback entries must
    never be installed -- spark publishes Fabric, Forge and NeoForge builds
    for the same Minecraft version as its Paper one, and picking the first
    that merely fits the version installs a mod into plugins/, where it does
    nothing and reports nothing.
    """
    data = await versions(project_id, family=family, game_version=game_version)
    usable = [
        v for v in data["items"]
        if v.get("fits_version")
        and not v.get("off_version")
        and v.get("compat") in ("exact", "good")
    ]
    if not usable:
        return None
    release = [v for v in usable if v.get("release_type") == "release"]
    exact = [v for v in (release or usable) if v.get("compat") == "exact"]
    return (exact or release or usable)[0]


async def resolve(project_id: str, *, family: str = "paper",
                  game_version: str | None = None,
                  installed: set[str] | None = None) -> dict:
    """What installing this plugin would pull in, before committing.

    Plugin dependencies are shallower than mod dependencies -- one or two
    levels, not eight -- but they matter more: a permissions plugin missing
    Vault does not warn, it just silently does nothing.
    """
    installed = {i.lower() for i in (installed or set())}
    root = await modrinth.get_project(project_id)
    if not root:
        raise PluginError(f"Modrinth has no project '{project_id}'")
    version = await _best_version(root["id"], family, game_version)
    if not version:
        raise PluginError(
            f"{root['name']} has no build for "
            f"{game_version or 'this version'} on {loaders.family_of(family)}."
        )

    plan = [{
        "project_id": root["id"], "slug": root.get("slug"),
        "name": root["name"], "logo": root.get("logo"),
        "file_id": version["file_id"], "file_name": version["file_name"],
        "version": version.get("version_number"), "size": version.get("size"),
        "role": "requested", "required_by": None,
        "compat": version.get("compat"), "compat_note": version.get("compat_note"),
        "present": (version.get("file_name") or "").lower() in installed,
    }]

    seen = {root["id"]}
    queue = [(version, root["name"])]
    depth = 0
    while queue and depth < 3:
        depth += 1
        nxt = []
        for ver, owner in queue:
            for dep in (ver.get("dependencies") or []):
                if dep.get("dependency_type") != "required":
                    continue
                pid = dep.get("project_id")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                project = await modrinth.get_project(pid)
                if not project:
                    continue
                dep_version = (
                    await modrinth.get_version(dep["version_id"])
                    if dep.get("version_id")
                    else await _best_version(pid, family, game_version)
                )
                if not dep_version:
                    plan.append({
                        "project_id": pid, "name": project["name"],
                        "slug": project.get("slug"), "logo": project.get("logo"),
                        "role": "missing", "required_by": owner,
                        "compat": "blocked",
                        "compat_note": (
                            f"{project['name']} is required by {owner} but has "
                            f"no build for {game_version or 'this version'}."
                        ),
                    })
                    continue
                plan.append({
                    "project_id": pid, "slug": project.get("slug"),
                    "name": project["name"], "logo": project.get("logo"),
                    "file_id": dep_version["file_id"],
                    "file_name": dep_version["file_name"],
                    "version": dep_version.get("version_number"),
                    "size": dep_version.get("size"),
                    "role": "dependency", "required_by": owner,
                    "compat": dep_version.get("compat", "unknown"),
                    "compat_note": dep_version.get("compat_note"),
                    "present": (dep_version.get("file_name") or "").lower()
                    in installed,
                })
                nxt.append((dep_version, project["name"]))
        queue = nxt

    return {
        "plugin": plan[0],
        "plan": plan,
        "dependencies": [p for p in plan if p["role"] == "dependency"],
        "missing": [p for p in plan if p["role"] == "missing"],
        "total_bytes": sum(p.get("size") or 0 for p in plan
                           if not p.get("present")),
        "family": loaders.family_of(family),
        "game_version": game_version,
    }


async def install(job: Job, server_id: str, *, project_id: str,
                  file_id: str | None = None, family: str = "paper",
                  game_version: str | None = None,
                  with_dependencies: bool = True,
                  skip_projects: list[str] | None = None) -> dict:
    """Install a plugin (and what it needs) into `plugins/`."""
    skip = {str(s) for s in (skip_projects or [])}
    job.set_step("Reading the plugins folder", 6)
    try:
        listing = await modmgr.list_mods(server_id, "plugins")
        installed = {m["file"].lower() for m in listing.get("mods", [])}
    except Exception:
        installed = set()

    job.set_step("Resolving dependencies", 16)
    plan = await resolve(project_id, family=family, game_version=game_version,
                         installed=installed)

    if file_id:
        # An explicit build overrules the automatic pick, but its dependencies
        # are still the ones the resolver found.
        chosen = await modrinth.get_version(str(file_id))
        if not chosen:
            raise PluginError(f"Modrinth version {file_id} not found")
        plan["plan"][0].update(
            file_id=chosen["file_id"], file_name=chosen["file_name"],
            version=chosen.get("version_number"), size=chosen.get("size"),
        )

    blocked = [p for p in plan["plan"]
               if p.get("compat") == "blocked" and p["role"] != "missing"]
    if blocked and plan["plan"][0].get("compat") == "blocked":
        raise PluginError(plan["plan"][0].get("compat_note")
                          or "This plugin cannot run on this server.")

    todo = [p for p in plan["plan"]
            if p["role"] != "missing"
            and not p.get("present")
            and str(p["project_id"]) not in skip
            and (with_dependencies or p["role"] == "requested")]
    if not todo:
        return {**plan, "installed": [],
                "note": "Everything this plugin needs is already installed."}

    await crafty.ensure_dir(server_id, "plugins")
    done, failed = [], []
    for i, entry in enumerate(todo):
        job.set_step(f"Installing {entry['name']} ({i + 1}/{len(todo)})",
                     25 + 70 * i / max(len(todo), 1))
        try:
            result = await modmgr.add_mod(
                server_id, source="modrinth",
                project_id=str(entry["project_id"]),
                file_id=str(entry["file_id"]),
                directory="plugins",
                required_by=entry.get("required_by"),
            )
            done.append({**entry, "installed": result.get("installed")})
            job.log_line(f"Installed {result.get('installed')}")
        except Exception as e:
            failed.append({"name": entry["name"], "error": str(e)})
            job.log_line(f"{entry['name']} failed: {e}", "error")

    job.set_step("Done", 100)
    note_bits = []
    if plan["missing"]:
        note_bits.append(
            f"{len(plan['missing'])} required plugin(s) have no build for this "
            "version and were not installed: "
            + ", ".join(p["name"] for p in plan["missing"])
        )
    if failed:
        note_bits.append(f"{len(failed)} failed to download.")
    note_bits.append("Restart the server for new plugins to load — Paper does "
                     "not hot-load them.")
    return {**plan, "installed": done, "failed": failed,
            "note": " ".join(note_bits)}


async def starter_pack(*, family: str = "paper",
                       game_version: str | None = None) -> dict:
    """The short list a new Paper server almost always wants."""
    if not config.MODRINTH_ENABLED:
        return {"items": [], "note": "Modrinth is disabled."}

    async def one(entry: dict) -> dict | None:
        try:
            project = await modrinth.get_project(entry["slug"])
            if not project:
                return None
            version = await _best_version(project["id"], family, game_version)
            return {
                "project_id": project["id"], "slug": project["slug"],
                "name": project["name"], "summary": project.get("summary"),
                "logo": project.get("logo"), "downloads": project.get("downloads"),
                "why": entry["why"],
                "file_id": (version or {}).get("file_id"),
                "file_name": (version or {}).get("file_name"),
                "version": (version or {}).get("version_number"),
                "available": bool(version),
                "compat": (version or {}).get("compat", "unknown"),
                "url": project.get("url"),
            }
        except Exception:
            return None

    found = await asyncio.gather(*(one(e) for e in STARTER_PACK))
    items = [f for f in found if f]
    return {
        "items": items,
        "family": loaders.family_of(family),
        "game_version": game_version,
        "note": "Tick what you want; each one is installed with its "
                "dependencies.",
    }


async def audit(server_id: str) -> dict:
    """Look at what is actually in `plugins/` and say what is wrong with it.

    The two failures a Paper operator hits and cannot see: a mod jar dropped
    into plugins/ (which does nothing at all and reports nothing), and a
    plugin built for a different Minecraft version.
    """
    from app import jarmeta

    manifest = await crafty.read_studio_manifest(server_id)
    family = loaders.family_of(manifest.get("loader") or "paper")
    mc = manifest.get("minecraft") or ""

    listing = await modmgr.list_mods(server_id, "plugins")
    entries = listing.get("mods", [])
    findings: list[dict] = []

    for mod in entries:
        name = mod["file"]
        if not name.lower().endswith((".jar", ".jar.disabled")):
            continue
        # A mod jar carries loader metadata a plugin never does. That is the
        # cheap, certain test for "this is in the wrong folder".
        try:
            blob = await crafty.download_file(server_id, f"plugins/{name}")
        except Exception:
            continue
        markers = jarmeta.loader_markers(blob)
        if markers & {"fabric", "forge", "neoforge", "quilt"}:
            findings.append({
                "severity": "critical", "file": name,
                "title": f"{name} is a mod, not a plugin",
                "detail": (
                    f"It carries {', '.join(sorted(markers))} metadata. Paper "
                    "ignores plugins/ entries that are not Bukkit plugins, so "
                    "this jar is doing nothing at all — and reporting nothing."
                ),
                "fix": {"action": "delete", "files": [name]},
            })
            continue
        try:
            import io
            import zipfile
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                names = set(z.namelist())
                if not ({"plugin.yml", "paper-plugin.yml", "bungee.yml",
                         "velocity-plugin.json"} & names):
                    findings.append({
                        "severity": "warn", "file": name,
                        "title": f"{name} has no plugin.yml",
                        "detail": "Paper will refuse to load it. It may be a "
                                  "library another plugin bundles, in which "
                                  "case it belongs somewhere else.",
                    })
        except Exception:
            pass

    return {
        "family": family, "minecraft": mc, "count": len(entries),
        "findings": findings,
        "ok": not findings,
        "note": "Every jar in plugins/ was opened and checked."
        if entries else "There are no plugins installed yet.",
    }
