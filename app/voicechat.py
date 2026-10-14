"""Simple Voice Chat: found, configured, and on a UDP port of its own.

Simple Voice Chat (henkelmax/simple-voice-chat, the `voicechat` mod and
plugin) talks to its players over UDP on a port of its own -- 24454 unless its
config says otherwise. Two things go wrong on a box with several servers:

  * Every server with it installed wants 24454. The first to start gets it;
    the next one's voice port is already taken, so the node starts it without
    publishing voice (agent/containers.py says so in the console) and nobody
    on it can talk.
  * Its config file does not exist until it has run once, so a fresh install
    cannot be pointed anywhere before its first start.

So the panel gives every server with Voice Chat a UDP port of its own and
writes the config file itself, before the first start if need be (the mod
reads a file that is already there and adds whatever keys are missing). The
node then publishes that port next to the game port (agent/addons.py), on the
same number, because it reads the same file.

Ports come from VOICE_FIRST upward, skipping every game port and every other
server's voice port on the same node, and anything the node reports as held.
The port is kept in the server's record (`limits.voice.port`) so the next
allocation knows about it without opening every other server's files, and
it is checked again before every start -- a jar dropped in by hand, or a
config edited to 24454 on two servers, is sorted out then.

`port = -1` (share the game port) is never written: the voice chat wiki warns
that UDP on the game port is the query port's, and that sharing it can crash
the server.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from app import db, engine
from app.db.models import Server

VOICE_FIRST = 24454
VOICE_LAST = 24999
MOD_CONFIG = "config/voicechat/voicechat-server.properties"
PLUGIN_CONFIG = "plugins/voicechat/voicechat-server.properties"
CODECS = ("VOIP", "AUDIO", "RESTRICTED_LOWDELAY")

# The settings the panel offers, with the mod's own defaults (the wiki's
# server_config page). Everything else in the file is kept as it is.
FIELDS: dict[str, dict[str, Any]] = {
    "max_voice_distance": {"default": 48.0, "kind": "float", "min": 1, "max": 1000,
                           "label": "How far a voice carries (blocks)"},
    "whisper_distance": {"default": 24.0, "kind": "float", "min": 1, "max": 1000,
                         "label": "How far a whisper carries (blocks)"},
    "codec": {"default": "VOIP", "kind": "choice", "choices": CODECS,
              "label": "Opus codec"},
    "enable_groups": {"default": True, "kind": "bool", "label": "Group chats"},
    "allow_recording": {"default": True, "kind": "bool", "label": "Players may record"},
    "spectator_interaction": {"default": False, "kind": "bool",
                              "label": "Spectators can talk to players"},
    "spectator_player_possession": {"default": False, "kind": "bool",
                                    "label": "Spectators talk to whoever they spectate"},
    "force_voice_chat": {"default": False, "kind": "bool",
                         "label": "Kick players without the mod"},
    "login_timeout": {"default": 10000, "kind": "int", "min": 1000, "max": 120000,
                      "label": "How long to wait for a player's mod (ms)"},
    "broadcast_range": {"default": -1.0, "kind": "float", "min": -1, "max": 1000,
                        "label": "Broadcast range (-1 = the voice distance)"},
    "allow_pings": {"default": True, "kind": "bool", "label": "Answer external pings"},
    "voice_host": {"default": "", "kind": "text", "max_len": 255,
                   "label": "Address players connect voice to (blank = the game's)"},
}
HEADER = ("# Simple Voice Chat server config\n"
          "# Written by BlessForge: the port is the one BlessForge gave this server,\n"
          "# so no two servers on this machine fight over a UDP port.\n")


class VoiceChatError(ValueError):
    """Something the user is told (a 400 or 409, with the reason)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- finding it -------------------------------------------------------------------

def _is_voicechat_jar(name: str) -> bool:
    n = name.lower()
    # voicechat-fabric-1.21.1-2.5.26.jar, voicechat-neoforge-…, voicechat-bukkit-…
    return n.endswith(".jar") and (n.startswith("voicechat-") or n.startswith("voicechat_"))


async def detect(server_id: str) -> dict[str, Any]:
    """Whether Voice Chat is installed here, as a mod or a plugin, and where
    its config lives. A disabled jar (`.jar.disabled`) is not installed."""
    for folder, kind, cfg in (("mods", "mod", MOD_CONFIG), ("plugins", "plugin", PLUGIN_CONFIG)):
        try:
            entries = await engine.list_dir(server_id, folder)
        except Exception:       # noqa: BLE001 -- no such folder: not this kind
            continue
        for name, entry in (entries or {}).items():
            if isinstance(entry, dict) and entry.get("dir"):
                continue
            if _is_voicechat_jar(name):
                m = re.search(r"-(\d+\.\d+\.\d+)(?:\.jar)$", name)
                return {"present": True, "kind": kind, "file": f"{folder}/{name}",
                        "version": m.group(1) if m else None, "config_path": cfg}
    return {"present": False}


