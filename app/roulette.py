"""Mod Roulette: build a modpack nobody chose.

The user sets constraints -- Minecraft version, loader, how many mods, how
reckless to be, which categories to prefer or ban -- and pulls a lever. What
comes back is a *hand*: a specific set of mods, drawn from the live CurseForge
and Modrinth catalogues, that can then be installed as a real server and
exported as a CurseForge modpack zip.

Three properties make this more than a shuffle:

  * **The hand holds together.** A roll used to deal add-ons without the mods
    they add to, and alternatives that refuse to sit beside each other. Every
    hand now goes through `kinship`: bare libraries are not dealt, an add-on
    brings its parent in or leaves, and a family that should only have one
    member in a pack only gets one.

  * **Every mod is real.** Nothing enters a hand until the catalogue has been
    asked for a build that declares this exact Minecraft version and a loader
    this server can run. What fails is replaced, not quietly subtracted.

  * **The odds are honest.** The panel that says "this will not boot" is
    computed from the same facts the installer will act on -- declared file
    sizes, download counts, staleness, and whether a mod has a server-side
    code path at all. It is not decoration.

The randomness is deliberately *not* uniform. Categories the user marks
preferred are weighted toward the hand; banned ones do not enter at all. That
keeps a roll feeling authored rather than arbitrary while still surprising the
person who asked for it. It is otherwise a genuine draw: there is no seed, no
saved roll and no way to ask for the same hand twice, which is the point.

Everything here is read-only against the catalogues until the user accepts a
hand; `install_roll` is the only function that touches Crafty.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import random
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import httpx

from app import (config, crafty, curseforge, deps, installer, kinship,
                 modrinth, packs)
from app.jobs import Job

# --- naming a roll ------------------------------------------------------
#
# There used to be a seed here, and a hash-into-PRNG ported from the design
# prototype so that a seed typed in the browser and a seed typed here agreed.
# It worked, and nobody wanted it: a roulette whose whole appeal is not
# knowing what you will get does not need a machine for getting it twice.
#
# What is left is a label. A roll still needs *a* name -- the export it writes
# is a file on disk, and a file needs a filename -- so one is minted per pull
# from the clock and four random letters. It identifies an archive. It does
# not reproduce anything.

ROLL_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def mint_roll_id() -> str:
    tag = "".join(random.choice(ROLL_ALPHABET) for _ in range(4))
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}"


def safe_roll_id(raw: str | None) -> str:
    """Whatever came in, reduced to something that can be a filename."""
    cleaned = re.sub(r"[^A-Za-z0-9-]", "", (raw or "").upper())[:40]
    return cleaned or mint_roll_id()


# --- categories ---------------------------------------------------------
#
# The nine buckets the UI offers, mapped onto the CurseForge category ids
# they actually correspond to. CurseForge has 52 categories and most of them
# are one mod family ("Thermal Expansion", "Twilight Forest"), which is far
# too fine a grain to gamble with -- these are the groupings a person thinks
# in when they say "give me a tech pack with no magic".
#
# `API and Library` (421) is deliberately in no bucket: libraries are what a
# roll pulls in as dependencies, never what it deals.

CATEGORIES: list[dict] = [
    {"key": "tech", "title": "Tech", "glyph": "⚙", "color": "#7AA7FF",
     "cf": [412, 413, 415, 417, 420, 4843, 4558, 6484, 4545, 9049, 6954],
     "mr": ["technology", "storage", "transportation"]},
    {"key": "magic", "title": "Magic", "glyph": "✦", "color": "#C77DFF",
     "cf": [419, 4485, 430], "mr": ["magic"]},
    {"key": "adventure", "title": "Adventure", "glyph": "⚔", "color": "#FFC44D",
     "cf": [422, 434, 10775, 7669], "mr": ["adventure", "equipment"]},
    {"key": "building", "title": "Building", "glyph": "▤", "color": "#56D9A3",
     "cf": [409, 9026], "mr": ["decoration"]},
    {"key": "farming", "title": "Farming", "glyph": "❀", "color": "#8FD96A",
     "cf": [416, 436, 10754, 418, 433], "mr": ["food"]},
    {"key": "worldgen", "title": "Worldgen", "glyph": "⛰", "color": "#E8A05C",
     "cf": [406, 407, 410, 408], "mr": ["worldgen"]},
    {"key": "mobs", "title": "Mobs", "glyph": "☠", "color": "#FF8095",
     "cf": [411], "mr": ["mobs"]},
    {"key": "utility", "title": "Utility", "glyph": "⌗", "color": "#9C9EA4",
     "cf": [435, 5191, 423, 414, 6821], "mr": ["utility", "management"]},
    {"key": "performance", "title": "Performance", "glyph": "⚡",
     "color": "#FFE9A8", "cf": [6814], "mr": ["optimization"]},
]
CATEGORY_BY_KEY = {c["key"]: c for c in CATEGORIES}

# Tri-state, matching the UI's click-to-cycle: neutral, preferred, banned.
NEUTRAL, PREFERRED, BANNED = 0, 1, 2
# How many draw tickets a preferred category's mods get. Three is enough to
# feel like a thumb on the scale without turning "preferred" into "only".
PREFERRED_WEIGHT = 3

INTENSITY_LABELS = ["Gentle", "Mild", "Spicy", "Reckless", "Unhinged"]

# --- what makes a mod risky --------------------------------------------

# A jar this big is a dimension mod, a mob pack or a tech monolith. Several
# in one hand is what turns a 4 GB heap into a crash.
HEAVY_BYTES = 15 * 1024 * 1024
# Past this, one mod alone can blow a download cap.
HUGE_BYTES = 30 * 1024 * 1024
# Nobody has touched it in this long, on a version it claims to support.
STALE_DAYS = 550


def _age_days(iso: str | None) -> float:
    if not iso:
        return 9999.0
    try:
        stamp = time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return 9999.0
    return max(0.0, (time.time() - time.mktime(stamp)) / 86400)


# --- the pool -----------------------------------------------------------
#
# A roll needs a candidate list far larger than the hand it deals, and
# building one costs dozens of catalogue requests. So a pool is built once
# per (minecraft, loader, source) and cached on disk: the first roll on a
# fresh version waits a few seconds, every roll after it is instant, and the
# cache survives a restart.

POOL_TTL = 60 * 60 * 24 * 3        # three days; catalogues move slowly
POOL_PAGES = config.ROULETTE_POOL_PAGES
POOL_PAGE_SIZE = 50


def _pool_path(mc: str, loader: str, source: str) -> Path:
    key = hashlib.sha1(f"{mc}|{loader}|{source}".encode()).hexdigest()[:16]
    return config.CACHE_DIR / "roulette" / f"pool-{key}.json"


def _classify(name: str, categories: list[str], size: int, updated: str | None,
              downloads: int, server_side: str | None) -> tuple[str | None, dict]:
    """Work out the one flag a mod carries, and why.

    One flag, not a set: the UI shows a single badge per row and the point is
    to answer "should I worry about this one" at a glance. They are ordered by
    how much they should worry you.
    """
    reasons: dict[str, Any] = {}
    lowered = [c.lower() for c in categories]

    client = False
    if server_side == "unsupported":
        client, reasons["client"] = True, "its author lists server_side: unsupported"
    elif packs.is_client_only_jar(name.replace(" ", "-") + ".jar"):
        client, reasons["client"] = True, "the name matches a known client-only mod"
    if client:
        return "CLIENT", reasons

    maybe_client = False
    if server_side == "optional":
        maybe_client, reasons["client"] = True, "its author lists server_side: optional"
    elif "cosmetic" in lowered or "map and information" in lowered:
        maybe_client = True
        reasons["client"] = "it is filed under a client-leaning category"
    if maybe_client:
        return "CLIENT?", reasons

    stale = _age_days(updated)
    if stale > STALE_DAYS:
        reasons["chaos"] = f"nothing has changed in it for {stale / 365:.1f} years"
        return "CHAOS", reasons

    if size and size >= HEAVY_BYTES:
        reasons["heavy"] = f"the jar alone is {size / 1048576:.0f} MB"
        return "HEAVY", reasons

    return None, reasons


def _bucket(cf_categories: list[str], mr_categories: list[str]) -> str | None:
    """Which of the nine buckets a catalogue entry belongs in."""
    names = {c.lower() for c in cf_categories} | {c.lower() for c in mr_categories}
    best = None
    for cat in CATEGORIES:
        titles = {
            "tech": {"technology", "processing", "storage", "automation",
                     "redstone", "energy", "energy, fluid, and item transport",
                     "create", "applied energistics 2", "refined storage",
                     "integrated dynamics", "transportation"},
            "magic": {"magic", "blood magic", "thaumcraft"},
            "adventure": {"adventure and rpg", "armor, tools, and weapons",
                          "horror", "twilight forest", "adventure", "equipment"},
            "building": {"structures", "creativemode", "decoration"},
            "farming": {"farming", "food", "farmer's delight", "genetics",
                        "forestry"},
            "worldgen": {"world gen", "biomes", "dimensions",
                         "ores and resources", "worldgen"},
            "mobs": {"mobs"},
            "utility": {"server utility", "utility & qol", "map and information",
                        "player transport", "bug fixes", "utility", "management"},
            "performance": {"performance", "optimization"},
        }[cat["key"]]
        if names & titles:
            # First match wins, and CATEGORIES is ordered from most specific
            # intent (Tech) to most generic (Performance), so a Create addon
            # lands in Tech rather than in Utility.
            best = cat["key"]
            break
    return best


async def _curseforge_pool(mc: str, loader: str, on_step=None) -> list[dict]:
    """Every mod CurseForge will show us for this version, by category."""
    out: dict[int, dict] = {}
    total = len(CATEGORIES) * POOL_PAGES
    done = 0
    for cat in CATEGORIES:
        for cf_id in cat["cf"][:3]:      # the three broadest ids per bucket
            for page in range(POOL_PAGES):
                try:
                    res = await curseforge.search(
                        class_id=curseforge.CLASS_MODS, game_version=mc,
                        mod_loader=loader, category_id=cf_id,
                        sort_field=6, index=page * POOL_PAGE_SIZE,
                        page_size=POOL_PAGE_SIZE,
                    )
                except Exception:
                    break
                items = res.get("items") or []
                for m in items:
                    if m["id"] in out:
                        continue
                    # `latestFiles` carries only a handful of recent builds
                    # and frequently has nothing for the version we asked
                    # about -- CurseForge still returns the mod, because a
                    # compatible file does exist, just not in this list.
                    # Taking files[0] regardless (which an earlier draft did)
                    # put a 1.16.5 Forge jar in a 1.21.1 NeoForge pool. So a
                    # non-matching file is treated as "not known yet" and the
                    # real one is resolved when the hand is dealt.
                    files = m.get("latest_files") or []
                    # Third-party distribution is a per-project switch, so a
                    # project whose every listed file has no download address
                    # has it turned off. Such a mod never enters the pool: a
                    # roll is only worth dealing if every card can be fetched
                    # without going around its author.
                    if files and all(curseforge.is_distribution_blocked(f)
                                     for f in files):
                        continue
                    picked = next(
                        (f for f in files
                         if mc in (f.get("game_versions") or [])
                         and (not f.get("loaders")
                              or loader.lower() in [l.lower() for l in f["loaders"]])),
                        None,
                    )
                    out[m["id"]] = {
                        "source": "curseforge",
                        "project_id": m["id"],
                        "file_id": picked.get("file_id") if picked else None,
                        "name": m.get("name") or "",
                        "slug": m.get("slug"),
                        "summary": m.get("summary") or "",
                        "logo": m.get("logo"),
                        "url": m.get("url"),
                        "downloads": m.get("downloads") or 0,
                        "updated": m.get("updated"),
                        "size": (picked.get("size") or 0) if picked else 0,
                        "file_name": picked.get("file_name") if picked else None,
                        "release_type": (picked.get("release_type")
                                         if picked else "release"),
                        "cf_categories": m.get("categories") or [],
                        "mr_categories": [],
                        "category": cat["key"],
                        "server_side": None,
                    }
                if len(items) < POOL_PAGE_SIZE:
                    break
            done += 1
            if on_step:
                on_step(done, total)
    return list(out.values())


async def _modrinth_pool(mc: str, loader: str) -> list[dict]:
    """Modrinth's half. It states sides outright, which is worth a lot here."""
    if not config.MODRINTH_ENABLED:
        return []
    out: dict[str, dict] = {}
    for cat in CATEGORIES:
        for mr_cat in cat["mr"][:2]:
            for page in range(2):
                try:
                    res = await modrinth.search(
                        project_type="mod", game_version=mc, loader=loader,
                        categories=[mr_cat], index=page * 50, page_size=50,
                        sort="downloads",
                    )
                except Exception:
                    break
                items = res.get("items") or []
                for m in items:
                    if m["id"] in out:
                        continue
                    out[m["id"]] = {
                        "source": "modrinth",
                        "project_id": m["id"],
                        "file_id": None,          # resolved when the hand is taken
                        "name": m.get("name") or "",
                        "slug": m.get("slug"),
                        "summary": m.get("summary") or "",
                        "logo": m.get("logo"),
                        "url": m.get("url"),
                        "downloads": m.get("downloads") or 0,
                        "updated": m.get("updated"),
                        "size": 0,
                        "file_name": None,
                        "release_type": "release",
                        "cf_categories": [],
                        "mr_categories": m.get("categories") or [],
                        "category": cat["key"],
                        "server_side": m.get("server_side"),
                    }
                if len(items) < 50:
                    break
    return list(out.values())


