"""Modpack archive analysis: manifests, loaders, and overlay selection.

Two shapes of archive show up on CurseForge:

  1. A *server pack* -- already laid out like a server directory (mods/,
     config/, a start script, often a bundled loader installer).
  2. A *client pack* -- manifest.json plus an overrides/ folder. The mods
     are not in the zip at all; the manifest lists project/file id pairs
     that have to be fetched individually. This is the fallback whenever a
     pack has no server pack, which is common.

Both end up as "a set of files to lay over a freshly created Crafty
instance", which is what this module produces.

The same two shapes arrive from `POST /api/uploads/modpack` when a user
imports a *private* export -- the zip the CurseForge app writes for a
profile they assembled themselves, which is never published and therefore
has no project id to install from. Such an export is a client pack with two
quirks worth knowing: its `version` is usually empty (nothing ever released
it), and its `overrides/mods/` may hold hand-added jars that are in no
catalogue at all. Both are handled here rather than at the call site.
"""
from __future__ import annotations

import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field

# Loader id in a CurseForge manifest -> Crafty jar-catalog key.
LOADER_TO_CRAFTY = {
    "forge": "forge-installer",
    "neoforge": "neoforge-installer",
    "fabric": "fabric",
}

# Directories from a client pack's overrides/ that are meaningless or
# harmful on a server. Everything else is copied across.
CLIENT_ONLY_DIRS = {
    "resourcepacks", "shaderpacks", "resources", "screenshots", "saves",
    "emotes", "essential", "optionsshaders", "citresewn", "iris",
    "fancymenu", "distanthorizons", "logs", "crash-reports", "downloads",
    "instance", "local", "replay_recordings", "schematics", "journeymap",
    "xaero", "bobby", "cameraoverhaul", "mcpatcher",
}

CLIENT_ONLY_FILES = {
    "options.txt", "optionsof.txt", "optionsshaders.txt", "servers.dat",
    "usercache.json", "usernamecache.json", "hotbar.nbt", "realms_persistence.json",
    "instance.cfg", "modrinth.index.json",
}

# Mods that are client-side by nature. A server pack normally omits these,
# but manifest installs pull whatever the manifest lists, and a few packs
# list client mods as required. Matched case-insensitively against the jar
# name; deliberately conservative to avoid dropping something needed.
CLIENT_ONLY_MODS = {
    "optifine", "iris", "sodium", "rubidium", "embeddium", "oculus",
    "canvas", "immediatelyfast", "entityculling", "ferritecore-client",
    "betterf3", "bettertaskbar", "dynamic-fps", "dynamicfps", "modmenu",
    "mod-menu", "reeses-sodium-options", "sodium-extra", "magnesium",
    "controlling", "toastcontrol", "toast-control", "jei-mekanism",
    "shoulder-surfing", "shouldersurfing", "replaymod", "journeymap-client",
    "xaeros-world-map", "xaerosworldmap", "xaeros-minimap", "xaerominimap",
    "readmelater", "loadmyresources", "resourcefullib-client",
    "notenoughanimations", "firstperson", "3dskinlayers", "eatinganimation",
    "visuality", "particular", "fancymenu", "drippyloadingscreen",
    "borderless-mining", "distanthorizons", "smoothboot-client",
    "screenshot-viewer", "betterthirdperson", "camera-utils", "freecam",
    "zoomify", "logical-zoom", "ok-zoomer", "okzoomer", "mousetweaks",
    "inventoryprofilesnext", "invmove", "chat-heads", "chatheads",
    "presence-footprint", "craftpresence", "simple-discord-rpc",
    "sound-physics-remastered", "soundphysics", "ambientsounds",
    "dynamic-surroundings", "physicsmod", "particle-rain", "effective",
    "falling-leaves", "fallingleaves", "make-bubbles-pop", "visual-workbench",
    "skinlayers3d", "capes", "cosmetic-armor-reworked",
}

