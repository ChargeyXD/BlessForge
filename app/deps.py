"""Dependency resolution for single-mod installs.

Installing a mod without its library mods is the most common way to break a
working server, so adding a mod resolves its required dependencies first,
recursively, and reports exactly what it intends to install before doing it.

Relation types differ per source:
  CurseForge  file.dependencies[].relationType
              1 embedded, 2 optional, 3 REQUIRED, 4 tool, 5 incompatible, 6 include
  Modrinth    version.dependencies[].dependency_type
              "required" | "optional" | "incompatible" | "embedded"

Only required entries are followed. Embedded dependencies are deliberately
skipped: the mod already contains them, and installing a second copy causes
the duplicate-mod crash we are trying to avoid.
"""
from __future__ import annotations

import asyncio
import re

from app import crafty, curseforge, modrinth

CF_REQUIRED = 3
CF_INCOMPATIBLE = 5
MAX_DEPTH = 4
MAX_DEPENDENCIES = 40


def _stem(filename: str) -> str:
    """Reduce a jar filename to a comparable mod name.

    "createaddition-neoforge-1.21.1-1.6.0.jar" -> "createaddition"
    Used to spot a mod that is already present under a different filename --
    the case that would otherwise install a second copy and trip the loader's
    duplicate-mod check.
    """
    name = re.sub(r"\.jar(\.disabled)?$", "", filename or "", flags=re.I)
    # Cut at the first version-looking segment.
    name = re.split(r"[-_+]v?\d", name, maxsplit=1)[0]
    name = re.sub(r"[-_+](neoforge|forge|fabric|quilt|mc)$", "", name, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", name.lower())


async def installed_index(server_id: str) -> dict:
    """What this instance already has, keyed every way we might match on."""
    manifest = await crafty.read_studio_manifest(server_id)
    by_project: dict[str, dict] = {}
    files: set[str] = set()
    for record in manifest.get("mods", []):
        pid = record.get("project_id")
        if pid is not None:
            by_project[f"{record.get('source')}:{pid}"] = record
        name = (record.get("file") or "").split("/")[-1]
        if name:
            files.add(name)
    try:
        entries = await crafty.list_dir(server_id, "mods")
        for name, meta in entries.items():
            if name != "root_path" and isinstance(meta, dict) and not meta.get("dir"):
                files.add(name)
                files.add(name.replace(".disabled", ""))
    except crafty.CraftyError:
        pass
    stems = {_stem(f): f for f in files if f}
    return {"by_project": by_project, "files": files, "stems": stems}


def _pick_file(candidates: list[dict], mc_version: str, loader: str) -> dict | None:
    """Choose the newest release that fits this instance.

    Stable releases win over beta/alpha at the same compatibility, because a
    dependency being pulled in automatically should be the boring choice.
    """
    if not candidates:
        return None

    def compatible(f: dict) -> bool:
        versions = [str(v).lower() for v in (f.get("game_versions") or [])]
        loaders = [str(l).lower() for l in (f.get("loaders") or [])]
        if mc_version and versions and mc_version.lower() not in versions:
            return False
        if loader and loaders and loader.lower() not in loaders:
            return False
        return True

    fits = [f for f in candidates if compatible(f)]
    pool = fits or []
    if not pool:
        return None

    rank = {"release": 0, "beta": 1, "alpha": 2}
    pool.sort(
        key=lambda f: (
            rank.get(str(f.get("release_type") or "").lower(), 3),
            -(f.get("file_id") if isinstance(f.get("file_id"), int) else 0),
            str(f.get("date") or ""),
        )
    )
    # Modrinth ids are strings, so fall back to date ordering there.
    if not isinstance(pool[0].get("file_id"), int):
        pool.sort(key=lambda f: (
            rank.get(str(f.get("release_type") or "").lower(), 3),
            str(f.get("date") or ""),
        ), reverse=False)
        best_type = rank.get(str(pool[0].get("release_type") or "").lower(), 3)
        same = [f for f in pool
                if rank.get(str(f.get("release_type") or "").lower(), 3) == best_type]
        same.sort(key=lambda f: str(f.get("date") or ""), reverse=True)
        return same[0]
    return pool[0]


def _mark_if_present(entry: dict, installed: dict, required_by: str | None) -> None:
    """Flag a resolved mod that already exists under a different filename.

    Mods installed as part of a modpack carry no project id until the user
    runs Identify, so an exact-id match misses them. Falling back to the
    filename stem catches "Create is already here as create-6.0.10.jar"
    before a second copy triggers the loader's duplicate-mod crash.
    """
    name = entry.get("file_name") or ""
    if name in installed["files"]:
        entry["already_installed"] = True
        entry["match"] = "exact filename"
        return
    stem = _stem(name)
    existing = installed.get("stems", {}).get(stem)
    if stem and existing:
        entry["already_installed"] = True
        entry["match"] = f"looks like {existing}"
        entry["conflict_note"] = (
            f"{entry.get('name')} appears to be installed already as "
            f"{existing}. Installing it again would load two copies, which "
            "the loader refuses."
        )


async def _cf_dependencies(file_meta: dict) -> tuple[list[int], list[int]]:
    required, incompatible = [], []
    for dep in file_meta.get("dependencies") or []:
        rel = dep.get("relationType")
        mod_id = dep.get("modId")
        if not mod_id:
            continue
        if rel == CF_REQUIRED:
            required.append(int(mod_id))
        elif rel == CF_INCOMPATIBLE:
            incompatible.append(int(mod_id))
    return required, incompatible


async def _mr_dependencies(version_meta: dict) -> tuple[list[dict], list[str]]:
    required, incompatible = [], []
    for dep in version_meta.get("dependencies") or []:
        kind = dep.get("dependency_type")
        if kind == "required":
            required.append(dep)
        elif kind == "incompatible" and dep.get("project_id"):
            incompatible.append(dep["project_id"])
    return required, incompatible


async def resolve(
    *, source: str, project_id: str | int, file_id: str | int,
    mc_version: str, loader: str, installed: dict,
) -> dict:
    """Walk the dependency graph and return a concrete install plan.

    Returns the root mod plus every required dependency that is missing, each
    already resolved to a specific downloadable file.
    """
    plan: list[dict] = []
    warnings: list[str] = []
    conflicts: list[dict] = []
    seen: set[str] = set()

    async def visit(src: str, pid: str | int, fid: str | int | None, depth: int,
                    required_by: str | None) -> None:
        key = f"{src}:{pid}"
        if key in seen or len(plan) >= MAX_DEPENDENCIES:
            return
        seen.add(key)

        already = installed["by_project"].get(key)
        if already and required_by:
            return  # dependency satisfied by something already installed

        if src == "curseforge":
            try:
                if fid:
                    file_meta = await curseforge.get_file(int(pid), int(fid))
                else:
                    candidates = await curseforge.list_files(
                        int(pid), game_version=mc_version or None,
                        mod_loader=loader or None, page_size=50,
                    )
                    file_meta = _pick_file(candidates, mc_version, loader)
                    if not file_meta:
                        # Retry unfiltered: some mods tag versions loosely.
                        candidates = await curseforge.list_files(int(pid),
                                                                 page_size=50)
                        file_meta = _pick_file(candidates, mc_version, loader)
                if not file_meta:
                    warnings.append(
                        f"No compatible file found for CurseForge project {pid}"
                        + (f" (needed by {required_by})" if required_by else "")
                    )
                    return
                project = await curseforge.get_mod(int(pid))
                entry = {
                    "source": "curseforge",
                    "project_id": int(pid),
                    "file_id": file_meta["file_id"],
                    "name": project.get("name") or file_meta.get("file_name"),
                    "file_name": file_meta.get("file_name"),
                    "version": file_meta.get("display_name"),
                    "logo": project.get("logo"),
                    "size": file_meta.get("size"),
                    "required_by": required_by,
                    "depth": depth,
                    "already_installed": bool(already),
                }
                _mark_if_present(entry, installed, required_by)
                plan.append(entry)

                if depth < MAX_DEPTH:
                    req, incompat = await _cf_dependencies(file_meta)
                    for dep_id in incompat:
                        if f"curseforge:{dep_id}" in installed["by_project"]:
                            conflicts.append({
                                "project_id": dep_id,
                                "reason": f"{entry['name']} declares it incompatible",
                            })
                    await asyncio.gather(*(
                        visit("curseforge", dep_id, None, depth + 1, entry["name"])
                        for dep_id in req
                    ))
            except Exception as e:
                warnings.append(f"Could not resolve CurseForge project {pid}: {e}")

        else:  # modrinth
            try:
                if fid:
                    version = await modrinth.get_version(str(fid))
                else:
                    candidates = await modrinth.list_versions(
                        str(pid), game_version=mc_version or None,
                        loader=loader or None,
                    )
                    version = _pick_file(candidates, mc_version, loader)
                if not version:
                    warnings.append(
                        f"No compatible Modrinth version for {pid}"
                        + (f" (needed by {required_by})" if required_by else "")
                    )
                    return
                project = await modrinth.get_project(str(pid)) or {}
                entry = {
                    "source": "modrinth",
                    "project_id": version.get("mod_id") or pid,
                    "file_id": version.get("file_id"),
                    "name": project.get("name") or version.get("file_name"),
                    "file_name": version.get("file_name"),
                    "version": version.get("version_number"),
                    "logo": project.get("logo"),
                    "size": version.get("size"),
                    "required_by": required_by,
                    "depth": depth,
                    "already_installed": bool(already),
                    "server_side": project.get("server_side"),
                }
                _mark_if_present(entry, installed, required_by)
                if project.get("server_side") == "unsupported":
                    warnings.append(
                        f"{entry['name']} is marked client-only on Modrinth and "
                        "will not do anything on a server."
                    )
                plan.append(entry)

                if depth < MAX_DEPTH:
                    req, _ = await _mr_dependencies(version)
                    for dep in req:
                        dep_pid = dep.get("project_id")
                        dep_vid = dep.get("version_id")
                        if dep_pid or dep_vid:
                            await visit("modrinth", dep_pid or dep_vid, dep_vid,
                                        depth + 1, entry["name"])
            except Exception as e:
                warnings.append(f"Could not resolve Modrinth project {pid}: {e}")

    await visit(source, project_id, file_id, 0, None)

    root = plan[0] if plan else None
    dependencies = [p for p in plan[1:] if not p["already_installed"]]
    satisfied = [p for p in plan[1:] if p["already_installed"]]
    return {
        "root": root,
        "dependencies": dependencies,
        "already_satisfied": satisfied,
        "conflicts": conflicts,
        "warnings": warnings,
        "total_downloads": len([p for p in plan if not p["already_installed"]]),
        "total_bytes": sum(
            p.get("size") or 0 for p in plan if not p["already_installed"]
        ),
    }


async def resolve_for_instance(
    server_id: str, *, source: str, project_id: str | int,
    file_id: str | int | None = None,
) -> dict:
    """Convenience wrapper that reads the instance's loader/MC version."""
    manifest = await crafty.read_studio_manifest(server_id)
    mc = manifest.get("minecraft") or ""
    loader = manifest.get("loader") or ""
    if not loader:
        server = await crafty.get_server(server_id)
        exe = (server.get("executable") or "").lower()
        for fam in ("neoforge", "forge", "fabric"):
            if fam in exe:
                loader = fam
                break
    installed = await installed_index(server_id)
    plan = await resolve(
        source=source, project_id=project_id, file_id=file_id,
        mc_version=mc, loader=loader, installed=installed,
    )
    plan["minecraft"] = mc
    plan["loader"] = loader
    return plan