async def build_pool(mc: str, loader: str, source: str = "both",
                     job: Job | None = None, refresh: bool = False) -> dict:
    """The candidate list for a set of constraints, cached on disk."""
    path = _pool_path(mc, loader, source)
    if not refresh and path.exists():
        try:
            cached = json.loads(path.read_text())
            if time.time() - cached.get("built_at", 0) < POOL_TTL:
                return cached
        except (OSError, ValueError):
            pass

    def step(done: int, total: int) -> None:
        if job:
            job.set_step(f"Reading the catalogue ({done}/{total})",
                         5 + 75 * done / max(total, 1))

    mods: list[dict] = []
    if source in ("curseforge", "both"):
        mods += await _curseforge_pool(mc, loader, on_step=step)
    if source in ("modrinth", "both"):
        if job:
            job.set_step("Reading Modrinth", 82)
        mods += await _modrinth_pool(mc, loader)

    # De-duplicate across sources on a normalised name: the same mod on both
    # catalogues should be one candidate, and CurseForge wins because its
    # entry carries a concrete file id.
    by_key: dict[str, dict] = {}
    for m in mods:
        key = re.sub(r"[^a-z0-9]", "", (m["name"] or "").lower())
        if not key:
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = m
        elif existing["source"] == "modrinth" and m["source"] == "curseforge":
            m["server_side"] = m.get("server_side") or existing.get("server_side")
            by_key[key] = m
        elif existing["source"] == "curseforge" and m["source"] == "modrinth":
            # Keep the CurseForge entry, but take Modrinth's side statement --
            # it is the only authoritative one either catalogue offers.
            existing["server_side"] = existing.get("server_side") or m.get("server_side")

    final = []
    for m in by_key.values():
        bucket = _bucket(m["cf_categories"], m["mr_categories"]) or m["category"]
        m["category"] = bucket
        flag, why = _classify(m["name"], m["cf_categories"] + m["mr_categories"],
                              m["size"], m["updated"], m["downloads"],
                              m.get("server_side"))
        m["flag"] = flag
        m["flag_why"] = why
        m["age_days"] = round(_age_days(m["updated"]))
        final.append(m)

    pool = {
        "built_at": time.time(),
        "minecraft": mc,
        "loader": loader,
        "source": source,
        "mods": sorted(final, key=lambda m: -(m["downloads"] or 0)),
    }
    # An unwritable cache costs speed, never correctness.
    config.write_state(path, json.dumps(pool))
    return pool


