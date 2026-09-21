"""Who a mod belongs to, and who it cannot sit beside.

The roulette had two failure modes that no amount of version-checking could
catch, because neither is about whether a jar *loads*:

  * It dealt **add-ons instead of the mods they add to.** "Create: Steam 'n'
    Rails" is a fine mod and a useless roll -- it is thirty minutes of track
    for a pack with no Create in it. Worse, the dependency resolver then
    pulled Create in silently at install time, so a hand of 120 became a
    server of 121 with an uninvited headline mod nobody chose.

  * It dealt **mods that refuse to sit together.** Two light engines, two
    recipe viewers, four biome generators. Each one passes every check the
    installer makes and the pack still comes apart on first boot.

Both are questions about *relationships between candidates*, which is a
different thing from `deps.py`. That module answers "what else must I install
to make this one work" for a server that already exists. This one answers
"does this SET of mods make sense together" for a set that does not exist yet
and can still be changed.

The shape of the answers is borrowed from Rearth's Modpack-Inspector, which
reads an installed pack and reports the same four things: what is a library
and what is content, which dependencies are satisfied and which are not, who
is an add-on of whom, and which libraries nothing is actually using. It does
that by reading bytecode out of the jars. We have no jars yet -- a hand is
still only catalogue metadata -- so the same questions are answered from what
the publishers declare, which is weaker but free, and available *before* the
pack is built rather than after.

Nothing here talks to Crafty and nothing here is random. `describe_refs` is
the only function that touches the network.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from app import curseforge, modrinth

# CurseForge's relationType, the same table `deps.py` reads.
CF_EMBEDDED = 1
CF_OPTIONAL = 2
CF_REQUIRED = 3
CF_TOOL = 4
CF_INCOMPATIBLE = 5
CF_INCLUDE = 6


def norm(name: str) -> str:
    """A name reduced to what two spellings of the same mod have in common."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def ref(source: str, project_id: Any) -> str:
    return f"{source}:{project_id}"


# --- libraries ----------------------------------------------------------
#
# A library is a mod that exists to be depended on. Rolling one is a wasted
# slot: it adds nothing a player can see, and if something in the hand needs
# it, the dependency pass installs it anyway. So the roulette drops them from
# the draw -- which it could not do before, because nothing told it which
# ones they were.
#
# Three signals, in descending order of how much they can be trusted.

# 1. The catalogue said so. CurseForge files libraries under one category and
#    Modrinth tags them; both are set by the author.
LIBRARY_CATEGORIES = {
    "api and library", "library", "libraries", "core mod", "coremod",
    "addons", "utility & qol",
}
# "utility & qol" and "addons" are too broad to act on alone, so they only
# corroborate -- see `looks_like_library`.
WEAK_LIBRARY_CATEGORIES = {"utility & qol", "addons"}

# 2. The name says so. Deliberately anchored: "Library of Exile" is a mod,
#    "Exile Library" is not, and `\blib\b` would catch "Liberation".
LIBRARY_NAME = re.compile(
    r"(?:^|[\s:_-])(?:api|lib|libs|library|libraries|core|corelib|"
    r"commons?|framework|toolkit|hooks|bridge|loader|codec|registry)"
    r"(?:$|[\s:_-])",
    re.I,
)

