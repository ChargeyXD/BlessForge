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


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
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

# How fast the console follows a server.
#
# Crafty exposes no push channel, so the console is a poll-and-diff. The
# interval used to be a flat 1.5s, which is why output arrived in visible
# clumps rather than line by line -- a booting server writes hundreds of
# lines into a window where nothing is sent at all.
#
# It is now adaptive: fast while output is flowing, backing off to the idle
# interval when it stops, and snapping back to fast the moment a line
# appears. A poll costs about 19ms and under a kilobyte against a Crafty on
# the same machine, which is the normal deployment, so the fast interval is
# affordable exactly when it matters and is not paid for when it does not.
CONSOLE_POLL_FAST = _float("CONSOLE_POLL_FAST", 0.2)
# 0.6s, not 1.5s. The backoff is there to be polite to a Crafty across a
# network; the normal deployment has it on the same machine, where a
# poll measured 19ms and under a kilobyte. At 0.6s that is under 2
# requests a second and about 1 KB/s for an idle console, and it halves
# the worst case for the FIRST line of a burst -- which is the one that
# decides whether the console feels live. Raise it if Crafty is remote.
CONSOLE_POLL_IDLE = _float("CONSOLE_POLL_IDLE", 0.6)
# Whether the server is up changes on the scale of minutes, so it is asked
# for on a clock rather than every Nth poll -- at the fast interval, every
# fifth pass would be once a second.
CONSOLE_STATS_EVERY = _float("CONSOLE_STATS_EVERY", 5.0)
# Default RAM for new instances (GB). Modpacks override via manifest hints.
DEFAULT_MEM_MIN = _int("DEFAULT_MEM_MIN", 2)
DEFAULT_MEM_MAX = _int("DEFAULT_MEM_MAX", 6)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", str(DATA_DIR / "cache")))

# Small persistent state -- the client-only decision list, the chosen AI
# endpoint, the update-sweep record.
#
# It lives in a SUBDIRECTORY, and that is the whole point. CasaOS creates the
# bind mount as root and the app runs as uid 1000, so a file written straight
# into DATA_DIR cannot be created at all -- and every one of these writes is
# wrapped in a try/except, so the failure was silent. The entrypoint chowns
# the subdirectories this app owns and deliberately never chowns DATA_DIR
# itself (pointing the mount at Crafty's folder and then taking ownership of
# it would turn a misconfiguration into someone else's outage), so anything
# that must survive a restart belongs under here.
STATE_DIR = Path(os.environ.get("STATE_DIR", str(DATA_DIR / "state")))


def state_path(name: str) -> Path:
    """Where a small state file lives, migrating one from the old location.

    Versions before 2.1 wrote these into DATA_DIR directly. On a host where
    that worked, the file is still there and still the user's data, so it is
    moved rather than abandoned.
    """
    target = STATE_DIR / name
    if target.exists():
        return target
    legacy = DATA_DIR / name
    if legacy.exists():
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            legacy.replace(target)
            return target
        except OSError:
            # Could not move it, but it is readable where it is.
            return legacy
    return target
# Keep downloaded modpack archives after install (useful for re-installs).
KEEP_CACHE = _bool("KEEP_CACHE", True)

# --- Imported modpack archives ----------------------------------------
# Where "Import a CurseForge export" parks uploaded .zip files. Kept apart
# from CACHE_DIR because these are the only files here the user cannot get
# back by re-downloading -- a private export exists nowhere else.
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(DATA_DIR / "uploads")))
# Ceiling for a single import. Exports carrying a world folder get big; the
# limit exists so a mis-drop cannot fill the volume, not to be restrictive.
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 4096)
# How many imports to keep on disk. The oldest are pruned past this.
MAX_UPLOADS = _int("MAX_UPLOADS", 12)

# Ceiling for the download cache, in GB. Everything under CACHE_DIR is a copy
# of something CurseForge or Modrinth will hand out again, so it is safe to
# delete -- but nothing ever did, and a few months of installs is several GB
# of jars for packs that are long gone, on the same disk as the worlds. Past
# this the least recently used files go first. Set 0 to never prune.
MAX_CACHE_GB = _int("MAX_CACHE_GB", 8)

# --- Undo --------------------------------------------------------------
# Snapshots kept per server before a destructive change. These hold mod
# names and enabled/disabled state, not the jars, so they cost kilobytes --
# the limit is about keeping the list readable, not about disk. 0 disables
# snapshots entirely.
MAX_BACKUPS = _int("MAX_BACKUPS", 20)

# --- Scheduled update checks -------------------------------------------
# How often to look for newer builds of installed mods, in hours. One pass
# covers every managed server and is a handful of API calls each, so this is
# cheap; it exists so "3 mods have updates" is waiting for you rather than
# something you have to go and ask for. 0 turns the schedule off.
UPDATE_CHECK_HOURS = _int("UPDATE_CHECK_HOURS", 12)

# --- Mod Roulette ------------------------------------------------------
# Catalogue pages (50 mods each) fetched per category when building a pool.
# Four gives roughly 3,000 candidates on a well-served version, which is
# ample for a 300-mod hand; raising it makes the first roll on a fresh
# version slower and the pool broader.
ROULETTE_POOL_PAGES = _int("ROULETTE_POOL_PAGES", 4)

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
    # The check above only fires when SOME of the bcrypt prefix survived. A
    # key eaten completely -- `$2a$10$D3Bo...` sourced by bash, where every
    # `$...` is a positional parameter -- comes out with no `$` at all and
    # still passes a length test. That mangled key then fails every request
    # with a 403 that reads like a revoked key rather than a quoting bug,
    # which is exactly what happened on 2026-09-11.
    if not key.startswith("$2"):
        return (
            "CURSEFORGE_API_KEY does not start with '$2'. Every CurseForge "
            "key is bcrypt-shaped, so this one has had its $-segments eaten "
            f"by shell expansion. {fix}"
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


def write_state(path: Path, text: str) -> bool:
    """Write a small state file so a power cut cannot destroy it.

    Every state writer in this app used to be one `write_text` straight
    over the live file. That has two failure modes, and a user hit the
    second one on 2026-09-11:

      1. **Not atomic.** A crash part-way through leaves a truncated file.
         `json.loads` then rejects it and the data is silently gone --
         someone's client-only decisions, or their rack layout.
      2. **Not durable.** Writing to a temp file and renaming fixes (1),
         but the rename can still reach the disk BEFORE the bytes do.
         Pull the power and the directory entry points at a correct-length
         file full of zeros. That is exactly what came back after a hard
         restart: 559 bytes of NUL where the fleet state had been.

    So: write the temp file, force it to the platform, then rename. The
    rename itself is atomic on both POSIX and Windows (`Path.replace`).
    The directory fsync afterwards is what makes the RENAME durable too;
    it is POSIX-only and simply unavailable on Windows, where the rename
    is already ordered, hence the guard.

    Returns whether it reached disk. Callers decide whether to tell the
    user -- most of them can carry on in memory, and refusing to let
    someone make a rack because /data is read-only would be worse than
    losing the rack on restart.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        if hasattr(os, "O_DIRECTORY"):          # POSIX only
            fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def read_state(path: Path) -> str | None:
    """Read a state file, treating a corrupt one as absent.

    A file of NUL bytes is what a torn write leaves behind (see
    `write_state`), and it is not the same as a missing file: it will not
    parse, and the caller has to decide that "cannot read" means "start
    empty" rather than crashing the route that touched it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    # chr(0) rather than an escape: a literal NUL in this source is
    # exactly the corruption being guarded against.
    if not text.strip() or not text.strip(chr(0)).strip():
        return None
    return text