# --- constraints --------------------------------------------------------


DEFAULT_CONSTRAINTS: dict[str, Any] = {
    "minecraft": "1.21.1",
    "loader": "NeoForge",
    "source": "both",
    "count": 120,          # the design's default hand
    "intensity": 3,
    # Millions of downloads; 0 means no floor. The design shipped 5 as the
    # default and labelled it "5M dl" while the code only applied it above 5,
    # so the control lied about itself. It now filters at every value, which
    # makes the default a real (and reasonable) quality bar.
    "quality": 5,
    "categories": {},           # key -> 0 neutral | 1 preferred | 2 banned
    "toggles": {"deps": True, "client": False, "conflict": False, "cap": True},
}


def merge_constraints(raw: dict | None) -> dict:
    c = {**DEFAULT_CONSTRAINTS, **(raw or {})}
    c["toggles"] = {**DEFAULT_CONSTRAINTS["toggles"], **(c.get("toggles") or {})}
    c["categories"] = {k: int(v) for k, v in (c.get("categories") or {}).items()
                       if k in CATEGORY_BY_KEY}
    c["count"] = max(5, min(300, int(c.get("count") or 60)))
    c["intensity"] = max(1, min(5, int(c.get("intensity") or 3)))
    c["quality"] = max(0, min(50, int(c.get("quality") or 0)))
    # A loader the installer cannot set up unattended is not a choice, it is
    # a dead end reached after a two-minute deal. A stored preference from
    # before Quilt was withdrawn lands here and is corrected rather than
    # refused.
    wanted = str(c.get("loader") or "").strip().lower()
    c["loader"] = next((l for l in LOADERS if l.lower() == wanted),
                       DEFAULT_CONSTRAINTS["loader"])
    return c


def eligible(pool_mods: list[dict], c: dict) -> list[dict]:
    """The mods a set of constraints actually allows into the draw.

    Mirrors the rules the UI describes next to each control, in the same
    order, so that the pool counter and the hand can never disagree about
    what was allowed.
    """
    cats, tog = c["categories"], c["toggles"]
    intensity, floor = c["intensity"], c["quality"]
    out = []
    for m in pool_mods:
        if cats.get(m["category"]) == BANNED:
            continue
        flag = m.get("flag")
        if not tog.get("client") and flag == "CLIENT":
            continue
        # "Known-conflicting" is a promise the catalogues cannot keep, so it
        # is read as "unstable or abandoned", which they can.
        if flag == "CHAOS" and not (tog.get("conflict") and intensity >= 4):
            continue
        if tog.get("cap") and m["size"] and m["size"] > HUGE_BYTES and intensity < 3:
            continue
        # The design gated this on `> 5`, which quietly made every setting
        # from 1 to 5 do nothing at all while the label promised otherwise.
        if floor > 0 and (m["downloads"] or 0) < floor * 1_000_000:
            continue
        # Gentle and Mild refuse anything that has not been touched recently
        # or that nobody uses, whatever else it passes.
        if intensity <= 2 and (m["age_days"] > 400 or (m["downloads"] or 0) < 200_000):
            continue
        if intensity == 1 and flag in ("HEAVY", "CLIENT?"):
            continue
        out.append(m)
    return out


def _weight(m: dict, c: dict) -> float:
    """How many tickets this mod holds in the draw.

    Applied as `u ** (1 / weight)`, which is the distribution of the best of
    `weight` uniform draws -- the same thumb on the scale that handing a mod
    three tickets would give, without building the ticket array. A preferred
    category is roughly three times as likely to come up; it is never made
    exclusive, because "preferred" that means "only" is a filter wearing a
    weight's clothes.
    """
    return float(PREFERRED_WEIGHT
                 if c["categories"].get(m["category"]) == PREFERRED else 1)


def deal(pool_mods: list[dict], c: dict, holds: list[str] | None = None,
         rng: random.Random | None = None) -> list[dict]:
    """Draw a hand at random from what the constraints allow.

    `rng` exists so the tests can pin a draw; nothing in the app passes one,
    and a roll is a fresh draw every time.
    """
    rng = rng or random
    allowed = eligible(pool_mods, c)
    held_names = set(holds or [])
    kept = [m for m in allowed if m["name"] in held_names]
    rest = [m for m in allowed if m["name"] not in held_names]

    scored = [(rng.random() ** (1.0 / _weight(m, c)), m["name"], m) for m in rest]
    scored.sort(key=lambda row: (-row[0], row[1]))
    want = max(0, c["count"] - len(kept))
    return kept + [row[2] for row in scored[:want]]


def reroll_one(hand: list[dict], target: str, pool_mods: list[dict], c: dict,
               rng: random.Random | None = None) -> list[dict]:
    """Replace a single mod in a hand, leaving every other slot alone."""
    rng = rng or random
    taken = {m["name"] for m in hand}
    candidates = [m for m in eligible(pool_mods, c) if m["name"] not in taken]
    if not candidates:
        raise ValueError("nothing else fits your constraints for that slot")
    best = max(candidates,
               key=lambda m: (rng.random() ** (1.0 / _weight(m, c)), m["name"]))
    return [best if m["name"] == target else m for m in hand]


async def reroll_verified(hand: list[dict], target: str, pool_mods: list[dict],
                          c: dict, tries: int = 6) -> tuple[list[dict], list[dict]]:
    """Reroll one slot into a mod that is known to exist for this version.

    `reroll_one` picks from the pool, and a pool entry is a catalogue listing,
    not a file -- it can perfectly well have no build for this exact
    Minecraft version. Dropping one of those into a verified hand put the one
    unverified mod in the pack back, through the only door left open, and it
    would not surface until the install. So the replacement is pinned here,
    and a replacement that cannot be pinned is replaced in turn.
    """
    taken = {m["name"] for m in hand}
    refused: list[dict] = []
    for _ in range(max(1, tries)):
        pruned = [m for m in pool_mods
                  if m["name"] not in taken
                  and m["name"] not in {r["name"] for r in refused}]
        candidate = reroll_one(hand, target, pruned, c)
        picked = next((m for m in candidate if m["name"] not in taken), None)
        if picked is None:
            break
        fresh = dict(picked)
        resolved, problems = await resolve_hand([fresh], c)
        if resolved:
            return [resolved[0] if m["name"] == target else m
                    for m in hand], refused
        refused.extend(problems or [{"name": picked["name"], "kind": "no-file",
                                     "reason": "no usable file"}])
    raise ValueError(
        "nothing else in the pool has a verified build for that slot")


# --- reading the hand ---------------------------------------------------


