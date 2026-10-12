"""Files whose authors turned off third-party distribution.

CurseForge gives such a file no `downloadUrl`, and its terms say a
third-party service must not fetch it any other way. Earlier versions of
BlessForge built the CDN address by hand and downloaded it regardless.
That is gone; this module is what replaced it.

For each blocked file, in order:

  1. **The same jar on Modrinth, by SHA-1.** CurseForge publishes every
     file's SHA-1 and Modrinth indexes by it, so a hit is the identical
     bytes from a place the author chose to publish them. Exact, and the
     common case for mods published on both.
  2. **The same project on Modrinth, by slug.** Not the same bytes, so it is
     only accepted when the project's name agrees too (a slug alone can
     collide) and a build exists for this Minecraft version and loader. The
     record says it was a substitute, and which build it replaced.
  3. **Nothing.** The file is listed as `blocked_by_author` -- in the
     pre-install review, in the install report and on the Mods tab -- with a
     link to its CurseForge page and the words "upload this jar yourself".
     It is never downloaded.

Pure lookups only. Nothing here downloads a jar; callers fetch the
substitute through `modrinth.download`, which only ever uses the address
Modrinth publishes.
"""
from __future__ import annotations

import re

from app import curseforge, modrinth


def _norm(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _unbracket(name: str) -> str:
    """'Aquaculture Delight (A Farmer's Delight Add-on)' -> 'Aquaculture Delight'."""
    return re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", name or "").strip()


def record(meta: dict, project: dict | None = None, *,
           required_by: str | None = None) -> dict:
    """The entry shown wherever a blocked file has to be explained."""
    project = project or {}
    out = {
        "name": project.get("name") or meta.get("file_name"),
        "file_name": meta.get("file_name"),
        "project_id": meta.get("mod_id") or project.get("id"),
        "file_id": meta.get("file_id"),
        "url": curseforge.page_url(meta, project.get("slug")),
        "reason": ("its author does not allow third-party downloads -- "
                   "upload this jar yourself"),
    }
    if required_by:
        out["required_by"] = required_by
    return out


def _pick_slug_build(versions: list[dict], cf_meta: dict) -> dict | None:
    """The Modrinth build that best stands in for a CurseForge file.

    Prefer the one whose version number appears in the CurseForge file's own
    name or display name -- the same release, published twice -- then the
    newest stable build, then the newest of anything.
    """
    usable = [v for v in versions if v.get("download_url")]
    if not usable:
        return None
    haystack = f"{cf_meta.get('file_name') or ''} {cf_meta.get('display_name') or ''}"
    same = [v for v in usable
            if v.get("version_number") and v["version_number"] in haystack]
    if same:
        return same[0]
    stable = [v for v in usable if v.get("release_type") == "release"]
    return (stable or usable)[0]


async def substitutes(
    metas: list[dict],
    projects: dict | None = None,
    *,
    mc_version: str = "",
    loader: str = "",
) -> tuple[dict, list[dict]]:
    """Find Modrinth stand-ins for blocked CurseForge files.

    Returns `(found, blocked)`: `found` maps a CurseForge file id to the
    Modrinth version that replaces it (with `substitute_via` set to "hash"
    or "slug"), and `blocked` lists the records for everything left over.
    Metas that are not blocked are ignored, so callers can pass a whole
    pack's worth.
    """
    projects = projects or {}
    todo = [m for m in metas if curseforge.is_distribution_blocked(m)]
    if not todo:
        return {}, []

    found: dict = {}
    if modrinth_enabled():
        by_hash = {m["sha1"]: m for m in todo if m.get("sha1")}
        try:
            hits = await modrinth.versions_from_hashes(list(by_hash))
        except Exception:
            hits = {}
        for sha, version in hits.items():
            meta = by_hash.get(sha)
            if meta and version.get("download_url"):
                found[meta.get("file_id")] = {**version, "substitute_via": "hash"}

        for meta in todo:
            if meta.get("file_id") in found:
                continue
            project = projects.get(meta.get("mod_id")) or {}
            slug = project.get("slug")
            if not slug:
                continue
            try:
                mr = await modrinth.get_project(slug)
            except Exception:
                mr = None
            # A slug is a short string two unrelated projects can share. The
            # names agreeing as well is what makes it the same mod.
            if not mr or _norm(mr.get("name")) != _norm(project.get("name")):
                continue
            try:
                versions = await modrinth.list_versions(
                    slug, game_version=mc_version or None,
                    loader=(loader or None))
            except Exception:
                continue
            best = _pick_slug_build(versions, meta)
            if best:
                found[meta.get("file_id")] = {**best, "substitute_via": "slug"}

        # The same project often lives under a different slug on Modrinth
        # ("ars-nouveau" vs "ars-nouveau-mod"). A search by name for this
        # version and loader finds it -- accepted only when the name matches
        # exactly AND an author is shared, because a name alone is something
        # two unrelated projects can have.
        for meta in todo:
            if meta.get("file_id") in found:
                continue
            project = projects.get(meta.get("mod_id")) or {}
            name = project.get("name") or ""
            if not name:
                continue
            try:
                hits = (await modrinth.search(query=name, game_version=mc_version or None,
                                              loader=loader or None, page_size=8))["items"]
            except Exception:
                continue
            cf_authors = {_norm(a) for a in project.get("authors") or [] if a}
            for hit in hits:
                if _norm(hit.get("name")) not in (_norm(name), _norm(_unbracket(name))):
                    continue
                mr_authors = {_norm(a) for a in (hit.get("authors") or [])
                              if isinstance(a, str)}
                if cf_authors and mr_authors and not (cf_authors & mr_authors):
                    continue
                try:
                    versions = await modrinth.list_versions(
                        hit["slug"] or hit["id"], game_version=mc_version or None,
                        loader=(loader or None))
                except Exception:
                    continue
                best = _pick_slug_build(versions, meta)
                if best:
                    found[meta.get("file_id")] = {**best, "substitute_via": "name"}
                    break

    blocked = [record(m, projects.get(m.get("mod_id")))
               for m in todo if m.get("file_id") not in found]
    return found, blocked


def modrinth_enabled() -> bool:
    from app import config
    return bool(config.MODRINTH_ENABLED)


def substitute_note(version: dict) -> str:
    """One line for a log or a record saying where the jar really came from."""
    if version.get("substitute_via") == "hash":
        return ("its author blocks third-party downloads on CurseForge; the "
                "identical jar (same SHA-1) was fetched from Modrinth")
    return ("its author blocks third-party downloads on CurseForge; the same "
            "project's build for this version was fetched from Modrinth "
            f"({version.get('version_number') or version.get('file_name')})")