# --- the properties file ------------------------------------------------------------

_LINE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*[=:]\s*(.*?)\s*$")


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if line.lstrip().startswith(("#", "!")):
            continue
        m = _LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def render(text: str, values: dict[str, str]) -> str:
    """`text` with `values` written in: a key already there is changed on its
    own line (comments and every other key kept), a new one is appended."""
    lines = (text or "").splitlines()
    done: set[str] = set()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("#", "!")):
            continue
        m = _LINE.match(line)
        if m and m.group(1) in values:
            lines[i] = f"{m.group(1)}={values[m.group(1)]}"
            done.add(m.group(1))
    if not lines:
        lines = HEADER.rstrip("\n").splitlines()
    for key, value in values.items():
        if key not in done:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def _typed(key: str, raw: str | None) -> Any:
    spec = FIELDS[key]
    if raw is None or raw == "":
        return spec["default"]
    try:
        if spec["kind"] == "bool":
            return raw.strip().lower() == "true"
        if spec["kind"] == "int":
            return int(float(raw))
        if spec["kind"] == "float":
            return float(raw)
    except ValueError:
        return spec["default"]
    return raw


def _clean(key: str, value: Any) -> str:
    """A submitted value as the file wants it, or VoiceChatError."""
    spec = FIELDS.get(key)
    if spec is None:
        raise VoiceChatError(f"{key} is not a setting BlessForge changes.")
    kind = spec["kind"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise VoiceChatError(f"{key} is true or false.")
        return "true" if value else "false"
    if kind == "choice":
        if value not in spec["choices"]:
            raise VoiceChatError(f"{key} is one of {', '.join(spec['choices'])}.")
        return str(value)
    if kind == "text":
        text = str(value or "").strip()
        if len(text) > spec["max_len"] or any(c in text for c in "\r\n= "):
            raise VoiceChatError(f"{key} is a host name, optionally with :port.")
        return text
    try:
        number = float(value) if kind == "float" else int(value)
    except (TypeError, ValueError):
        raise VoiceChatError(f"{key} is a number.")
    if not spec["min"] <= number <= spec["max"]:
        raise VoiceChatError(f"{key} is between {spec['min']} and {spec['max']}.")
    return f"{number:g}" if kind == "float" else str(number)


async def _read_config(server_id: str, path: str) -> str | None:
    try:
        return await engine.read_file(server_id, path)
    except Exception:       # noqa: BLE001 -- not written yet
        return None


# --- the port ------------------------------------------------------------------------

async def _row(server_id: str) -> Server | None:
    async with db.session() as s:
        return await s.get(Server, server_id)


def stored_port(row: Server | None) -> int | None:
    voice = ((row.limits or {}) if row else {}).get("voice") or {}
    port = voice.get("port")
    return int(port) if isinstance(port, int) and port > 0 else None


async def _store_port(server_id: str, port: int | None) -> None:
    async with db.session() as s:
        row = await s.get(Server, server_id)
        if row is None:
            return
        limits = dict(row.limits or {})
        voice = dict(limits.get("voice") or {})
        if port is None:
            voice.pop("port", None)
        else:
            voice["port"] = int(port)
        limits["voice"] = voice
        row.limits = limits


async def taken_ports(server_id: str) -> dict[int, str]:
    """UDP ports this server's voice chat must not use, and who has each:
    every game port and every other server's voice port on the same node,
    plus whatever the node says is held by something other than this server."""
    row = await _row(server_id)
    node = row.node_id if row else None
    out: dict[int, str] = {}
    async with db.session() as s:
        q = select(Server)
        if node is not None:
            q = q.where(Server.node_id == node)
        for other in (await s.execute(q)).scalars().all():
            if other.port:
                out.setdefault(int(other.port), f"{other.name}'s game port")
            if other.uuid != server_id:
                vp = stored_port(other)
                if vp:
                    out[vp] = f"{other.name}'s voice chat"
    try:
        for held in await engine.host_ports(server_id):
            port = int(held.get("port") or 0)
            if port and held.get("server") != server_id:
                out.setdefault(port, str(held.get("by") or "another program"))
    except Exception:       # noqa: BLE001 -- the node's view is a bonus, not a need
        pass
    return out


async def allocate(server_id: str, prefer: int | None = None) -> int:
    """A free UDP port for this server's voice chat: `prefer` if it is free,
    else the lowest free one from VOICE_FIRST."""
    taken = await taken_ports(server_id)
    if prefer and VOICE_FIRST <= prefer <= 65535 and prefer not in taken:
        return prefer
    for port in range(VOICE_FIRST, VOICE_LAST + 1):
        if port not in taken:
            return port
    raise VoiceChatError("Every voice chat port from 24454 to 24999 is in use on this "
                         "machine. Free one, or set a port by hand.", 409)


# --- reading and writing ---------------------------------------------------------------

async def status(server_id: str) -> dict[str, Any]:
    """What the Voice tab shows: whether it is installed, the port, every
    setting the panel offers (the file's value or the default), and whether
    the port is shared with another server."""
    found = await detect(server_id)
    if not found["present"]:
        return found
    text = await _read_config(server_id, found["config_path"])
    raw = parse(text or "")
    row = await _row(server_id)
    port_in_file = None
    try:
        port_in_file = int(raw["port"]) if "port" in raw else None
    except ValueError:
        port_in_file = None
    taken = await taken_ports(server_id)
    port = port_in_file or stored_port(row)
    clash = taken.get(port) if port and port > 0 else None
    return {
        **found,
        "configured": text is not None,
        "port": port,
        "stored_port": stored_port(row),
        "port_shared": port_in_file is not None and port_in_file <= 0,
        "clash": clash,
        "values": {k: _typed(k, raw.get(k)) for k in FIELDS},
        "fields": {k: dict(v) for k, v in FIELDS.items()},
        "game_port": int(row.port) if row and row.port else None,
    }


async def configure(server_id: str, values: dict[str, Any] | None = None, *,
                    port: int | None = None, job: Any = None) -> dict[str, Any]:
    """Write the config: the server's own UDP port (kept if it is free,
    otherwise a free one), plus any `values` given. Returns the new status
    and what changed."""
    found = await detect(server_id)
    if not found["present"]:
        raise VoiceChatError("Simple Voice Chat is not installed on this server.", 404)
    path = found["config_path"]
    text = await _read_config(server_id, path)
    raw = parse(text or "")
    row = await _row(server_id)

    updates: dict[str, str] = {}
    for key, value in (values or {}).items():
        updates[key] = _clean(key, value)

    current = None
    try:
        current = int(raw["port"]) if "port" in raw else None
    except ValueError:
        current = None
    if port is not None:
        if not 1024 <= int(port) <= 65535:
            raise VoiceChatError("A voice port is 1024 to 65535.")
        taken = await taken_ports(server_id)
        if int(port) in taken:
            raise VoiceChatError(f"UDP {port} is already {taken[int(port)]}.", 409)
        chosen = int(port)
    else:
        # Keep what it has when that is free; otherwise sort it out.
        keep = current if current and current > 0 else stored_port(row)
        chosen = await allocate(server_id, keep)
    moved = chosen != current
    updates["port"] = str(chosen)
    if not text:
        # A first write: blank bind address (the container's game address),
        # and the codec default, so the file reads complete to a person.
        updates.setdefault("bind_address", "")
    new_text = render(text or "", updates)
    if new_text != (text or ""):
        if not text:
            await engine.ensure_dir(server_id, path.rsplit("/", 1)[0])
        await engine.write_file(server_id, path, new_text)
    await _store_port(server_id, chosen)
    if job is not None and moved:
        job.log_line(f"Simple Voice Chat: UDP {chosen}"
                     + (f" (was {current}, which another server has)" if current else ""))
    out = await status(server_id)
    out["changed"] = sorted(updates) if new_text != (text or "") else []
    out["moved_from"] = current if moved else None
    return out


async def ensure(server_id: str, job: Any = None) -> dict[str, Any] | None:
    """Make an installed Voice Chat ready: its own port, written into its
    config. Called after every install and before every start. None when it
    is not installed. Never raises -- a voice config must not stop a server."""
    try:
        found = await detect(server_id)
        if not found["present"]:
            row = await _row(server_id)
            if stored_port(row):
                await _store_port(server_id, None)      # removed: free its port
            return None
        return await configure(server_id, job=job)
    except Exception as e:      # noqa: BLE001
        if job is not None:
            job.log_line(f"Simple Voice Chat could not be configured: {e}", "warn")
        return None