def summarise(hand: list[dict], c: dict, host: dict | None = None) -> dict:
    """Download size, heap estimate and the odds panel."""
    total_bytes = sum(m["size"] for m in hand)
    heavy = [m for m in hand if m.get("flag") == "HEAVY"]
    chaos = [m for m in hand if m.get("flag") == "CHAOS"]
    clients = [m for m in hand if m.get("flag") in ("CLIENT", "CLIENT?")]

    # Dependencies roughly triple a hand's jar count in practice, and the
    # heap a pack needs tracks jar count far better than it tracks download
    # size. Both numbers are labelled as estimates in the UI.
    est_jars = int(round(len(hand) * 3.4))
    heap_need = round(min(16.0, 1.5 + est_jars * 0.018 + len(heavy) * 0.35), 1)

    ceiling = 4.0
    if host and host.get("total_ram_gb"):
        ceiling = max(1.0, min(host["total_ram_gb"] - 4.0,
                               (host.get("available_ram_gb") or 0) - 1.0))
        ceiling = round(max(1.0, ceiling), 1)

    if chaos:
        odds = {
            "title": "This hand will probably not boot first time",
            "confidence": "LIKELY", "tone": "bad", "glyph": "✕",
            "reasons": [
                f"{len(chaos)} rolled mod{'s are' if len(chaos) > 1 else ' is'} "
                f"unmaintained at intensity {c['intensity']}",
                f"Estimated heap of {heap_need} GB against a {ceiling} GB ceiling here",
                "BlessForge will still install it, then hand you the crash review",
            ],
        }
    elif heap_need > ceiling:
        odds = {
            "title": "It will boot, then run out of memory under load",
            "confidence": "LIKELY", "tone": "warn", "glyph": "▲",
            "reasons": [
                f"{len(heavy)} heavyweight mod{'' if len(heavy) == 1 else 's'} in the hand",
                f"Estimated {heap_need} GB heap need against a {ceiling} GB safe ceiling",
                "Drop the mod count or ban Worldgen to bring it back under",
            ],
        }
    else:
        odds = {
            "title": "This hand should boot cleanly",
            "confidence": "LIKELY", "tone": "good", "glyph": "✓",
            "reasons": [
                "Every rolled mod has a server-side code path",
                f"Estimated {heap_need} GB heap fits inside the {ceiling} GB ceiling",
                (f"{len(clients)} mod{'' if len(clients) == 1 else 's'} lean client-side "
                 "and will be caught by the review step") if clients
                else "Nothing client-only survived the constraints",
            ],
        }

    return {
        "mods": len(hand),
        "estimated_jars": est_jars,
        "download_mb": round(total_bytes / 1048576, 1),
        "heap_gb": heap_need,
        "heap_ceiling_gb": ceiling,
        "heavy": len(heavy),
        "chaos": len(chaos),
        "client_leaning": len(clients),
        "odds": odds,
        "intensity": INTENSITY_LABELS[c["intensity"] - 1],
    }


# --- turning a hand into a modpack --------------------------------------


# Last resort only. A real manifest id carries the loader build too
# ('forge-47.4.12'), and the shape differs per family, so the id is normally
# resolved from CurseForge's catalogue -- see curseforge.loader_build_id.
# Writing one of these bare names is what made every export before this fail
# to import with MinecraftUnsupportedModLoader.
LOADER_MANIFEST_ID = {
    "forge": "forge", "neoforge": "neoforge", "fabric": "fabric",
}

# What the roulette will roll for. Quilt is deliberately absent: `packs.py`
# cannot install a Quilt loader unattended ("Quilt and unknown loaders must be
# installed manually"), so every Quilt roll ended as an instance that could
# never start, after the user had waited through the whole deal. BlessForge
# still *imports* a Quilt pack somebody hands it -- this is only about what
# the roulette is allowed to promise.
LOADERS = ["NeoForge", "Forge", "Fabric"]


def _loader_ok(declared: list[str], loader: str, mc: str) -> bool:
    """Whether a build's declared loaders include one this server can run.

    Deliberately permissive about a build that declares none: CurseForge only
    tags a file with a loader when the author set one, and a lot of perfectly
    good library jars carry no tag at all. Refusing those would drop half the
    libraries in the catalogue for a problem that does not exist.
    """
    have = {d.lower() for d in (declared or [])}
    want = (loader or "").lower()
    if not have or not want:
        return True
    if want == "neoforge":
        # NeoForge for 1.20.1 is a Forge fork and still reads mods.toml; from
        # 1.20.2 the descriptor was renamed and a Forge-only jar cannot load.
        return "neoforge" in have or ("forge" in have and mc.startswith("1.20.1"))
    return want in have


def _mc_ok(declared: list[str], mc: str) -> tuple[bool, str]:
    """Whether a build declares the exact Minecraft version. Exactness matters.

    A file listed for 1.21 and not 1.21.1 is the single most common way a
    rolled pack fails to boot, and it is invisible in a filename -- so this
    reads what the publisher actually declared rather than trusting the
    catalogue's search filter, which is looser than it looks.
    """
    have = [str(v) for v in (declared or [])]
    if not have:
        return True, "untagged"
    if mc in have:
        return True, "exact"
    line = mc.rsplit(".", 1)[0]
    if any(v == line or v.startswith(line + ".") for v in have):
        return False, "same-line"
    return False, "mismatch"


def _pick_build(builds: list[dict], mc: str, loader: str) -> tuple[dict | None, str]:
    """The best usable build of one mod, and why the others were rejected.

    Order of preference: an exact-version stable release, then an
    exact-version build of any kind, then nothing. A build tagged for a
    neighbouring patch is *not* accepted -- "probably fine" is what makes a
    120-mod roll fail on its first start with no clue which mod did it.
    """
    usable, near, wrong_loader = [], [], 0
    for b in builds:
        if not _loader_ok(b.get("loaders") or [], loader, mc):
            wrong_loader += 1
            continue
        ok, how = _mc_ok(b.get("game_versions") or [], mc)
        if ok:
            usable.append((b, how))
        elif how == "same-line":
            near.append(b)
    if not usable:
        if near:
            line = mc.rsplit(".", 1)[0]
            return None, (f"only publishes builds for a neighbouring patch of "
                          f"{line}, not {mc}")
        if wrong_loader:
            return None, f"has no {loader} build for {mc}"
        return None, f"no build for {mc}"

    exact = [b for b, how in usable if how == "exact"]
    pool = exact or [b for b, _ in usable]
    stable = [b for b in pool if b.get("release_type") == "release"]
    chosen = (stable or pool)[0]
    return chosen, "exact" if exact and chosen in exact else "untagged"


