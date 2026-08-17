"""Runtime configuration, sourced entirely from environment variables.

Every value has a sane default so the container starts even when half
configured -- the UI then shows a setup banner instead of crashing.
"""
import base64
import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# --- Crafty Controller -------------------------------------------------
# CRAFTY_URL must include scheme + port, e.g. https://192.168.1.10:8443
CRAFTY_URL = (os.environ.get("CRAFTY_URL", "") or "").rstrip("/")
CRAFTY_TOKEN = os.environ.get("CRAFTY_TOKEN", "") or ""
# Crafty ships a self-signed cert by default, so verification is OFF unless
# the operator explicitly turns it on.
CRAFTY_VERIFY_SSL = _bool("CRAFTY_VERIFY_SSL", False)

# --- CurseForge --------------------------------------------------------
CURSEFORGE_API_KEY = (os.environ.get("CURSEFORGE_API_KEY", "") or "").strip()
if len(CURSEFORGE_API_KEY) >= 2 and (
    (CURSEFORGE_API_KEY.startswith("'") and CURSEFORGE_API_KEY.endswith("'"))
    or (CURSEFORGE_API_KEY.startswith('"') and CURSEFORGE_API_KEY.endswith('"'))
):
    CURSEFORGE_API_KEY = CURSEFORGE_API_KEY[1:-1]
# Replace escaped double-dollar if user escaped for compose
if "$$" in CURSEFORGE_API_KEY and not CURSEFORGE_API_KEY.startswith("$2a$"):
    CURSEFORGE_API_KEY = CURSEFORGE_API_KEY.replace("$$", "$")


def _looks_truncated(key: str) -> bool:
    """A bcrypt-shaped key always has three '$'. Fewer means it was eaten."""
    return key.startswith("$2") and key.count("$") < 3


# Escape-proof alternative. CurseForge keys contain '$', and every layer this
# app passes through wants to interpolate it: Compose expands '$name' in
# `environment:`, and CasaOS un-doubles '$$' when it stores an imported
# compose and then hands the file back to Compose, which expands it AGAIN --
# so no amount of doubling survives both. Base64 has no '$' at all, so it
# survives every layer untouched. Used when the plain key is missing or
# arrives damaged.
_KEY_B64 = (os.environ.get("CURSEFORGE_API_KEY_B64", "") or "").strip().strip("'\"")
if _KEY_B64 and (not CURSEFORGE_API_KEY or _looks_truncated(CURSEFORGE_API_KEY)):
    try:
        _decoded = base64.b64decode(_KEY_B64, validate=True).decode().strip()
        if _decoded:
            CURSEFORGE_API_KEY = _decoded
    except (ValueError, UnicodeDecodeError):
        pass

CURSEFORGE_API_BASE = os.environ.get(
    "CURSEFORGE_API_BASE", "https://api.curseforge.com"
).rstrip("/")

# --- Modrinth ----------------------------------------------------------
MODRINTH_API_BASE = os.environ.get(
    "MODRINTH_API_BASE", "https://api.modrinth.com/v2"
).rstrip("/")
MODRINTH_ENABLED = _bool("MODRINTH_ENABLED", True)

# --- Behaviour ---------------------------------------------------------
# Parallel mod downloads during a manifest install. CurseForge tolerates
# this comfortably; lower it on slow links.
DOWNLOAD_CONCURRENCY = _int("DOWNLOAD_CONCURRENCY", 8)
# Chunk size for uploads pushed into Crafty (bytes).
UPLOAD_CHUNK_SIZE = _int("UPLOAD_CHUNK_SIZE", 8 * 1024 * 1024)
# Seconds to wait for Crafty to finish its loader install before overlaying.
SERVER_READY_TIMEOUT = _int("SERVER_READY_TIMEOUT", 900)
# Default RAM for new instances (GB). Modpacks override via manifest hints.
DEFAULT_MEM_MIN = _int("DEFAULT_MEM_MIN", 2)
DEFAULT_MEM_MAX = _int("DEFAULT_MEM_MAX", 6)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", str(DATA_DIR / "cache")))
# Keep downloaded modpack archives after install (useful for re-installs).
KEEP_CACHE = _bool("KEEP_CACHE", True)

PORT = _int("PORT", 8710)

GAME_ID_MINECRAFT = 432
CLASS_ID_MODPACKS = 4471
CLASS_ID_MODS = 6

# Marker file written into every instance we touch, recording exactly which
# project/file each jar came from. This is what makes version switching and
# update checks possible later on.
STUDIO_MANIFEST = ".blessforge.json"
# Read-only fallback for instances created before the rename, so an existing
# server keeps its mod history instead of silently looking unmanaged.
LEGACY_MANIFESTS = (".modpack-studio.json",)


def curseforge_key_warning() -> str | None:
    """Detect a CurseForge key mangled by shell/compose variable expansion.

    Keys are bcrypt-style ("$2a$10$<22 chars><31 chars>"), so they always
    contain '$'. Docker Compose expands '$NAME' inside a substituted value,
    which quietly eats part of the key and leaves a 403 with no clue as to
    why. Catching the shape here turns a mystery into a one-line fix.
    """
    key = CURSEFORGE_API_KEY
    if not key:
        return None
    fix = (
        "Set CURSEFORGE_API_KEY_B64 instead -- base64 contains no '$', so it "
        "survives Compose and CasaOS untouched. Generate it with: "
        "echo -n '<your key>' | base64 -w0"
    )
    if _looks_truncated(key):
        return (
            "CURSEFORGE_API_KEY was truncated by variable expansion -- it "
            f"starts like a bcrypt key but is missing '$' segments. {fix}"
        )
    if len(key) < 40:
        return (
            "CURSEFORGE_API_KEY looks too short; part of it was probably eaten "
            f"by variable expansion. {fix}"
        )
    return None


def configured() -> dict:
    """Report which integrations are usable, for the UI setup banner."""
    return {
        "crafty": bool(CRAFTY_URL and CRAFTY_TOKEN),
        "curseforge": bool(CURSEFORGE_API_KEY),
        "modrinth": MODRINTH_ENABLED,
        "crafty_url": CRAFTY_URL,
        "verify_ssl": CRAFTY_VERIFY_SSL,
        "curseforge_key_warning": curseforge_key_warning(),
    }