# Files inside a *server pack* we must not copy: Crafty installs the loader
# itself, so a bundled installer or a stale start script only causes trouble.
SERVER_PACK_SKIP_PATTERNS = [
    re.compile(r"^(forge|neoforge|fabric|quilt)[-_].*installer.*\.jar$", re.I),
    re.compile(r"^(minecraft_server|server)[-_.]?\d.*\.jar$", re.I),
    re.compile(r"^serverstarter.*\.jar$", re.I),
    re.compile(r"^(start|run|launch|setup|install)[^/]*\.(sh|bat|cmd|ps1|exe)$", re.I),
    re.compile(r"^(user_jvm_args\.txt|variables\.txt)$", re.I),
    re.compile(r"^eula\.txt$", re.I),
    re.compile(r"^(unix_args|win_args)\.txt$", re.I),
    re.compile(r"^server\.properties$", re.I),
    # Generator leftovers that mean nothing to a running server.
    re.compile(r"^(manifest\.json|modrinth\.index\.json|instance\.cfg)$", re.I),
    re.compile(r"^(how-to-run|readme|license|changelog)[^/]*\.(md|txt)$", re.I),
]


@dataclass
class PackPlan:
    """Everything we learn about a pack before touching Crafty."""
    name: str = ""
    version: str = ""
    mc_version: str = ""
    loader: str = ""            # forge | neoforge | fabric | quilt
    loader_version: str = ""    # exact version the pack asks for
    crafty_loader: str = ""     # catalog key we will actually install
    source: str = ""            # "server_pack" | "manifest"
    recommended_ram: int = 0    # MB, when the pack declares it
    java_version: int = 0       # Java major the pack was authored against
    manifest_files: list = field(default_factory=list)  # [{projectID, fileID, required}]
    overlay_members: list = field(default_factory=list)  # zip members to copy
    strip_prefix: str = ""      # nested root inside the archive
    warnings: list = field(default_factory=list)


# Archive members whose path escapes the directory they are extracted into.
# CurseForge's own zips are well behaved, but an imported archive comes from
# whoever built it, and every member here is eventually handed to Crafty to
# extract inside a server directory -- so the path is checked, not trusted.
def is_safe_target(rel: str) -> bool:
    if not rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        return False
    parts = rel.replace("\\", "/").split("/")
    return not any(part in ("", ".", "..") for part in parts)


def parse_loader_id(loader_id: str) -> tuple[str, str]:
    """'neoforge-21.1.247' -> ('neoforge', '21.1.247')."""
    if not loader_id:
        return "", ""
    lid = loader_id.strip().lower()
    for family in ("neoforge", "forge", "fabric", "quilt"):
        if lid.startswith(family):
            return family, lid[len(family) :].lstrip("-_")
    if "-" in lid:
        head, _, tail = lid.partition("-")
        return head, tail
    return lid, ""


def _detect_root(names: list[str], markers: tuple[str, ...]) -> str:
    """Find the prefix inside a zip that actually holds the server files.

    Many packs wrap everything in a single top-level folder; some nest it
    two deep. We look for the shallowest directory containing a marker.
    """
    best = None
    for name in names:
        parts = name.split("/")
        for i, part in enumerate(parts[:-1]):
            if part.lower() in markers:
                prefix = "/".join(parts[:i])
                if best is None or len(prefix) < len(best):
                    best = prefix
        # A marker file sitting at some depth also identifies the root.
        base = parts[-1].lower()
        if base in ("manifest.json",):
            prefix = "/".join(parts[:-1])
            if best is None or len(prefix) < len(best):
                best = prefix
    return best or ""