async def resolve_hand(hand: list[dict], c: dict, job: Job | None = None
                       ) -> tuple[list[dict], list[dict]]:
    """Pin every mod in a hand to a real, compatible, downloadable file.

    This is what makes a hand installable rather than merely plausible.
    Neither catalogue's search results can be trusted for it: CurseForge's
    `latestFiles` often has no build for the version being asked about, and a
    Modrinth project is not a file at all. So the newest matching build is
    looked up per mod, concurrently -- and then *verified* against what the
    publisher declared, rather than assumed correct because the search filter
    was asked nicely.

    That verification is the change that matters. The filter is looser than it
    looks: a file tagged 1.21 comes back for a 1.21.1 query, installs without
    complaint, and takes the server down on first boot with a registry error
    naming nothing useful. Anything that does not declare the exact version
    and a runnable loader is dropped here, with a reason, and the caller deals
    a replacement.

    It also back-fills the real file size, which is what the HEAVY flag, the
    download estimate and the heap estimate are all computed from.
    """
    problems: list[dict] = []
    mc, loader = c["minecraft"], c["loader"].lower()
    sem = asyncio.Semaphore(8)
    done = 0
    lock = asyncio.Lock()

    async def one(m: dict) -> None:
        nonlocal done
        async with sem:
            try:
                if m["source"] == "curseforge":
                    builds = await curseforge.list_files(
                        int(m["project_id"]), game_version=mc,
                        mod_loader=loader, page_size=30)
                    if not builds:
                        # The filtered query comes back empty for a mod whose
                        # files are tagged oddly; the unfiltered list is then
                        # checked properly by _pick_build anyway.
                        builds = await curseforge.list_files(
                            int(m["project_id"]), page_size=50)
                else:
                    builds = await modrinth.list_versions(
                        str(m["project_id"]), game_version=mc, loader=loader)
                    if not builds:
                        builds = await modrinth.list_versions(str(m["project_id"]))
            except Exception as e:
                problems.append({"name": m["name"], "reason": str(e),
                                 "kind": "lookup-failed"})
                return
            finally:
                async with lock:
                    done += 1
                    if job and done % 10 == 0:
                        job.set_step(f"Pinning builds ({done}/{len(hand)})",
                                     6 + 10 * done / max(len(hand), 1))
            if not builds:
                problems.append({
                    "name": m["name"], "kind": "no-builds",
                    "reason": f"has no {c['loader']} build for {mc}"})
                return
            if m["source"] == "curseforge":
                # A pool cached before blocked projects were filtered out
                # can still deal one; it is dropped here instead, and the
                # caller deals a replacement like for any other drop.
                fetchable = [b for b in builds
                             if not curseforge.is_distribution_blocked(b)]
                if not fetchable:
                    problems.append({
                        "name": m["name"], "kind": "distribution-blocked",
                        "reason": "its author does not allow third-party "
                                  "downloads"})
                    return
                builds = fetchable

            best, why = _pick_build(builds, mc, loader)
            if not best:
                problems.append({"name": m["name"], "kind": "incompatible",
                                 "reason": why})
                return

            m["file_id"] = best.get("file_id")
            m["file_name"] = best.get("file_name")
            m["size"] = best.get("size") or 0
            m["download_url"] = best.get("download_url")
            m["release_type"] = best.get("release_type")
            m["build_version"] = (best.get("version_number")
                                  or best.get("display_name"))
            m["declared_versions"] = best.get("game_versions") or []
            m["declared_loaders"] = best.get("loaders") or []
            m["version_match"] = why
            # What this build says it needs, embeds, and will not sit beside.
            # Only available once a specific file has been chosen -- a project
            # has no dependencies, a file does -- which is why the kinship
            # pass runs after this one and not before it.
            m["relations"] = kinship.relations_from_build(best, m["source"])
            # CurseForge ships the file's own hashes; keeping the sha1 is what
            # lets the side-check below be exact rather than a guess from the
            # mod's name.
            for h in best.get("hashes") or []:
                if h.get("algo") == 1 or str(h.get("algo")) == "1":
                    m["sha1"] = h.get("value")
            # The size is only now known, so the flag it drives is only now
            # correct.
            if m.get("flag") is None and m["size"] >= HEAVY_BYTES:
                m["flag"] = "HEAVY"
                m.setdefault("flag_why", {})["heavy"] = (
                    f"the jar alone is {m['size'] / 1048576:.0f} MB")

    if hand:
        await asyncio.gather(*(one(m) for m in hand))

    dropped = {p["name"] for p in problems}
    resolved = [m for m in hand
                if m["name"] not in dropped and m.get("file_id")]
    for m in hand:
        if m["name"] not in dropped and not m.get("file_id"):
            problems.append({"name": m["name"], "kind": "no-file",
                             "reason": "no usable file"})

    await _mark_sides(resolved)
    return resolved, problems


# --- does this hand hold together? --------------------------------------
#
# Everything above asks "is this mod real and will it load". This asks the
# question that was missing: "do these mods belong in the same folder". It is
# where the two complaints that started this rewrite are answered -- add-ons
# dealt without their parent, and mods dealt against each other.


def _pool_index(pool_mods: list[dict]) -> tuple[dict, dict]:
    """The pool keyed the two ways a parent gets identified."""
    by_ref = {kinship.ref(m["source"], m["project_id"]): m for m in pool_mods}
    by_name = {kinship.norm(m["name"]): m for m in pool_mods}
    return by_ref, by_name


def _record_from_project(project: dict, source: str) -> dict:
    """A pool-shaped candidate from a catalogue entry we went and fetched.

    Only used for a parent the pool never saw. The pool is a sample of the
    catalogue -- the most-downloaded few hundred per category -- and a rolled
    add-on's parent is occasionally outside it. Refusing to adopt in that case
    would drop the add-on for a reason that is about our sampling rather than
    about the mod.
    """
    cats = project.get("categories") or []
    return {
        "source": source,
        "project_id": project.get("id"),
        "file_id": None,
        "name": project.get("name") or "",
        "slug": project.get("slug"),
        "summary": project.get("summary") or "",
        "logo": project.get("logo"),
        "url": project.get("url"),
        "downloads": project.get("downloads") or 0,
        "updated": project.get("updated"),
        "size": 0,
        "file_name": None,
        "release_type": "release",
        "cf_categories": cats if source == "curseforge" else [],
        "mr_categories": cats if source == "modrinth" else [],
        "category": _bucket(cats if source == "curseforge" else [],
                            cats if source == "modrinth" else []) or "utility",
        "server_side": project.get("server_side"),
        "flag": None,
        "flag_why": {},
        "age_days": round(_age_days(project.get("updated"))),
    }


