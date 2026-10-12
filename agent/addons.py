"""The ports a server's add-ons open, besides the game port.

Crafty runs every server inside its own container and publishes a whole range
(25500-25600) to it, so whatever a mod opens in that range just works. Here
each server is a container of its own that publishes only what it is given --
which used to be the game port alone, so a Bedrock player could not reach
Geyser, nobody could talk over Simple Voice Chat, and the Dynmap page never
loaded.

This reads the server's own folder for the add-ons that open a port players
or browsers connect to, and returns those ports to publish next to the game
port. A config file the add-on wrote wins; an add-on whose jar is there but
that has not run yet (no config yet) gets its default port, which is what it
will bind on that first start.

Deliberately NOT published:

  * RCON. Anyone who reaches it can run any command, and the agent does not
    need it -- it writes to the console directly. Forward it yourself if you
    really want it outside.
  * Anything bound to the game port (Plasmo Voice's default, query on the
    game port, Voice Chat with port -1): the game port is already published
    for both TCP and UDP.

Every read goes through the file jail: the folder belongs to the server and
to every plugin in it, so a config file that is a link out of it is not
followed.
"""
from __future__ import annotations

import re
from pathlib import Path

from agent import files

# Add-ons, in the order they are reported. Each one: a name for the console,
# the protocol, its default port, the config files it may have written (one
# per platform), how to read the port out of one, and the jar-name prefixes
# that mean it is installed even if it has not run yet.
#
# `read` returns the port, None for "not stated here", or 0 for "this add-on
# is switched off / shares the game port" -- nothing to publish.


def _properties(key: str):
    rx = re.compile(rf"^\s*{re.escape(key)}\s*[=:]\s*(-?\d+)\s*$", re.M)

    def read(text: str) -> int | None:
        m = rx.search(text)
        if not m:
            return None
        port = int(m.group(1))
        return 0 if port <= 0 else port       # -1: Voice Chat shares the game port
    return read


def _yaml_child(section: str, key: str, *, off: str | None = None):
    """`key:` under a top-level `section:` block -- enough YAML for a port
    without a YAML parser. `off` names a sibling that, if false, means the
    feature is switched off."""
    head = re.compile(rf"^{re.escape(section)}\s*:\s*$")

    def read(text: str) -> int | None:
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not head.match(line):
                continue
            port = None
            for child in lines[i + 1:]:
                if child.strip() and not child[0].isspace():
                    break                       # the next top-level key
                m = re.match(rf"^\s+{re.escape(key)}\s*:\s*(\d+)\s*(#.*)?$", child)
                if m:
                    port = int(m.group(1))
                if off and re.match(rf"^\s+{re.escape(off)}\s*:\s*false\b", child):
                    return 0
            return port
        return None
    return read


def _dynmap(text: str) -> int | None:
    if re.search(r"^\s*disable-webserver\s*:\s*true\b", text, re.M):
        return 0
    m = re.search(r"^\s*webserver-port\s*:\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def _bluemap(text: str) -> int | None:
    # HOCON: `enabled: false` turns the built-in web server off.
    if re.search(r"^\s*enabled\s*[:=]\s*false\b", text, re.M):
        return 0
    m = re.search(r"^\s*port\s*[:=]\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def _plasmo(text: str) -> int | None:
    # [host] port = 0 means the game port, which is published already.
    m = re.search(r"^\s*port\s*=\s*(\d+)", text, re.M)
    return int(m.group(1)) if m else None


def _query(text: str) -> int | None:
    if not re.search(r"^\s*enable-query\s*=\s*true\s*$", text, re.M):
        return 0
    return _properties("query.port")(text)


ADDONS = [
    {"what": "Simple Voice Chat", "proto": "udp", "default": 24454,
     "configs": ["config/voicechat/voicechat-server.properties",
                 "plugins/voicechat/voicechat-server.properties"],
     "read": _properties("port"), "jars": ("voicechat-",)},
    {"what": "Geyser (Bedrock players)", "proto": "udp", "default": 19132,
     "configs": ["config/Geyser-Fabric/config.yml", "config/Geyser-NeoForge/config.yml",
                 "config/geyser/config.yml", "plugins/Geyser-Spigot/config.yml"],
     "read": _yaml_child("bedrock", "port"), "jars": ("geyser-", "geyser.")},
    {"what": "Dynmap", "proto": "tcp", "default": 8123,
     "configs": ["dynmap/configuration.txt", "plugins/dynmap/configuration.txt"],
     "read": _dynmap, "jars": ("dynmap",)},
    {"what": "BlueMap", "proto": "tcp", "default": 8100,
     "configs": ["config/bluemap/webserver.conf", "plugins/BlueMap/webserver.conf"],
     "read": _bluemap, "jars": ("bluemap",)},
    {"what": "squaremap", "proto": "tcp", "default": 8080,
     "configs": ["config/squaremap/config.yml", "plugins/squaremap/config.yml"],
     "read": _yaml_child("internal-webserver", "port", off="enabled"),
     "jars": ("squaremap",)},
    {"what": "Plasmo Voice", "proto": "udp", "default": 0,
     "configs": ["config/plasmovoice/config.toml", "plugins/PlasmoVoice/config.toml"],
     "read": _plasmo, "jars": ()},
    {"what": "the query port", "proto": "udp", "default": 0,
     "configs": ["server.properties"], "read": _query, "jars": ()},
]

# A folder full of add-ons is still a handful of ports; anything past this is
# not an add-on list any more.
MAX_ADDON_PORTS = 8


def _read(folder: Path, rel: str) -> str | None:
    try:
        return files.read_text(folder, rel)
    except (files.JailError, OSError):
        return None


def _jars(folder: Path) -> list[str]:
    names: list[str] = []
    for d in ("mods", "plugins"):
        try:
            names += [n.lower() for n, e in files.list_dir(folder, d).items()
                      if not e.get("dir") and not e.get("link") and n.lower().endswith(".jar")]
        except (files.JailError, OSError):
            pass
    return names


def detect(folder: Path, game_port: int) -> list[dict]:
    """[{"port", "proto", "what", "from"}] for this server's add-ons. `from`
    is the config file the port was read from, or None for a default."""
    jars = None
    out: list[dict] = []
    seen = {(game_port, "tcp"), (game_port, "udp")}
    for addon in ADDONS:
        port = source = None
        for rel in addon["configs"]:
            text = _read(folder, rel)
            if text is None:
                continue
            port, source = addon["read"](text), rel
            if port is not None:
                break
        if port is None and addon["jars"]:
            if jars is None:
                jars = _jars(folder)
            if any(j.startswith(addon["jars"]) for j in jars):
                port, source = addon["default"], None      # not run yet
        if not port or not 1024 <= port <= 65535:
            continue
        key = (port, addon["proto"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"port": port, "proto": addon["proto"],
                    "what": addon["what"], "from": source})
    return out[:MAX_ADDON_PORTS]
