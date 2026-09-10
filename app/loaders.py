"""Where a loader jar comes from when Crafty cannot fetch it.

Crafty downloads loader jars from one mirror (`jars.arcadiatech.org`) on a
daemon thread. When that mirror is down -- and it goes down -- Crafty logs a
single line, gives up, and still answers the create call with 201. The
instance then exists with no launcher and nothing in the API ever says so.

The installer already retries through Crafty's own jar index. This module is
the layer below that: the *upstream* projects, which are independent of both
Crafty and its mirror, so a mirror outage stops being fatal.

  vanilla    Mojang's version manifest
  fabric     meta.fabricmc.net -- serves a ready-made server launcher jar
  neoforge   maven.neoforged.net -- installer jar
  forge      maven.minecraftforge.net -- installer jar
  paper      fill.papermc.io (v3) -- also serves Folia and Velocity
  purpur     api.purpurmc.org

Every function here returns `{}` rather than raising when a source has
nothing: "we could not find a build" and "the network is broken" are
different answers and the caller distinguishes them.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

UA = "BlessForge/2.1 (+https://github.com/ChargeyXD/BlessForge)"

MOJANG_MANIFEST = "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META = "https://meta.fabricmc.net/v2"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
FORGE_META = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
# PaperMC sunset its v2 API: every path under api.papermc.io/v2 now answers
# 410 with {"error":"sunset"}. v3 lives on a different host, and its shapes
# differ enough that it is not a base-URL swap -- versions arrive grouped by
# family, and a build's download is keyed "server:default" rather than
# "application".
PAPER_API = "https://fill.papermc.io/v3/projects"
PURPUR_API = "https://api.purpurmc.org/v2/purpur"

# Which family each Crafty jar-catalogue key belongs to. Crafty's keys are
# what the rest of the app speaks, so the translation lives here rather than
# leaking into every caller.
CRAFTY_KEY_TO_FAMILY = {
    "vanilla": "vanilla",
    "fabric": "fabric",
    "forge-installer": "forge",
    "neoforge-installer": "neoforge",
    "paper": "paper",
    "purpur": "purpur",
    "folia": "folia",
}
FAMILY_TO_CRAFTY_KEY = {v: k for k, v in CRAFTY_KEY_TO_FAMILY.items()}

# Families whose jar is an *installer* that has to be run once before the
# server exists. Everything else is the server itself.
INSTALLER_FAMILIES = {"forge", "neoforge"}

# Families that load Bukkit-style plugins rather than mods.
PLUGIN_FAMILIES = {"paper", "purpur", "folia", "spigot", "bukkit"}
MOD_FAMILIES = {"forge", "neoforge", "fabric", "quilt"}

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 60 * 30


def _client(timeout: float = 45.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout, follow_redirects=True,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )


async def _json(url: str, timeout: float = 30.0) -> Any:
    now = asyncio.get_event_loop().time()
    hit = _cache.get(url)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    async with _client(timeout) as c:
        r = await c.get(url)
        if r.status_code >= 400:
            raise RuntimeError(f"{url} -> {r.status_code}")
        data = r.json()
    _cache[url] = (now, data)
    return data


def family_of(loader: str) -> str:
    """Normalise anything the app might call a loader into a family name."""
    key = (loader or "").strip().lower()
    if key in CRAFTY_KEY_TO_FAMILY:
        return CRAFTY_KEY_TO_FAMILY[key]
    key = key.replace("-installer", "").replace("_", "")
    return {"neoforged": "neoforge", "papermc": "paper",
            "spigot": "paper", "bukkit": "paper"}.get(key, key)


def crafty_key(loader: str) -> str:
    return FAMILY_TO_CRAFTY_KEY.get(family_of(loader), family_of(loader))


def mod_directory(loader: str) -> str:
    """`mods` or `plugins` -- which folder this loader actually reads.

    The single most consequential difference between a Paper instance and a
    Fabric one, and the reason a plugin dropped into `mods/` does nothing at
    all while reporting no error anywhere.
    """
    return "plugins" if family_of(loader) in PLUGIN_FAMILIES else "mods"


def takes_plugins(loader: str) -> bool:
    return family_of(loader) in PLUGIN_FAMILIES


# --- version listing ---------------------------------------------------


async def minecraft_versions(*, releases_only: bool = True) -> list[str]:
    data = await _json(MOJANG_MANIFEST)
    out = []
    for v in data.get("versions", []):
        if releases_only and v.get("type") != "release":
            continue
        out.append(v["id"])
    return out


async def versions_for(family: str) -> list[str]:
    """Minecraft versions this loader has a build for, newest first."""
    family = family_of(family)
    try:
        if family == "vanilla":
            return await minecraft_versions()
        if family == "fabric":
            games = await _json(f"{FABRIC_META}/versions/game")
            return [g["version"] for g in games if g.get("stable")]
        if family == "neoforge":
            builds = await _neoforge_versions()
            # Reconciled against Mojang's own release list: NeoForge's version
            # scheme has changed once already, and a list of Minecraft
            # versions that do not exist is worse than a short one.
            try:
                real = set(await minecraft_versions())
            except Exception:
                real = set()
            seen, out = set(), []
            for b in sorted(builds, key=_version_key, reverse=True):
                for mc in _neoforge_candidates(b):
                    if mc in seen:
                        continue
                    if real and mc not in real:
                        continue
                    seen.add(mc)
                    out.append(mc)
            return out
        if family == "forge":
            promos = await _json(FORGE_META)
            seen, out = set(), []
            for key in (promos.get("promos") or {}):
                mc = key.rsplit("-", 1)[0]
                if mc not in seen:
                    seen.add(mc)
                    out.append(mc)
            out.sort(key=_version_key, reverse=True)
            return out
        if family in ("paper", "folia", "velocity"):
            data = await _json(f"{PAPER_API}/{family}")
            # v3 returns {"versions": {"<family>": ["26.2", "26.2-rc-2", ...]}}
            # -- grouped, newest family first, and carrying release candidates
            # nobody should be offered as a default.
            grouped = data.get("versions") or {}
            out: list[str] = []
            if isinstance(grouped, dict):
                for versions in grouped.values():
                    out.extend(v for v in versions if _is_stable(v))
            else:
                out.extend(v for v in grouped if _is_stable(v))
            return out
        if family == "purpur":
            data = await _json(PURPUR_API)
            return list(reversed(data.get("versions") or []))
    except Exception:
        return []
    return []


def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0
                 for p in re.split(r"[.\-]", v)[:3])


_NEOFORGE_CACHE: dict[str, tuple[float, list[str]]] = {}


async def _neoforge_versions() -> list[str]:
    """NeoForge publishes a maven-metadata.xml, not JSON.

    Cached separately from `_json` because it is the one source here that is
    not JSON, and re-fetching a 300 KB XML index for every loader row on the
    create screen is a waste of somebody's link.
    """
    now = asyncio.get_event_loop().time()
    hit = _NEOFORGE_CACHE.get("v")
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    async with _client() as c:
        r = await c.get(f"{NEOFORGE_MAVEN}/maven-metadata.xml")
        if r.status_code >= 400:
            return []
        versions = re.findall(r"<version>([^<]+)</version>", r.text)
    _NEOFORGE_CACHE["v"] = (now, versions)
    return versions


def _neoforge_candidates(loader_version: str) -> list[str]:
    """Every Minecraft version a NeoForge build could plausibly be for.

    NeoForge encodes the game version in its own, and there are now two
    schemes in the index at once:

        21.1.250     three parts  -> Minecraft 1.21.1  (the 1.x era)
        26.2.0.84    four parts   -> Minecraft 26.2    (the year-based era)
        26.1.2.108   four parts   -> Minecraft 26.1.2

    Rather than guess which era a version belongs to, both readings are
    returned and the caller keeps whichever one Mojang actually publishes.
    That way the next scheme change costs a list entry, not a bug.
    """
    parts = (loader_version or "").strip().split(".")
    if len(parts) < 3 or not all(p.isdigit() for p in parts[:3]):
        return []
    a, b, c = parts[0], parts[1], parts[2]
    if len(parts) >= 4:
        return [f"{a}.{b}" if c == "0" else f"{a}.{b}.{c}"]
    return [f"1.{a}" if b == "0" else f"1.{a}.{b}"]


def _neoforge_mc(loader_version: str) -> str:
    """One best answer, for callers that cannot check against Mojang."""
    candidates = _neoforge_candidates(loader_version)
    return candidates[0] if candidates else ""


# --- resolving one downloadable jar ------------------------------------


async def resolve(family: str, mc_version: str) -> dict:
    """`{url, filename, kind, loader_version}` for this loader + version.

    `kind` is "server" (run it directly) or "installer" (run once, then the
    real launcher exists). `{}` means no upstream build was found, which is
    a real answer for e.g. Paper on a snapshot.
    """
    family = family_of(family)
    try:
        if family == "vanilla":
            return await _vanilla(mc_version)
        if family == "fabric":
            return await _fabric(mc_version)
        if family == "neoforge":
            return await _neoforge(mc_version)
        if family == "forge":
            return await _forge(mc_version)
        if family in ("paper", "folia", "velocity"):
            return await _paper_family(family, mc_version)
        if family == "purpur":
            return await _purpur(mc_version)
    except Exception:
        return {}
    return {}


async def _vanilla(mc: str) -> dict:
    data = await _json(MOJANG_MANIFEST)
    entry = next((v for v in data.get("versions", []) if v["id"] == mc), None)
    if not entry:
        return {}
    detail = await _json(entry["url"])
    server = ((detail.get("downloads") or {}).get("server") or {})
    if not server.get("url"):
        return {}
    return {
        "url": server["url"], "filename": f"server-{mc}.jar", "kind": "server",
        "loader_version": mc, "sha1": server.get("sha1"),
        "source": "Mojang",
    }


async def _fabric(mc: str) -> dict:
    loaders = await _json(f"{FABRIC_META}/versions/loader/{mc}")
    if not loaders:
        return {}
    loader_v = loaders[0]["loader"]["version"]
    installers = await _json(f"{FABRIC_META}/versions/installer")
    stable = next((i for i in installers if i.get("stable")), None)
    installer_v = (stable or installers[0])["version"]
    return {
        "url": f"{FABRIC_META}/versions/loader/{mc}/{loader_v}/{installer_v}"
               "/server/jar",
        "filename": f"fabric-server-mc.{mc}-loader.{loader_v}"
                    f"-launcher.{installer_v}.jar",
        "kind": "server", "loader_version": loader_v, "source": "FabricMC",
    }


async def _neoforge(mc: str) -> dict:
    versions = await _neoforge_versions()
    matching = [v for v in versions
                if mc in _neoforge_candidates(v) and "beta" not in v.lower()]
    if not matching:
        matching = [v for v in versions if mc in _neoforge_candidates(v)]
    if not matching:
        return {}
    best = sorted(matching, key=_version_key)[-1]
    return {
        "url": f"{NEOFORGE_MAVEN}/{best}/neoforge-{best}-installer.jar",
        "filename": f"neoforge-{best}-installer.jar",
        "kind": "installer", "loader_version": best, "source": "NeoForged",
    }


async def _forge(mc: str) -> dict:
    promos = (await _json(FORGE_META)).get("promos") or {}
    build = promos.get(f"{mc}-recommended") or promos.get(f"{mc}-latest")
    if not build:
        return {}
    full = f"{mc}-{build}"
    return {
        "url": f"{FORGE_MAVEN}/{full}/forge-{full}-installer.jar",
        "filename": f"forge-{full}-installer.jar",
        "kind": "installer", "loader_version": build, "source": "MinecraftForge",
    }


def _is_stable(version: str) -> bool:
    """Filter out the pre-releases and candidates Paper lists alongside."""
    v = (version or "").lower()
    return not any(t in v for t in ("-rc", "-pre", "snapshot", "-exp"))


async def _paper_family(project: str, mc: str) -> dict:
    """v3 hands back the finished download URL, checksum included."""
    detail = await _json(f"{PAPER_API}/{project}/versions/{mc}/builds/latest")
    downloads = detail.get("downloads") or {}
    entry = downloads.get("server:default") or next(iter(downloads.values()), None)
    if not entry or not entry.get("url"):
        return {}
    build = detail.get("id")
    return {
        "url": entry["url"],
        "filename": entry.get("name") or f"{project}-{mc}-{build}.jar",
        "kind": "server",
        "loader_version": str(build),
        "sha256": (entry.get("checksums") or {}).get("sha256"),
        "source": "PaperMC",
    }


async def _purpur(mc: str) -> dict:
    data = await _json(f"{PURPUR_API}/{mc}")
    builds = ((data.get("builds") or {}).get("all")) or []
    latest = (data.get("builds") or {}).get("latest") or (
        builds[-1] if builds else None)
    if not latest:
        return {}
    return {
        "url": f"{PURPUR_API}/{mc}/{latest}/download",
        "filename": f"purpur-{mc}-{latest}.jar", "kind": "server",
        "loader_version": str(latest), "source": "PurpurMC",
    }


async def fetch(url: str, attempts: int = 4) -> bytes:
    """Download with backoff. Loader jars are the one file worth retrying."""
    last: Exception | None = None
    async with _client(timeout=600) as c:
        for i in range(attempts):
            try:
                r = await c.get(url)
                r.raise_for_status()
                return r.content
            except Exception as e:      # noqa: BLE001 -- retried then re-raised
                last = e
                await asyncio.sleep(2 ** i)
    raise RuntimeError(f"could not download {url}: {last}")


# --- what the UI offers ------------------------------------------------

CATALOGUE = [
    {"key": "fabric", "title": "Fabric", "kind": "mods",
     "logo": "/assets/loader-fabric.png",
     "blurb": "Light, fast, and the quickest to update to a new Minecraft "
              "release. Takes Fabric mods."},
    {"key": "forge", "title": "Forge", "kind": "mods",
     "logo": "/assets/loader-forge.jpg",
     "blurb": "The oldest and largest mod ecosystem. Almost every big "
              "modpack is Forge or NeoForge."},
    {"key": "neoforge", "title": "NeoForge", "kind": "mods",
     "logo": "/assets/loader-neoforge.png",
     "blurb": "The Forge fork most 1.20.2+ packs moved to. Takes NeoForge "
              "mods; 1.20.1 also reads Forge ones."},
    {"key": "paper", "title": "Paper", "kind": "plugins",
     "logo": None,
     "blurb": "No mods — Bukkit/Spigot plugins, and no client install for "
              "your players. The usual choice for a survival server."},
    {"key": "purpur", "title": "Purpur", "kind": "plugins",
     "logo": None,
     "blurb": "Paper with several hundred extra gameplay toggles. Runs "
              "every Paper plugin."},
    {"key": "folia", "title": "Folia", "kind": "plugins",
     "logo": None,
     "blurb": "Paper's regionised-threading fork, for very large player "
              "counts. Not every plugin supports it."},
    {"key": "vanilla", "title": "Vanilla", "kind": "none",
     "logo": "/assets/loader-vanilla.svg",
     "blurb": "Mojang's server, unmodified. Datapacks only."},
]


async def catalogue(crafty_catalog: dict | None = None) -> list[dict]:
    """The loader list for the create screen, with real version lists.

    Crafty's own index is asked first, because a version Crafty knows about
    is one it can install without any of the fallbacks below being needed.
    Upstream fills in whatever Crafty's index is missing.
    """
    out: list[dict] = []
    crafty_types = ((crafty_catalog or {}).get("mc_java_servers") or {}
                    ).get("types") or {}

    async def one(entry: dict) -> dict:
        key = entry["key"]
        ckey = crafty_key(key)
        from_crafty = sorted(
            ((crafty_types.get(ckey) or {}).get("versions") or {}).keys(),
            key=_version_key, reverse=True,
        )
        upstream = await versions_for(key)
        merged = list(dict.fromkeys(from_crafty + upstream))
        merged.sort(key=_version_key, reverse=True)
        return {
            **entry,
            "crafty_key": ckey,
            "versions": merged[:60],
            "crafty_versions": from_crafty[:60],
            "in_crafty": bool(from_crafty),
            "mod_directory": mod_directory(key),
        }

    out = list(await asyncio.gather(*(one(e) for e in CATALOGUE)))
    return [o for o in out if o["versions"]]