async def vet_hand(hand: list[dict], c: dict, pool_mods: list[dict],
                   job: Job | None = None, adopt: bool = True
                   ) -> tuple[list[dict], list[dict], dict]:
    """Drop what does not belong here, and fetch what should have come along.

    Three rules, in this order, because each one changes what the next sees:

      1. **A bare library is not a roll.** It adds nothing anybody can point
         at in-game, and if the hand needs it the dependency pass installs it
         anyway -- so rolling one spends a slot on a jar the user would have
         got for free. Modpack-Inspector calls these out in an installed pack;
         here they are caught before the pack exists.

      2. **An add-on brings its parent, or it leaves.** This is the one that
         was actually reported: a hand full of Create add-ons and no Create.
         The parent is adopted where the constraints allow it -- as a
         companion, not as a slot, the same standing a dependency has -- and
         where they do not, the add-on goes instead of quietly dragging a
         headline mod in at install time that nobody chose.

      3. **One of each family.** Two recipe viewers, two light engines, four
         biome generators: each passes every check the installer makes and the
         pack still comes apart. The catalogues' own incompatibility
         declarations are honoured first; `kinship.CONFLICT_FAMILIES` covers
         the far larger set nobody declares.

    Returns (hand, problems, report). The report is the Inspector's read of
    what survived, which is what the UI's Lineage panel is drawn from.
    """
    if not hand:
        return hand, [], kinship.study([], {}, {})

    if job:
        job.set_step("Checking the hand holds together", 90)

    by_ref, by_name = _pool_index(pool_mods)
    problems: list[dict] = []
    order = {m["name"]: i for i, m in enumerate(hand)}
    live = {m["name"]: m for m in hand}

    def drop(name: str, kind: str, reason: str) -> None:
        if live.pop(name, None) is not None:
            problems.append({"name": name, "kind": kind, "reason": reason})

    refs: set[str] = set()
    for m in hand:
        refs |= set((m.get("relations") or {}).get("requires") or [])
    described = await kinship.describe_refs(refs)
    report = kinship.study(hand, described, by_name)

    # 1. bare libraries
    for m in hand:
        why = kinship.looks_like_library(
            m["name"], (m.get("cf_categories") or []) + (m.get("mr_categories") or []),
            m.get("summary") or "")
        if why and not m.get("adopted_for"):
            drop(m["name"], "library",
                 f"{why} — it will still be installed if anything needs it")

    # 2. add-ons whose parent is not here
    adopted: list[dict] = []
    for orphan in report["orphans"]:
        if orphan["name"] not in live:
            continue                      # already dropped as a library
        parent_name = orphan.get("parent") or "?"
        row = next((r for r in report["rows"] if r["name"] == orphan["name"]), {})
        candidate = None
        if adopt:
            parent_ref = (row.get("parent") or {}).get("ref")
            candidate = by_ref.get(parent_ref) if parent_ref else None
            candidate = candidate or by_name.get(kinship.norm(parent_name))
            if candidate is None and parent_ref:
                candidate = await _fetch_parent(parent_ref)
        if candidate is None:
            drop(orphan["name"], "orphan-addon",
                 f"it is an add-on for {parent_name}, which is not in this "
                 "hand and could not be found for this version")
            continue
        if c["categories"].get(candidate.get("category")) == BANNED:
            drop(orphan["name"], "orphan-addon",
                 f"it is an add-on for {parent_name}, and you banned "
                 f"{candidate['category']}")
            continue
        if candidate["name"] in live or any(a["name"] == candidate["name"]
                                            for a in adopted):
            continue
        adopted.append(dict(candidate, adopted_for=orphan["name"]))

    if adopted:
        if job:
            job.set_step(f"Bringing in {len(adopted)} mods their add-ons need", 91)
        resolved, adopt_problems = await resolve_hand(adopted, c, job=None)
        got = {m["name"] for m in resolved}
        for m in resolved:
            live[m["name"]] = m
            order.setdefault(m["name"], len(order))
        for a in adopted:
            if a["name"] in got:
                continue
            # The parent exists but has no build for this version, so the
            # add-on has nothing to attach to either.
            reason = next((p["reason"] for p in adopt_problems
                           if p["name"] == a["name"]), "has no build here")
            drop(a["adopted_for"],
                 "orphan-addon",
                 f"it is an add-on for {a['name']}, which {reason}")

    # 3. families and declared incompatibilities, recomputed on what is left
    survivors = sorted(live.values(), key=lambda m: order.get(m["name"], 0))
    for clash in kinship.find_conflicts(survivors):
        for name in clash["drop"]:
            drop(name, "conflict",
                 f"{clash['title'].lower()}: {clash['why']} — kept "
                 f"{clash['keep']}")

    final = sorted(live.values(), key=lambda m: order.get(m["name"], 0))
    # The report is re-read on the final hand so the UI never shows a family
    # or a clash that has already been resolved away.
    report = kinship.study(final, described, by_name)
    if job and problems:
        job.log_line(f"{len(problems)} mods did not belong in the hand: "
                     + ", ".join(sorted({p['kind'] for p in problems})))
    return final, problems, report


async def _fetch_parent(parent_ref: str) -> dict | None:
    """One catalogue lookup for a parent the pool never sampled."""
    source, _, pid = parent_ref.partition(":")
    try:
        if source == "curseforge":
            project = await curseforge.get_mod(int(pid))
        else:
            project = await modrinth.get_project(pid)
    except Exception:
        return None
    if not project or not project.get("name"):
        return None
    return _record_from_project(project, source)


async def fill_hand(pool_mods: list[dict], c: dict,
                    holds: list[str] | None = None, job: Job | None = None,
                    max_rounds: int = 4) -> dict:
    """Deal, verify, and keep dealing until the hand is the size it promised.

    Over-dealing once and truncating -- what this used to do -- works only
    while few mods drop out. On a version the catalogues have half-updated
    to, a third of a 120-mod roll can fail verification, and the user gets an
    80-mod pack having asked for 120 with nothing saying why.

    So it deals in rounds: verify, vet, count what survived, deal
    replacements for exactly the shortfall out of the mods not already tried,
    and stop when the hand is full or the pool has nothing left to offer.

    Adopted parents are companions, not slots. A hand of 120 means 120 mods
    somebody rolled; the Create that a Create add-on dragged in sits beside
    them the way a dependency does, and is not one of the 120.
    """
    want = c["count"]
    kept: list[dict] = []
    all_problems: list[dict] = []
    tried: set[str] = set()
    report: dict = kinship.study([], {}, {})
    rounds = 0

    def rolled(mods: list[dict]) -> list[dict]:
        return [m for m in mods if not m.get("adopted_for")]

    while len(rolled(kept)) < want and rounds < max_rounds:
        rounds += 1
        need = want - len(rolled(kept))
        # Over-deal on the first round only; later rounds are top-ups and a
        # big over-deal there would mostly re-check mods already rejected.
        ask = min(len(pool_mods), int(need * (1.35 if rounds == 1 else 1.15)) + 4)
        round_c = dict(c, count=ask)
        available = [m for m in pool_mods if m["name"] not in tried]
        if not available:
            break
        dealt = [m for m in deal(available, round_c, holds if rounds == 1 else [])
                 if m["name"] not in tried
                 and m["name"] not in {k["name"] for k in kept}]
        if not dealt:
            break
        tried |= {m["name"] for m in dealt}
        if job:
            job.set_step(
                f"Verifying {len(dealt)} mods have a real {c['loader']} "
                f"{c['minecraft']} build"
                + (f" (round {rounds})" if rounds > 1 else ""),
                min(88, 20 + rounds * 14))
        resolved, problems = await resolve_hand(dealt, c, job=job)
        all_problems.extend(problems)

        # Side information only arrives with the file, so a mod the pool
        # believed was server-safe can turn out not to be. Honour the toggle
        # now that the truth is known.
        if not c["toggles"].get("client"):
            held = set(holds or [])
            still = []
            for m in resolved:
                if m.get("flag") == "CLIENT" and m["name"] not in held:
                    all_problems.append({
                        "name": m["name"], "kind": "client-only",
                        "reason": (m.get("flag_why") or {}).get(
                            "client", "client-only"),
                    })
                else:
                    still.append(m)
            resolved = still

        kept.extend(resolved)
        # Vet the whole hand rather than the round: a clash is a property of
        # two mods that may have arrived in different rounds, and a parent
        # adopted in round 1 satisfies an add-on dealt in round 3.
        kept, vetted_out, report = await vet_hand(kept, c, pool_mods, job=job)
        all_problems.extend(vetted_out)
        tried |= {p["name"] for p in vetted_out}
        if job and rounds > 1:
            job.log_line(
                f"Round {rounds}: {len(resolved)} more passed verification "
                f"({len(rolled(kept))}/{want})")

    # Trim to size on the rolled mods only, then keep the companions whose
    # add-on survived the trim. Truncating the flat list would cut a parent
    # loose from the add-on that asked for it -- which is the exact bug this
    # whole pass exists to prevent.
    keeping = {m["name"] for m in rolled(kept)[:want]}
    hand = [m for m in kept
            if m["name"] in keeping
            or (m.get("adopted_for") and m["adopted_for"] in keeping)]

    # One last pass, on exactly the hand that is going out. Two reasons, and
    # both were live bugs:
    #
    #   * The rounds over-deal, so the report from the last round in the loop
    #     describes forty-nine mods when thirty-seven are being handed back.
    #     The Lineage panel is drawn from that report, so it named families
    #     whose members were not in the list underneath it.
    #
    #   * The trim can orphan an add-on. If a parent and its add-on were BOTH
    #     dealt, and the parent falls outside the first `want`, cutting it
    #     re-creates the exact bug this pass exists to prevent -- it comes
    #     back as a companion here instead.
    hand, trimmed_out, report = await vet_hand(hand, c, pool_mods, job=job)
    all_problems.extend(trimmed_out)
    adopted = [m for m in hand if m.get("adopted_for")]
    short = want - len(rolled(hand))
    dropped_for_fit = sum(1 for p in all_problems
                          if p["kind"] in ("library", "conflict", "orphan-addon"))
    return {
        "hand": hand,
        "dropped": all_problems,
        "rounds": rounds,
        "short": short,
        "adopted": len(adopted),
        "inspection": report,
        "note": (
            f"Dealt {len(rolled(hand))} of the {want} asked for. "
            f"{len(all_problems)} candidates were turned away, and the pool "
            f"ran out of replacements. Pick a Minecraft version the "
            "catalogues have caught up with, or lower the quality floor to "
            "widen the pool."
        ) if short else (
            f"{len(rolled(hand))} mods, every one verified to publish a "
            f"{c['loader']} build for exactly {c['minecraft']}"
            + (f", plus {len(adopted)} brought in by their add-ons"
               if adopted else "")
            + (f". {dropped_for_fit} were turned away for not fitting the "
               "rest of the hand." if dropped_for_fit else ".")
        ),
    }