# 3. We simply know. These are the jars that show up in every pack's mods
#    folder and in nobody's description of what they are playing. Listed by
#    normalised name so that "Cloth Config API" and "cloth-config" agree.
KNOWN_LIBRARIES = {
    norm(n) for n in (
        "Fabric API", "Fabric Language Kotlin", "Architectury API",
        "Cloth Config API", "Balm", "Bookshelf", "Collective", "Puzzles Lib",
        "Kotlin for Forge", "GeckoLib", "Curios API", "Patchouli",
        "Resourceful Lib", "Resourceful Config", "YUNG's API", "TerraBlender",
        "Moonlight Lib", "Prism Lib", "Iceberg", "CreativeCore",
        "Placebo", "AutoRegLib", "Shetiphian Core", "CoFH Core",
        "MixinExtras", "Mixin Extras", "Forgified Fabric API",
        "Sinytra Connector", "Connector Extras", "Fabric Loader",
        "Athena", "Cupboard", "Framework", "Konkrete", "Selene",
        "Supermartijn642's Core Lib", "Supermartijn642's Config Lib",
        "TeamLapen Lib", "Zeta", "AppleSkin", "Caelus API", "Playeranimator",
        "Player Animator", "Trinkets", "Fabric Seasons API", "Terrablender",
        "JamLib", "MidnightLib", "Amecs API", "Notenoughanimations",
        "NeoForge", "Forge", "Sophisticated Core", "Titanium",
        "Chipped API", "Jade Addons", "Botarium", "Blueprint",
        "Structure Gel API", "Silent Lib", "The Bumblezone API",
        "Nitrogen Internals", "Cardinal Components API", "Reach Entity Attributes",
    )
}


# Mods CurseForge files under "API and Library" that are nothing of the sort.
# Each of these puts something on screen that a player would notice missing,
# and a roll that quietly swapped one out for another mod would be wrong in a
# way only the person who wanted it could see. The category is usually right;
# this is the short list of where it is not.
NOT_LIBRARIES = {
    norm(n) for n in (
        "The One Probe", "Jade", "WAILA", "Hwyla", "WTHIT",
        "Waystones", "Corail Tombstone", "JourneyMap", "Xaero's Minimap",
    )
}


def looks_like_library(name: str, categories: Iterable[str] = (),
                       summary: str = "") -> str | None:
    """Why this is a library, or None if it looks like content.

    Returns the reason rather than a bool because every drop the roulette
    makes has to be explainable in the UI -- "dropped 6 mods" with no reason
    is indistinguishable from a bug.
    """
    key = norm(name)
    if key in NOT_LIBRARIES:
        return None
    if key in KNOWN_LIBRARIES:
        return "it is a library every pack carries, not something to roll"

    cats = {str(c).lower() for c in (categories or ())}
    strong = cats & (LIBRARY_CATEGORIES - WEAK_LIBRARY_CATEGORIES)
    if strong:
        return f"its author files it under {sorted(strong)[0]}"

    named = LIBRARY_NAME.search(name or "")
    if named:
        # A name alone is thin evidence -- "Applied Energistics 2" has no
        # library word in it and "Ars Nouveau" has none either, but "Iron's
        # Spells 'n Spellbooks" would trip a sloppier pattern. So a name
        # match needs a second opinion from the category or the summary.
        blurb = (summary or "").lower()
        if cats & LIBRARY_CATEGORIES or "library" in blurb or "api for" in blurb:
            return f"it is named like a library ({named.group(0).strip()}) "\
                   "and filed like one"
    return None


# --- add-ons ------------------------------------------------------------
#
# CurseForge keeps a category per big mod family, and files that family's
# add-ons under it. The family's own entry is filed there too, so the rule is
# "filed under a family whose name is not yours" -- which is exactly what an
# add-on is.
#
# Kept short and unambiguous on purpose. A generic category here ("Redstone",
# "Storage") would call half the catalogue an add-on of nothing.

FAMILY_CATEGORIES = {
    "create": "Create",
    "applied energistics 2": "Applied Energistics 2",
    "thermal expansion": "Thermal Expansion",
    "tinkers construct": "Tinkers' Construct",
    "tinker's construct": "Tinkers' Construct",
    "twilight forest": "The Twilight Forest",
    "blood magic": "Blood Magic",
    "thaumcraft": "Thaumcraft",
    "industrial craft": "IndustrialCraft",
    "buildcraft": "BuildCraft",
    "forestry": "Forestry",
    "galacticraft": "Galacticraft",
    "immersive engineering": "Immersive Engineering",
    "mekanism": "Mekanism",
    "botania": "Botania",
    "ars magica": "Ars Magica",
    "farmer's delight": "Farmer's Delight",
    "farmers delight": "Farmer's Delight",
    "jei": "Just Enough Items",
    "just enough items": "Just Enough Items",
    "refined storage": "Refined Storage",
    "ars nouveau": "Ars Nouveau",
    "cobblemon": "Cobblemon",
}