def analyse_client_pack(zf: zipfile.ZipFile) -> PackPlan:
    """Read manifest.json + pick the overrides worth copying to a server."""
    names = zf.namelist()
    manifest_name = None
    for n in names:
        if posixpath.basename(n) == "manifest.json" and n.count("/") <= 1:
            manifest_name = n
            break
    if not manifest_name:
        raise ValueError("no manifest.json in this archive")

    prefix = posixpath.dirname(manifest_name)
    manifest = json.loads(zf.read(manifest_name).decode("utf-8", "replace"))
    mc = manifest.get("minecraft", {}) or {}
    loaders = mc.get("modLoaders", []) or []
    primary = next((l for l in loaders if l.get("primary")), loaders[0] if loaders else {})
    family, lver = parse_loader_id(primary.get("id", ""))

    plan = PackPlan(
        name=manifest.get("name", ""),
        version=str(manifest.get("version", "")),
        mc_version=mc.get("version", ""),
        loader=family,
        loader_version=lver,
        crafty_loader=LOADER_TO_CRAFTY.get(family, ""),
        source="manifest",
        recommended_ram=int(mc.get("recommendedRam") or 0),
        # Only exports written by newer CurseForge app builds carry this; it
        # is the Java the pack was actually tested on, which beats inferring
        # one from the Minecraft version.
        java_version=int(mc.get("javaVersion") or 0),
        manifest_files=manifest.get("files", []) or [],
    )

    overrides_dir = manifest.get("overrides", "overrides")
    over_prefix = posixpath.join(prefix, overrides_dir) if prefix else overrides_dir
    over_prefix = over_prefix.rstrip("/") + "/"

    unsafe = 0
    for n in names:
        if not n.startswith(over_prefix) or n.endswith("/"):
            continue
        rel = n[len(over_prefix) :]
        if not rel:
            continue
        if not is_safe_target(rel):
            unsafe += 1
            continue
        top = rel.split("/")[0].lower()
        if top in CLIENT_ONLY_DIRS:
            continue
        if rel.lower() in CLIENT_ONLY_FILES or posixpath.basename(rel).lower() in CLIENT_ONLY_FILES:
            continue
        plan.overlay_members.append({"member": n, "target": rel})

    plan.strip_prefix = over_prefix
    if unsafe:
        plan.warnings.append(
            f"{unsafe} file(s) in overrides/ had paths pointing outside the "
            "server directory and were dropped."
        )
    if not plan.manifest_files and not plan.overlay_members:
        plan.warnings.append(
            "This export lists no mods and carries no override files -- it "
            "may have been exported with everything deselected."
        )
    if not plan.crafty_loader:
        plan.warnings.append(
            f"Loader '{family or 'unknown'}' is not in Crafty's jar catalog; "
            "Quilt and unknown loaders must be installed manually."
        )
    return plan