def compatibility_report(hand: list[dict], c: dict) -> dict:
    """What the hand's compatibility actually looks like, for the odds panel."""
    exact = [m for m in hand if m.get("version_match") == "exact"]
    untagged = [m for m in hand if m.get("version_match") == "untagged"]
    stable = [m for m in hand if m.get("release_type") == "release"]
    prerelease = [m for m in hand
                  if m.get("release_type") in ("alpha", "beta")]
    lines = [f"{len(exact)} of {len(hand)} mods declare {c['minecraft']} "
             f"explicitly."]
    if untagged:
        lines.append(f"{len(untagged)} carry no version tag at all — normally "
                     f"libraries, which is expected.")
    if prerelease:
        lines.append(f"{len(prerelease)} are alpha or beta builds, because no "
                     f"stable release exists for {c['minecraft']}.")
    return {
        "verified": len(exact), "untagged": len(untagged),
        "stable": len(stable), "prerelease": len(prerelease),
        "total": len(hand), "lines": lines,
    }


async def _mark_sides(hand: list[dict]) -> None:
    """Ask Modrinth which of these mods a server can actually run.

    Neither the CurseForge catalogue nor a mod's name tells you whether it
    has a server-side code path, and for a NeoForge or Forge jar the jar's
    own metadata does not either -- only Fabric declares an environment. So
    a CurseForge-only roll had no way to spot a client mod at all, and dealt
    things like ItemZoom into a server pack.

    Modrinth does state it, and identifies a file by SHA-1, which CurseForge
    hands us with the file metadata. Two bulk calls settle the whole hand
    exactly, the same mechanism the pre-install review already uses.
    """
    if not config.MODRINTH_ENABLED:
        return
    by_hash = {m["sha1"]: m for m in hand if m.get("sha1") and not m.get("server_side")}
    if not by_hash:
        return
    hashes = list(by_hash)
    versions: dict[str, dict] = {}
    for start in range(0, len(hashes), 250):
        try:
            versions.update(await modrinth.versions_from_hashes(hashes[start:start + 250]))
        except Exception:
            continue
    if not versions:
        return
    try:
        projects = await modrinth.get_projects(
            {v.get("mod_id") for v in versions.values() if v.get("mod_id")})
    except Exception:
        return
    for sha, version in versions.items():
        mod = by_hash.get(sha)
        project = projects.get(version.get("mod_id"))
        if not mod or not project:
            continue
        side = project.get("server_side")
        mod["server_side"] = side
        if side == "unsupported":
            mod["flag"] = "CLIENT"
            mod.setdefault("flag_why", {})["client"] = (
                "its author lists server_side: unsupported")
        elif side == "optional" and mod.get("flag") in (None, "CLIENT?"):
            mod["flag"] = "CLIENT?"
            mod.setdefault("flag_why", {})["client"] = (
                "its author lists server_side: optional")


# Kept as the name the installer calls.
_resolve_files = resolve_hand


async def _with_dependencies(resolved: list[dict], c: dict, job: Job | None = None
                             ) -> tuple[list[dict], list[dict]]:
    """Add whatever the hand needs to actually load.

    A rolled mod is useless without its library, and the design promises
    dependencies "do not count against the hand" -- so they are resolved
    here, appended, and marked so the UI can show them apart from the roll.
    """
    if not c["toggles"].get("deps"):
        return resolved, []

    if job:
        job.set_step("Resolving dependencies", 22)
    have = {(m["source"], str(m["project_id"])) for m in resolved}
    # Shaped exactly like deps.installed_index(): `stems` is a mapping of
    # comparable-name -> filename, not a set. Passing a set made every
    # resolve raise inside the catch below and quietly return no
    # dependencies at all, which is the kind of failure that looks like
    # success.
    files = {m["file_name"] for m in resolved if m.get("file_name")}
    installed_index = {
        "by_project": {f"{m['source']}:{m['project_id']}": m for m in resolved},
        "files": files,
        "stems": {deps._stem(f): f for f in files if f},
    }
    extra: dict[tuple, dict] = {}
    warnings: list[str] = []
    failures: list[str] = []
    blocked_deps: list[tuple[dict, dict]] = []
    sem = asyncio.Semaphore(5)

    async def one(m: dict) -> None:
        async with sem:
            try:
                plan = await deps.resolve(
                    source=m["source"], project_id=m["project_id"],
                    file_id=m["file_id"], mc_version=c["minecraft"],
                    loader=c["loader"].lower(),
                    # Nothing is installed yet -- the hand is the world, so
                    # the resolver treats the roll itself as what is present
                    # and only reports what is genuinely missing.
                    installed=installed_index,
                )
            except Exception as e:
                failures.append(f"{m['name']}: {e}")
                return
            for dep in plan.get("dependencies", []):
                key = (dep["source"], str(dep["project_id"]))
                if key in have or key in extra:
                    continue
                if dep.get("distribution_blocked"):
                    # The mod cannot load without it and it cannot be
                    # fetched. Shipping the pair would ship a crash, so the
                    # mod that needs it goes instead.
                    blocked_deps.append((m, dep))
                    continue
                extra[key] = {
                    "source": dep["source"],
                    "project_id": dep["project_id"],
                    "file_id": dep["file_id"],
                    "name": dep.get("name") or "",
                    "file_name": dep.get("file_name"),
                    "size": dep.get("size") or 0,
                    "logo": dep.get("logo"),
                    "category": "utility",
                    "flag": None,
                    "downloads": 0,
                    "dependency_of": m["name"],
                }

    await asyncio.gather(*(one(m) for m in resolved))
    if blocked_deps:
        gone = {id(m) for m, _ in blocked_deps}
        resolved = [m for m in resolved if id(m) not in gone]
        for m, dep in blocked_deps:
            warnings.append(
                f"Dropped {m['name']}: it needs {dep.get('name') or 'a library'}"
                ", whose author does not allow third-party downloads")
    if extra:
        warnings.append(f"{len(extra)} dependencies were added to the hand")
    if failures:
        warnings.append(
            f"{len(failures)} mods' dependency graphs could not be read: "
            + "; ".join(failures[:3])
        )
    return resolved + list(extra.values()), warnings