# The shapes an add-on's name takes. Each pattern's first group is the parent
# it is naming, which is then looked for in the pool -- a match against
# nothing present is not evidence of anything.
ADDON_NAME_PATTERNS = [
    re.compile(r"^(.{3,40}?)\s*:\s+\S", re.I),                    # "Create: Foo"
    re.compile(r"^(.{3,40}?)\s+(?:add[- ]?on|addon)s?\b", re.I),  # "Create Addon"
    re.compile(r"\b(?:add[- ]?on|addon|integration|compat(?:ibility)?|"
               r"tweaks?|extras?|expanded|delight)\s+for\s+(.{3,40})$", re.I),
    re.compile(r"^(.{3,40}?)\s+(?:integration|compat(?:ibility)?|extras?|"
               r"tweaks?|delight)\b", re.I),
]


def name_parent(name: str, present: dict[str, dict]) -> dict | None:
    """The mod this one is named after, if that mod is actually around.

    `present` maps a normalised name to a candidate. Requiring the parent to
    be in hand or in the pool is what keeps this from firing on "Sophisticated
    Backpacks" or any other colon in a title.
    """
    for pattern in ADDON_NAME_PATTERNS:
        m = pattern.search(name or "")
        if not m:
            continue
        key = norm(m.group(1))
        if len(key) < 4 or key == norm(name):
            continue
        parent = present.get(key)
        if parent and norm(parent.get("name", "")) != norm(name):
            return parent
    return None


def category_parent(name: str, cf_categories: Iterable[str]) -> str | None:
    """The mod family this one is filed under but is not."""
    mine = norm(name)
    for cat in cf_categories or ():
        family = FAMILY_CATEGORIES.get(str(cat).strip().lower())
        if not family:
            continue
        fam = norm(family)
        # "Create" is filed under Create; its add-ons are too. Only the ones
        # that are not the family itself are add-ons of it.
        if mine == fam or mine.startswith(fam):
            return None
        return family
    return None


# --- who cannot sit beside whom -----------------------------------------
#
# The catalogues do carry incompatibility declarations and those are honoured
# first, but almost nobody fills them in. This table is the part that is not
# in any metadata: families where running two members is a known way to break
# a server, written down with the reason so the UI can say it out loud.
#
# `limit` is how many of a family may survive a roll. One means "these are
# alternatives"; two means "stacking them is a taste decision that stops
# being one past two".