def _read_meta_sources(zf: zipfile.ZipFile, names: list[str], plan: PackPlan) -> None:
    """Fill in loader/version from whichever metadata file the pack ships.

    Server packs are built by several different tools, each with its own
    idea of a manifest, so we try all of the shapes seen in the wild:

      * CurseForge client manifest  {"minecraft": {"version", "modLoaders"}}
      * ServerPackCreator manifest  {"minecraftVersion", "modloader",
                                     "modloaderVersion"}   <- very common
      * ServerPackCreator variables.txt  MINECRAFT_VERSION=... etc.
      * Modrinth index  {"dependencies": {"minecraft", "neoforge", ...}}
    """
    for n in names:
        base = posixpath.basename(n).lower()
        if base not in ("manifest.json", "modrinth.index.json", "variables.txt"):
            continue
        try:
            raw = zf.read(n).decode("utf-8", "replace")
        except Exception:
            continue

        if base == "variables.txt":
            kv = dict(
                re.findall(r"^([A-Z_]+)=\"?([^\"\n\r]*)\"?\s*$", raw, re.M)
            )
            plan.mc_version = plan.mc_version or kv.get("MINECRAFT_VERSION", "").strip()
            loader = (kv.get("MODLOADER") or "").strip().lower()
            if loader and not plan.loader:
                plan.loader = loader
            plan.loader_version = (
                plan.loader_version or kv.get("MODLOADER_VERSION", "").strip()
            )
            m = re.search(r"-Xmx(\d+)([GgMm])", kv.get("JAVA_ARGS", ""))
            if m and not plan.recommended_ram:
                plan.recommended_ram = int(m.group(1)) * (
                    1024 if m.group(2).lower() == "g" else 1
                )
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        if base == "modrinth.index.json":
            deps = data.get("dependencies", {}) or {}
            plan.name = plan.name or data.get("name", "")
            plan.version = plan.version or str(data.get("versionId", ""))
            plan.mc_version = plan.mc_version or deps.get("minecraft", "")
            for fam in ("neoforge", "forge", "fabric-loader", "quilt-loader"):
                if deps.get(fam):
                    plan.loader = plan.loader or fam.replace("-loader", "")
                    plan.loader_version = plan.loader_version or deps[fam]
                    break
            continue

        # manifest.json -- either CurseForge's or ServerPackCreator's.
        mc = data.get("minecraft")
        if isinstance(mc, dict):
            loaders = mc.get("modLoaders", []) or []
            primary = next(
                (l for l in loaders if l.get("primary")), loaders[0] if loaders else {}
            )
            fam, lver = parse_loader_id(primary.get("id", ""))
            plan.name = plan.name or data.get("name", "")
            plan.version = plan.version or str(data.get("version", ""))
            plan.mc_version = plan.mc_version or mc.get("version", "")
            plan.loader = plan.loader or fam
            plan.loader_version = plan.loader_version or lver
            plan.recommended_ram = plan.recommended_ram or int(
                mc.get("recommendedRam") or 0
            )
            plan.java_version = plan.java_version or int(mc.get("javaVersion") or 0)
        elif data.get("minecraftVersion"):
            plan.mc_version = plan.mc_version or data.get("minecraftVersion", "")
            plan.loader = plan.loader or (data.get("modloader") or "").strip().lower()
            plan.loader_version = plan.loader_version or data.get(
                "modloaderVersion", ""
            )
            plan.name = plan.name or data.get("name", "")


def analyse_server_pack(zf: zipfile.ZipFile) -> PackPlan:
    """Work out the layout of an already-built server pack."""
    names = [n for n in zf.namelist() if not n.endswith("/")]
    root = _detect_root(names, ("mods", "config", "defaultconfigs"))
    prefix = (root.rstrip("/") + "/") if root else ""

    plan = PackPlan(source="server_pack", strip_prefix=prefix)
    _read_meta_sources(zf, names, plan)
    # Loader names arrive in every casing ("NeoForge", "neoforge", "Forge").
    if plan.loader:
        plan.loader = plan.loader.strip().lower()

    # Otherwise infer the loader from the files that are present.
    for n in names:
        rel = n[len(prefix) :] if prefix and n.startswith(prefix) else n
        base = posixpath.basename(rel).lower()
        if not plan.loader:
            m = re.match(
                r"(neoforge|forge)[-_](?:installer[-_])?(\d+\.\d+(?:\.\d+)?)[-_]"
                r"(\d+\.\d+(?:\.\d+)?)",
                base,
            )
            if m:
                plan.loader = m.group(1)
                plan.mc_version = plan.mc_version or m.group(2)
                plan.loader_version = plan.loader_version or m.group(3)
            elif "fabric-server" in base or base.startswith("fabric-installer"):
                plan.loader = "fabric"
        # libraries/net/neoforged/neoforge/<ver>/ is a strong signal.
        m2 = re.search(
            r"libraries/net/(neoforged/neoforge|minecraftforge/forge)/([^/]+)/", rel
        )
        if m2:
            plan.loader = "neoforge" if "neoforged" in m2.group(1) else "forge"
            ver = m2.group(2)
            if "-" in ver:
                mcv, _, lv = ver.partition("-")
                plan.mc_version = plan.mc_version or mcv
                plan.loader_version = plan.loader_version or lv
            else:
                plan.loader_version = plan.loader_version or ver

    plan.crafty_loader = LOADER_TO_CRAFTY.get(plan.loader, "")

    unsafe = 0
    for n in names:
        rel = n[len(prefix) :] if prefix and n.startswith(prefix) else n
        if not rel or rel.startswith("."):
            continue
        if not is_safe_target(rel):
            unsafe += 1
            continue
        top = rel.split("/")[0].lower()
        if top in CLIENT_ONLY_DIRS:
            continue
        # Never overwrite the loader install Crafty just produced.
        if top in ("libraries", "versions"):
            continue
        base = posixpath.basename(rel)
        if any(p.match(base) for p in SERVER_PACK_SKIP_PATTERNS) and "/" not in rel:
            continue
        plan.overlay_members.append({"member": n, "target": rel})

    if unsafe:
        plan.warnings.append(
            f"{unsafe} file(s) had paths pointing outside the server "
            "directory and were dropped."
        )
    if not plan.overlay_members:
        plan.warnings.append("The server pack appears to be empty.")
    if not any(m["target"].startswith("mods/") for m in plan.overlay_members):
        plan.warnings.append(
            "No mods/ directory found in the server pack -- it may use a "
            "downloader script instead."
        )
    return plan