def build_export(hand: list[dict], c: dict, name: str,
                 bundled: dict[str, bytes] | None = None,
                 loader_id: str = "") -> bytes:
    """Write a CurseForge modpack archive for a hand.

    The format is the one the CurseForge app itself exports, which means the
    zip this produces can be handed to a friend, imported into their launcher
    -- or dropped straight back into BlessForge's own import screen, which
    reads exactly this shape.

    Modrinth mods have no CurseForge project id and cannot go in `files`, so
    their jars are bundled under `overrides/mods/` instead. That is the same
    thing a person does by hand when they add a mod to a pack they exported.
    """
    cf = [m for m in hand if m["source"] == "curseforge"]
    other = [m for m in hand if m["source"] != "curseforge"]
    loader_key = loader_id or LOADER_MANIFEST_ID.get(
        c["loader"].lower(), c["loader"].lower())

    manifest = {
        "minecraft": {
            "version": c["minecraft"],
            "modLoaders": [{"id": loader_key, "primary": True}],
        },
        "manifestType": "minecraftModpack",
        "manifestVersion": 1,
        "name": name,
        "version": "1.0.0",
        "author": "BlessForge Mod Roulette",
        "files": [
            {"projectID": int(m["project_id"]), "fileID": int(m["file_id"]),
             "required": True}
            for m in cf
        ],
        "overrides": "overrides",
    }

    rows = "\n".join(
        f'<li><a href="{m.get("url") or ""}">{m["name"]}</a></li>' for m in hand
    )
    modlist = f"<ul>\n{rows}\n</ul>\n"

    adopted = [m for m in hand if m.get("adopted_for")]
    readme = (
        f"{name}\n{'=' * len(name)}\n\n"
        f"Rolled by BlessForge Mod Roulette.\n\n"
        f"  roll         {c.get('roll_id', '?')}\n"
        f"  minecraft    {c['minecraft']}\n"
        f"  loader       {c['loader']}\n"
        f"  mods         {len(hand)} ({len(cf)} from CurseForge"
        + (f", {len(other)} bundled" if other else "") + ")\n"
        + (f"  brought in   {len(adopted)} parents of rolled add-ons\n"
           if adopted else "")
        + f"  intensity    {INTENSITY_LABELS[c['intensity'] - 1]}\n\n"
        "There is no seed and this hand will not come round again. Import\n"
        "this zip through BlessForge's Discover screen to install it, or\n"
        "through the CurseForge app to play it.\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        z.writestr("modlist.html", modlist)
        z.writestr("README.txt", readme)
        for path, blob in (bundled or {}).items():
            z.writestr(f"overrides/mods/{path}", blob)
    return buf.getvalue()


# --- accepting a hand ---------------------------------------------------

EXPORT_DIR_NAME = "roulette-exports"


def export_path(roll_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", roll_id)[:40] or "roll"
    return config.DATA_DIR / EXPORT_DIR_NAME / f"{safe}.zip"


async def install_roll(
    job: Job,
    *,
    roll_id: str,
    constraints: dict,
    hand: list[dict],
    server_name: str,
    port: int = 25565,
    motd: str | None = None,
    optimize: bool = True,
) -> dict:
    """Take an accepted hand: build the pack, install it, keep the export.

    Deliberately routed through the ordinary install pipeline rather than a
    private one. A rolled pack gets the same client-only review, the same
    loader repair, the same port and heap handling and the same manifest as
    a pack from the catalogue -- so a server built by the roulette is not a
    second-class instance, and everything that already knows how to diagnose
    a BlessForge server knows how to diagnose this one.
    """
    c = merge_constraints(constraints)
    roll_id = safe_roll_id(roll_id)
    c["roll_id"] = roll_id
    job.set_step("Pinning every mod to a file", 6)

    resolved, problems = await _resolve_files(hand, c, job)
    if not resolved:
        raise RuntimeError(
            "None of the rolled mods had a usable build for "
            f"{c['loader']} {c['minecraft']}. Try a different version."
        )
    for p in problems:
        job.log_line(f"Dropped {p['name']}: {p['reason']}", "warn")

    full, warnings = await _with_dependencies(resolved, c, job)
    for w in warnings:
        job.log_line(w)

    # Modrinth jars have to travel inside the archive, so they are fetched
    # once here and reused for both the export and the install.
    bundled: dict[str, bytes] = {}
    others = [m for m in full if m["source"] != "curseforge"]
    if others:
        job.set_step(f"Fetching {len(others)} non-CurseForge jars", 34)
        async with httpx.AsyncClient(timeout=600, follow_redirects=True) as client:
            for m in others:
                try:
                    r = await client.get(m.get("download_url") or "")
                    r.raise_for_status()
                    bundled[m.get("file_name") or f"{m['name']}.jar"] = r.content
                except Exception as e:
                    job.log_line(f"Could not fetch {m['name']}: {e}", "warn")

    job.set_step("Writing the CurseForge export", 44)
    pack_name = server_name or f"Roulette {roll_id}"
    # The manifest has to name a real loader build, not just the family, or
    # the CurseForge app refuses the import outright. BlessForge's own
    # importer is happy either way, which is how this went unnoticed.
    loader_id = await curseforge.loader_build_id(c["minecraft"], c["loader"])
    if loader_id:
        job.log_line(f"Export targets {loader_id}")
    else:
        job.log_line(
            f"No {c['loader']} build listed for {c['minecraft']}; the export "
            "will name the loader without a version, which BlessForge can "
            "import but the CurseForge app cannot.", "warn")
    archive = build_export(full, c, pack_name, bundled, loader_id)

    path = export_path(roll_id)
    export_bytes = 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(archive)
        export_bytes = len(archive)
        job.log_line(f"Export written: {path.name} ({len(archive) / 1048576:.1f} MB)")
    except OSError as e:
        job.log_line(f"Could not save the export: {e}", "warn")
        roll_id = ""

    # Hand the archive to the ordinary importer. It is the same code path a
    # user's own .zip takes, which is exactly the point.
    job.set_step("Installing the rolled pack", 50)
    from app import uploads
    record = await uploads.store_bytes(archive, f"{pack_name}.zip")
    upload_id = record["upload_id"]

    result = await installer.install_modpack(
        job,
        upload_id=upload_id,
        server_name=server_name,
        port=port,
        motd=motd,
        optimize=optimize,
    )
    # Everything the server's own Overview needs to offer the export back
    # later, written down here because by then the job is gone: the registry
    # lives in memory and does not outlive a restart.
    roll_info = {
        "roll_id": roll_id,
        "export_available": bool(roll_id),
        "export_bytes": export_bytes,
        "export_name": f"{roll_id}.zip" if roll_id else "",
        "rolled_at": time.time(),
        "minecraft": c["minecraft"],
        "loader": c["loader"],
        "intensity": INTENSITY_LABELS[c["intensity"] - 1],
        "hand": len(hand),
        "adopted": sum(1 for m in full if m.get("adopted_for")),
        "curseforge_mods": sum(1 for m in full if m["source"] == "curseforge"),
        "bundled_mods": sum(1 for m in full if m["source"] != "curseforge"),
        "installed": len(full),
        "dependencies": len(full) - len(resolved),
        "dropped": problems,
        "constraints": {k: v for k, v in c.items() if k != "roll_id"},
    }
    result.update({"roll": roll_info})

    # Record it on the instance as well as in the job result. The job is gone
    # by the time anyone wants the export -- the registry is in memory and does
    # not outlive a restart -- and the server's own screen is where someone
    # goes looking for the pack it was built from.
    sid = result.get("server_id")
    if sid:
        try:
            manifest = await crafty.read_studio_manifest(sid)
            manifest["roll"] = roll_info
            await crafty.write_studio_manifest(sid, manifest)
        except Exception as e:
            job.log_line(f"Could not record the roll on the instance: {e}", "warn")
    job.log_line(
        f"Roulette {roll_id}: {len(hand)} rolled, {len(full)} jars installed"
        + (", export ready to download" if roll_id else "")
    )
    return result