CONFLICT_FAMILIES: list[dict] = [
    {
        "key": "recipe-viewer", "limit": 1, "kind": "conflict",
        "title": "Recipe viewers",
        "why": "they register the same recipe-plugin hooks, and the second "
               "one to load either loses its entries or takes the first "
               "one's with it",
        "members": ["Just Enough Items", "Roughly Enough Items", "EMI",
                    "Not Enough Items", "JEI", "REI"],
    },
    {
        "key": "light-engine", "limit": 1, "kind": "conflict",
        "title": "Light engine rewrites",
        "why": "each one replaces the lighting engine outright; two cannot "
               "both be it",
        "members": ["Starlight", "Phosphor", "Radplex"],
    },
    {
        "key": "renderer", "limit": 1, "kind": "conflict",
        "title": "Renderer replacements",
        "why": "they are alternative rewrites of the same renderer and "
               "mixin into the same classes",
        "members": ["Sodium", "Embeddium", "Rubidium", "Magnesium", "Canvas",
                    "OptiFine", "Optifine"],
    },
    {
        "key": "pregenerator", "limit": 1, "kind": "conflict",
        "title": "Chunk pregenerators",
        "why": "two of them drive the same chunk queue from opposite ends "
               "and the server spends its whole tick budget arguing",
        "members": ["Chunky", "Chunk Pregenerator", "Chunk-Pregenerator",
                    "Dynamic View"],
    },
    {
        "key": "combat", "limit": 1, "kind": "conflict",
        "title": "Combat overhauls",
        "why": "both replace the attack handler; whichever loads second "
               "wins some hits and not others",
        "members": ["Epic Fight", "Better Combat", "Bettercombat",
                    "Combat Overhaul"],
    },
    {
        "key": "biomes", "limit": 2, "kind": "overlap",
        "title": "Biome generators",
        "why": "each one wants the overworld to look like itself; past two "
               "you get a world that looks like none of them",
        "members": ["Biomes O' Plenty", "Oh The Biomes You'll Go",
                    "Oh The Biomes We've Gone", "Terralith",
                    "Regions Unexplored", "BetterEnd", "Wythers",
                    "William Wythers' Overhauled Overworld", "Biomes You Go",
                    "Traverse", "Byg"],
    },
    {
        "key": "dimension-suite", "limit": 2, "kind": "overlap",
        "title": "Large dimension packs",
        "why": "each adds a full dimension with its own worldgen; three of "
               "them is most of your heap before a player joins",
        "members": ["The Twilight Forest", "Twilight Forest", "The Aether",
                    "Aether", "The Undergarden", "Undergarden",
                    "Blue Skies", "The Bumblezone", "Deeper and Darker"],
    },
]

_FAMILY_BY_MEMBER: dict[str, dict] = {}
for _fam in CONFLICT_FAMILIES:
    for _member in _fam["members"]:
        _FAMILY_BY_MEMBER.setdefault(norm(_member), _fam)


def family_of(name: str) -> dict | None:
    """The conflict family a mod belongs to, matched on its whole name.

    Whole-name matching, not substring: "Sodium Extras" is an add-on of
    Sodium, not a second renderer, and dropping it as one would be wrong in a
    way nobody could see from the outside.
    """
    return _FAMILY_BY_MEMBER.get(norm(name))


# --- reading a pinned build ---------------------------------------------


def relations_from_build(build: dict | None, source: str) -> dict:
    """What one file says it needs, embeds and refuses to sit beside."""
    out: dict[str, list[str]] = {"requires": [], "breaks": [], "embeds": []}
    for dep in (build or {}).get("dependencies") or []:
        if source == "curseforge":
            mod_id = dep.get("modId") or dep.get("mod_id")
            if not mod_id:
                continue
            key = ref("curseforge", int(mod_id))
            kind = dep.get("relationType")
            if kind == CF_REQUIRED:
                out["requires"].append(key)
            elif kind == CF_INCOMPATIBLE:
                out["breaks"].append(key)
            elif kind in (CF_EMBEDDED, CF_INCLUDE):
                out["embeds"].append(key)
        else:
            pid = dep.get("project_id")
            if not pid:
                continue
            key = ref("modrinth", pid)
            kind = dep.get("dependency_type")
            if kind == "required":
                out["requires"].append(key)
            elif kind == "incompatible":
                out["breaks"].append(key)
            elif kind == "embedded":
                out["embeds"].append(key)
    for k in out:
        out[k] = sorted(set(out[k]))
    return out