def find_manifest_member(names: list[str]) -> str:
    """The CurseForge manifest, at the archive root or one folder deep."""
    for n in names:
        if posixpath.basename(n) == "manifest.json" and n.count("/") <= 1:
            return n
    return ""


def detect_kind(zf: zipfile.ZipFile) -> str:
    """'manifest' (client export) or 'server_pack', from the archive alone.

    Order matters here. A client export whose author hand-added jars has
    `overrides/mods/*.jar`, which looks exactly like a server pack's mods
    directory to a filename scan -- so the manifest is checked first, and a
    manifest only counts when the archive really is built around it (it has
    an overrides/ folder, or a files list, or says so outright). Server packs
    that happen to bundle a stray client manifest fail all three and fall
    through correctly.
    """
    names = [n for n in zf.namelist() if not n.endswith("/")]
    member = find_manifest_member(names)
    if member:
        try:
            data = json.loads(zf.read(member).decode("utf-8", "replace"))
        except Exception:
            data = {}
        if isinstance(data, dict) and isinstance(data.get("minecraft"), dict):
            prefix = posixpath.dirname(member)
            overrides = data.get("overrides", "overrides")
            over_prefix = (
                posixpath.join(prefix, overrides) if prefix else overrides
            ).rstrip("/") + "/"
            if (
                data.get("files")
                or data.get("manifestType") == "minecraftModpack"
                or any(n.startswith(over_prefix) for n in names)
            ):
                return "manifest"
    return "server_pack"


def analyse_archive(zf: zipfile.ZipFile) -> PackPlan:
    """Analyse an archive of unknown shape. Used for every imported zip."""
    if detect_kind(zf) == "manifest":
        return analyse_client_pack(zf)
    return analyse_server_pack(zf)


def overlay_jars(plan: PackPlan) -> list[dict]:
    """Overlay members that are mod jars -- hand-added mods, in an export."""
    return [
        m for m in plan.overlay_members
        if m["target"].startswith("mods/") and m["target"].lower().endswith(".jar")
    ]


def is_client_only_jar(filename: str) -> bool:
    base = re.sub(r"\.jar(\.disabled)?$", "", posixpath.basename(filename), flags=re.I)
    norm = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    for token in CLIENT_ONLY_MODS:
        if norm == token or norm.startswith(token + "-"):
            return True
    return False


def mem_from_ram_hint(recommended_mb: int, default_min: int, default_max: int
                      ) -> tuple[int, int]:
    """Turn a pack's recommendedRam (MB) into Crafty's min/max GB values."""
    if not recommended_mb:
        return default_min, default_max
    gb = max(2, round(recommended_mb / 1024))
    return min(default_min, gb), max(gb, default_min)