async def describe_refs(refs: Iterable[str]) -> dict[str, dict]:
    """Name and classify a set of `source:id` references, in bulk.

    Each entry carries back the `ref` it was asked about. That is what lets
    a parent the pool never sampled be fetched on its own afterwards -- the
    strongest add-on signal is a declared dependency, and without the ref
    travelling with the answer it was the one signal that could not reach the
    catalogue fallback.

    Two requests total however many refs come in, which is what makes it
    affordable to run over a 120-mod hand. Anything the catalogue will not
    answer for comes back missing rather than wrong, and every caller treats
    missing as "no opinion".
    """
    wanted = {r for r in refs if r}
    cf_ids, mr_ids = [], []
    for r in wanted:
        source, _, pid = r.partition(":")
        if source == "curseforge":
            try:
                cf_ids.append(int(pid))
            except ValueError:
                continue
        elif source == "modrinth":
            mr_ids.append(pid)

    out: dict[str, dict] = {}
    if cf_ids:
        try:
            for mod_id, project in (await curseforge.get_mods(cf_ids)).items():
                cats = project.get("categories") or []
                name = project.get("name") or ""
                out[ref("curseforge", mod_id)] = {
                    "ref": ref("curseforge", mod_id),
                    "source": "curseforge", "project_id": mod_id, "name": name,
                    "slug": project.get("slug"), "url": project.get("url"),
                    "logo": project.get("logo"),
                    "downloads": project.get("downloads") or 0,
                    "categories": cats,
                    "library": looks_like_library(
                        name, cats, project.get("summary") or ""),
                }
        except Exception:
            pass
    if mr_ids:
        try:
            for pid, project in (await modrinth.get_projects(mr_ids)).items():
                cats = project.get("categories") or []
                name = project.get("name") or ""
                out[ref("modrinth", pid)] = {
                    "ref": ref("modrinth", pid),
                    "source": "modrinth", "project_id": pid, "name": name,
                    "slug": project.get("slug"),
                    "url": f"https://modrinth.com/mod/{project.get('slug')}",
                    "logo": project.get("logo"),
                    "downloads": project.get("downloads") or 0,
                    "categories": cats,
                    "library": looks_like_library(name, cats,
                                                  project.get("summary") or ""),
                }
        except Exception:
            pass
    return out


# --- the report ---------------------------------------------------------


def _key(mod: dict) -> str:
    return ref(mod.get("source", ""), mod.get("project_id"))


def study(hand: list[dict], described: dict[str, dict] | None = None,
          present: dict[str, dict] | None = None) -> dict:
    """The Inspector's read of a hand: families, libraries, gaps, clashes.

    Pure and offline -- `described` is whatever `describe_refs` managed to
    fetch, and an empty one degrades the report rather than breaking it.
    """
    described = described or {}
    in_hand = {_key(m): m for m in hand}
    by_norm = {**(present or {}), **{norm(m["name"]): m for m in hand}}

    rows: list[dict] = []
    for m in hand:
        rel = m.get("relations") or {}
        requires = rel.get("requires") or []
        mine_lib = looks_like_library(
            m.get("name", ""),
            (m.get("cf_categories") or []) + (m.get("mr_categories") or []),
            m.get("summary") or "")

        needs_content: list[dict] = []
        needs_library: list[dict] = []
        unresolved: list[str] = []
        for r in requires:
            info = described.get(r)
            if not info:
                unresolved.append(r)
                continue
            (needs_library if info.get("library") else needs_content).append(info)

        # Three ways to be somebody's add-on, strongest first.
        parent = None
        how = ""
        if needs_content:
            parent = max(needs_content, key=lambda i: i.get("downloads") or 0)
            how = "declared"
        else:
            family = category_parent(m.get("name", ""), m.get("cf_categories") or [])
            if family:
                parent, how = {"name": family, "ref": None}, "category"
            else:
                named = name_parent(m.get("name", ""), by_norm)
                if named:
                    parent, how = {"name": named.get("name"),
                                   "ref": _key(named)}, "name"

        rows.append({
            "name": m.get("name"),
            "key": _key(m),
            "library": mine_lib,
            "parent": parent,
            "parent_how": how,
            "parent_in_hand": bool(
                parent and (parent.get("ref") in in_hand
                            or norm(parent.get("name", "")) in
                            {norm(x["name"]) for x in hand})),
            "needs_library": [i["name"] for i in needs_library],
            "needs_content": [i["name"] for i in needs_content],
            "unresolved": unresolved,
            "breaks": rel.get("breaks") or [],
        })

    # Libraries the hand will pull in, and who asked for each.
    wanted_by: dict[str, list[str]] = {}
    for row in rows:
        for lib in row["needs_library"]:
            wanted_by.setdefault(lib, []).append(row["name"])

    families: dict[str, dict] = {}
    for row in rows:
        if not row["parent"]:
            continue
        pname = row["parent"].get("name") or "?"
        fam = families.setdefault(pname, {
            "parent": pname, "ref": row["parent"].get("ref"),
            "in_hand": row["parent_in_hand"], "members": [], "how": row["parent_how"],
        })
        fam["members"].append(row["name"])
        fam["in_hand"] = fam["in_hand"] or row["parent_in_hand"]

    return {
        "total": len(hand),
        "content": sum(1 for r in rows if not r["library"]),
        "libraries_rolled": [r["name"] for r in rows if r["library"]],
        "addons": sum(1 for r in rows if r["parent"]),
        "orphans": [
            {"name": r["name"], "parent": r["parent"].get("name"),
             "how": r["parent_how"]}
            for r in rows if r["parent"] and not r["parent_in_hand"]
        ],
        "families": sorted(families.values(),
                           key=lambda f: (-len(f["members"]), f["parent"])),
        "libraries": sorted(
            ({"name": name, "needed_by": sorted(set(who))}
             for name, who in wanted_by.items()),
            key=lambda x: (-len(x["needed_by"]), x["name"])),
        "unresolved": [
            {"name": r["name"], "refs": r["unresolved"]}
            for r in rows if r["unresolved"]
        ],
        "conflicts": find_conflicts(hand),
        "rows": rows,
    }


def find_conflicts(hand: list[dict]) -> list[dict]:
    """Every group in this hand that should not all be in it.

    Two sources, reported the same way: what the publishers declared, and the
    families above. Each entry names one survivor and the rest to drop, so the
    caller can act on it without re-deciding anything.
    """
    by_key = {_key(m): m for m in hand}
    out: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()

    def rank(m: dict) -> tuple:
        # Most-downloaded survives; the name only breaks ties, so the result
        # does not depend on the order the hand happens to be in.
        return (-(m.get("downloads") or 0), m.get("name") or "")

    # 1. declared, by whoever bothered to fill it in
    for m in hand:
        for other_ref in (m.get("relations") or {}).get("breaks") or []:
            other = by_key.get(other_ref)
            if not other:
                continue
            pair = tuple(sorted((_key(m), other_ref)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            keep, drop = sorted((m, other), key=rank)
            out.append({
                "kind": "declared", "key": "declared",
                "title": f"{m['name']} lists {other['name']} as incompatible",
                "why": "one of the two authors says outright that these "
                       "cannot both be installed",
                "members": [m["name"], other["name"]],
                "keep": keep["name"], "drop": [drop["name"]],
            })

    # 2. the families -- the part no catalogue records
    grouped: dict[str, list[dict]] = {}
    for m in hand:
        fam = family_of(m.get("name", ""))
        if fam:
            grouped.setdefault(fam["key"], []).append(m)
    for key, members in grouped.items():
        fam = next(f for f in CONFLICT_FAMILIES if f["key"] == key)
        if len(members) <= fam["limit"]:
            continue
        ordered = sorted(members, key=rank)
        out.append({
            "kind": fam["kind"], "key": key, "title": fam["title"],
            "why": fam["why"],
            "members": [m["name"] for m in ordered],
            "keep": ", ".join(m["name"] for m in ordered[:fam["limit"]]),
            "drop": [m["name"] for m in ordered[fam["limit"]:]],
        })
    return out
