"""server.properties: full read/write with types and explanations.

The Optimize tab exposes a handful of performance keys. This module backs the
complete editor: every key in the file, typed so the UI can render a checkbox
for a boolean and a dropdown for an enum, plus a short note on what each one
actually does.

Keys not in the catalogue are still shown and editable -- mods and forks add
their own, and hiding them would make the editor lie about the file's contents.
"""
from __future__ import annotations

import re

from app import config, engine

PROPERTIES_FILE = "server.properties"

# key -> (type, default, group, description, choices)
# Types: bool | int | string | enum
CATALOG: dict[str, tuple] = {
    # --- Identity -------------------------------------------------------
    "motd": ("string", "A Minecraft Server", "Identity",
             "The line players see under the server name in their list.", None),
    "server-port": ("int", "25565", "Network",
                    "TCP port the server listens on. Changing it here alone is "
                    "not enough -- use the Port control so the server record "
                    "and the published port stay in step.", None),
    "server-ip": ("string", "", "Network",
                  "Bind address. Leave empty to listen on all interfaces.", None),
    "query.port": ("int", "25565", "Network",
                   "Port for the query protocol. Normally matches server-port.",
                   None),
    "enable-query": ("bool", "false", "Network",
                     "Answer GameSpy4 query requests (used by server listings).",
                     None),
    "enable-rcon": ("bool", "false", "Network",
                    "Enable remote console. Leave off unless you need it -- it "
                    "is a remote command channel.", None),
    "rcon.port": ("int", "25575", "Network", "Port for RCON.", None),
    "rcon.password": ("string", "", "Network",
                      "RCON password. Anyone with this can run any command.",
                      None),
    "enable-status": ("bool", "true", "Network",
                      "Reply to status pings so the server shows as online.",
                      None),
    "network-compression-threshold": (
        "int", "256", "Network",
        "Compress packets above this size in bytes. Raising it trades "
        "bandwidth for CPU; -1 disables compression.", None),
    "prevent-proxy-connections": (
        "bool", "false", "Network",
        "Reject players whose Mojang-reported region does not match.", None),

    # --- Players --------------------------------------------------------
    "max-players": ("int", "20", "Players", "Maximum simultaneous players.", None),
    "online-mode": ("bool", "true", "Players",
                    "Verify players against Mojang's session servers. Turning "
                    "this off lets anyone join under any name -- only do it "
                    "behind a proxy that authenticates for you.", None),
    "white-list": ("bool", "false", "Players",
                   "Only allow players on the whitelist.", None),
    "enforce-whitelist": ("bool", "false", "Players",
                          "Kick players already online who are not whitelisted.",
                          None),
    "pvp": ("bool", "true", "Players", "Allow players to damage each other.", None),
    "player-idle-timeout": ("int", "0", "Players",
                            "Kick idle players after this many minutes. 0 disables.",
                            None),
    "op-permission-level": ("enum", "4", "Players",
                            "Default permission tier granted to operators.",
                            ["1", "2", "3", "4"]),
    "enforce-secure-profile": ("bool", "true", "Players",
                               "Require signed chat profiles.", None),
    "hide-online-players": ("bool", "false", "Players",
                            "Omit the player list from status pings.", None),

    # --- World ----------------------------------------------------------
    "level-name": ("string", "world", "World",
                   "Folder holding the world. Changing this starts a NEW world "
                   "and leaves the old one on disk.", None),
    "level-seed": ("string", "", "World",
                   "Seed for generation. Only affects a world being created.",
                   None),
    "level-type": ("string", "minecraft:normal", "World",
                   "Generator type. Modded packs often set their own.", None),
    "generate-structures": ("bool", "true", "World",
                            "Generate villages, temples and similar.", None),
    "generator-settings": ("string", "{}", "World",
                           "JSON settings for custom generators.", None),
    "max-world-size": ("int", "29999984", "World",
                       "World border radius in blocks.", None),
    "allow-nether": ("bool", "true", "World", "Enable the Nether.", None),
    "difficulty": ("enum", "easy", "World", "Game difficulty.",
                   ["peaceful", "easy", "normal", "hard"]),
    "gamemode": ("enum", "survival", "World", "Default game mode for new players.",
                 ["survival", "creative", "adventure", "spectator"]),
    "force-gamemode": ("bool", "false", "World",
                       "Reset players to the default game mode on join.", None),
    "hardcore": ("bool", "false", "World",
                 "Death is permanent and players are set to spectator.", None),
    "spawn-monsters": ("bool", "true", "World", "Allow hostile mob spawning.", None),
    "spawn-npcs": ("bool", "true", "World", "Allow villagers.", None),
    "spawn-animals": ("bool", "true", "World", "Allow passive mobs.", None),
    "spawn-protection": ("int", "16", "World",
                         "Radius around spawn only operators may build in. "
                         "0 disables.", None),
    "allow-flight": ("bool", "false", "World",
                     "Permit flight in survival. Leave off unless a mod needs "
                     "it, or anti-cheat will kick players.", None),
    "view-distance": ("int", "10", "Performance",
                      "Chunks sent to each player. The single biggest "
                      "server-side cost on a modded pack.", None),
    "simulation-distance": ("int", "10", "Performance",
                            "How far entities and blocks actually tick. Cheaper "
                            "to lower than view-distance, and less noticeable.",
                            None),
    "entity-broadcast-range-percentage": (
        "int", "100", "Performance",
        "How far entities are tracked, as a percentage. Lowering cuts packets.",
        None),
    "max-tick-time": ("int", "60000", "Performance",
                      "Watchdog timeout in ms. Big packs exceed the default "
                      "during world load; -1 disables the watchdog.", None),
    "sync-chunk-writes": ("bool", "true", "Performance",
                          "Write chunks synchronously. Turning this off is "
                          "noticeably smoother on spinning disks.", None),
    "use-native-transport": ("bool", "true", "Performance",
                             "Use optimised Linux networking.", None),
    "max-chained-neighbor-updates": (
        "int", "1000000", "Performance",
        "Cap on chained block updates, to contain redstone lag machines.", None),

    # --- Misc -----------------------------------------------------------
    "enable-command-block": ("bool", "false", "Misc",
                             "Allow command blocks to run.", None),
    "function-permission-level": ("enum", "2", "Misc",
                                  "Permission tier for datapack functions.",
                                  ["1", "2", "3", "4"]),
    "broadcast-console-to-ops": ("bool", "true", "Misc",
                                 "Show console command output to operators.", None),
    "broadcast-rcon-to-ops": ("bool", "true", "Misc",
                              "Show RCON command output to operators.", None),
    "log-ips": ("bool", "true", "Misc", "Record player IPs in the log.", None),
    "require-resource-pack": ("bool", "false", "Misc",
                              "Disconnect players who decline the resource pack.",
                              None),
    "resource-pack": ("string", "", "Misc", "URL of a resource pack to offer.",
                      None),
    "resource-pack-sha1": ("string", "", "Misc",
                           "SHA-1 of the resource pack, so clients can cache it.",
                           None),
    "resource-pack-prompt": ("string", "", "Misc",
                             "Message shown with the resource pack prompt.", None),
    "text-filtering-config": ("string", "", "Misc",
                              "Path to a chat filtering configuration.", None),
    "initial-enabled-packs": ("string", "vanilla", "Misc",
                              "Datapacks enabled when the world is created.", None),
    "initial-disabled-packs": ("string", "", "Misc",
                               "Datapacks disabled when the world is created.",
                               None),
    "accepts-transfers": ("bool", "false", "Misc",
                          "Accept players transferred from another server.", None),
    "bug-report-link": ("string", "", "Misc", "Link shown on a crash.", None),
    "pause-when-empty-seconds": (
        "int", "60", "Performance",
        "Stop ticking after this long with no players. Saves CPU on an idle "
        "server; 0 keeps it always running.", None),
}

# Every group a key can carry. A name missing here gets no tab, and its
# keys become unreachable in the editor -- which is how `motd` (group
# "Identity") ended up as the one property of 62 you could not edit.
GROUP_ORDER = ["Identity", "Network", "Performance", "Players", "World",
               "Misc", "Other"]

# Changing these needs more than a file write, so the plain editor refuses
# them and points at the dedicated control.
GUARDED_KEYS = {
    "server-port": "Use the Port control so the server record stays in step.",
}


def parse(text: str) -> tuple[dict[str, str], list[str]]:
    """Return (values, raw_lines). Comments and order are preserved."""
    values: dict[str, str] = {}
    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values, lines


def render(original: str, updates: dict[str, str]) -> str:
    """Apply updates while keeping comments, ordering and untouched keys."""
    lines = original.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"^([\w.\-]+)\s*=", line)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    return "\n".join(out) + "\n"


def coerce(key: str, value) -> str:
    """Normalise a submitted value to what Minecraft expects in the file."""
    kind = CATALOG.get(key, ("string",))[0]
    if kind == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        return "true" if str(value).strip().lower() in ("1", "true", "yes", "on") \
            else "false"
    if kind == "int":
        try:
            return str(int(str(value).strip()))
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number")
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError(f"{key} cannot contain a line break")
    return text


async def load(server_id: str) -> dict:
    """Every property in the file, typed and described."""
    try:
        raw = await engine.read_file(server_id, PROPERTIES_FILE)
    except engine.EngineError as e:
        raise RuntimeError(f"could not read {PROPERTIES_FILE}: {e}")

    values, _ = parse(raw)
    items = []
    for key, value in values.items():
        kind, default, group, desc, choices = CATALOG.get(
            key, ("string", "", "Other", "", None)
        )
        items.append({
            "key": key,
            "value": value,
            "type": kind,
            "default": default,
            "group": group,
            "description": desc,
            "choices": choices,
            "known": key in CATALOG,
            "modified": bool(default) and value != default,
            "guarded": GUARDED_KEYS.get(key),
        })

    # Known-but-absent keys are offered too, so people can add one without
    # hand-editing the file.
    for key, (kind, default, group, desc, choices) in CATALOG.items():
        if key in values:
            continue
        items.append({
            "key": key, "value": default, "type": kind, "default": default,
            "group": group, "description": desc, "choices": choices,
            "known": True, "modified": False, "absent": True,
            "guarded": GUARDED_KEYS.get(key),
        })

    items.sort(key=lambda i: (
        GROUP_ORDER.index(i["group"]) if i["group"] in GROUP_ORDER else 99,
        i["key"],
    ))
    groups = []
    for name in GROUP_ORDER:
        count = sum(1 for i in items if i["group"] == name)
        if count:
            groups.append({"name": name, "count": count})
    return {"file": PROPERTIES_FILE, "raw": raw, "items": items,
            "groups": groups, "count": len(values)}


async def _read_or_new(server_id: str) -> str:
    """server.properties, or "" for a server that has never started.

    Minecraft writes the file on its first start, so a freshly created
    server has none. Starting from empty is right: render() appends the
    keys being set, and Minecraft fills in every other default itself.
    Anything other than "not there" is still an error.
    """
    try:
        return await engine.read_file(server_id, PROPERTIES_FILE)
    except engine.EngineError as e:
        if e.status == 404:
            return ""
        raise RuntimeError(f"could not read {PROPERTIES_FILE}: {e}")


async def save(server_id: str, updates: dict) -> dict:
    """Write a set of property changes back to the instance."""
    if not isinstance(updates, dict) or not updates:
        raise ValueError("no changes supplied")

    raw = await _read_or_new(server_id)

    clean: dict[str, str] = {}
    rejected: list[dict] = []
    for key, value in updates.items():
        if not re.match(r"^[\w.\-]+$", str(key)):
            rejected.append({"key": key, "why": "invalid property name"})
            continue
        if key in GUARDED_KEYS:
            rejected.append({"key": key, "why": GUARDED_KEYS[key]})
            continue
        try:
            clean[key] = coerce(key, value)
        except ValueError as e:
            rejected.append({"key": key, "why": str(e)})

    if clean:
        await engine.write_file(server_id, PROPERTIES_FILE, render(raw, clean))
    return {"saved": clean, "rejected": rejected,
            "restart_required": bool(clean)}


# --- port ---------------------------------------------------------------

# The game ports a node's edge forwards. A port outside the range binds on the
# node but nobody outside can reach it -- worth saying rather than letting
# someone wonder why nobody can connect.
PUBLISHED_RANGE = config.GAME_PORT_RANGE


async def _taken_by_apps(server_id: str | None) -> list[dict]:
    """Host ports another app on this machine holds (its containers' published
    ports, another panel's whole range among them), as runs: [{"from", "to", "by"}].
    No server: the machine a new server would go to."""
    try:
        held = [p for p in (await engine.host_ports(server_id) if server_id
                            else await engine.node_host_ports())
                if not p.get("server")]
    except Exception:       # noqa: BLE001 -- a node that cannot say is not a refusal
        return []
    runs: list[dict] = []
    for p in sorted(held, key=lambda p: p["port"]):
        if runs and runs[-1]["by"] == p["by"] and runs[-1]["to"] == p["port"] - 1:
            runs[-1]["to"] = p["port"]
        else:
            runs.append({"from": p["port"], "to": p["port"], "by": p["by"]})
    return runs


async def taken_by_apps() -> list[dict]:
    """For the new-server form: what other apps hold where it would go."""
    return await _taken_by_apps(None)


async def port_status(server_id: str) -> dict:
    """Current port, plus what else on this node is already using one."""
    server = await engine.get_server(server_id)
    current = server.get("server_port")

    props_port = query_port = None
    try:
        raw = await engine.read_file(server_id, PROPERTIES_FILE)
        values, _ = parse(raw)
        props_port = values.get("server-port")
        query_port = values.get("query.port")
    except engine.EngineError:
        pass

    taken = []
    try:
        for other in await engine.list_servers():
            if other.get("server_id") == server_id:
                continue
            taken.append({"name": other.get("server_name"),
                          "port": other.get("server_port")})
    except engine.EngineError:
        pass

    mismatch = (
        props_port is not None and current is not None
        and str(props_port) != str(current)
    )
    return {
        "record_port": current,
        "properties_port": props_port,
        "query_port": query_port,
        "auto": bool((server.get("limits") or {}).get("port_auto")),
        "taken_by_apps": await _taken_by_apps(server_id),
        "in_use_by_others": taken,
        "published_range": list(PUBLISHED_RANGE),
        "mismatch": mismatch,
        "note": (
            "The server record and server.properties disagree. The node "
            "publishes and watches the record's port, while Minecraft binds "
            "the one in server.properties -- saving a port here fixes both."
        ) if mismatch else None,
    }


async def set_auto(server_id: str, auto: bool) -> dict:
    """Port: automatic. On, the node uses the last port if it is free at a
    start, otherwise the first free one in the game port range, and says
    which (the record follows)."""
    server = await engine.get_server(server_id)
    limits = {**(server.get("limits") or {}), "port_auto": bool(auto)}
    await engine.update_server(server_id, {"limits": limits})
    return {"auto": bool(auto)}


async def set_port(server_id: str, port: int, *, update_query: bool = True,
                   force: bool = False) -> dict:
    """Change a server's port in both places that matter.

    The server record holds the port the node publishes and polls, while
    Minecraft binds whatever is in server.properties. Setting only one leaves
    the server running but unreachable or shown as offline, so both are
    written together. The node applies a new port on the next start.
    """
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("port must be a number")
    if not 1024 <= port <= 65535:
        raise ValueError("port must be between 1024 and 65535")

    # Servers may share a port -- one public address, switched between --
    # so another server on it is information, not a refusal: only one of
    # them can run at a time, and starting the other offers to switch.
    # `force` is kept for callers; nothing is refused either way.
    del force
    warnings: list[str] = []
    me = next((s for s in await engine.list_servers() if s.get("server_id") == server_id),
              {})
    conflicts = [other.get("server_name") for other in await engine.list_servers()
                 if other.get("server_id") != server_id
                 and other.get("server_port") == port
                 and other.get("node") == me.get("node")]
    if conflicts:
        warnings.append(
            f"Also used by {', '.join(conflicts)} (only one can run at a time)."
        )
    for run in await _taken_by_apps(server_id):
        if run["from"] <= port <= run["to"]:
            warnings.append(f"Port {port} is taken by another app on this machine "
                            f"({run['by']}); this server cannot start on it while "
                            "that runs. Port: automatic picks a free one instead.")

    low, high = PUBLISHED_RANGE
    if not low <= port <= high:
        warnings.append(
            f"This node's edge forwards game ports {low}-{high}. Port {port} "
            "will bind on the node but will not be reachable from outside "
            "until it is forwarded too."
        )

    # server.properties first: if this fails, leave the record alone so the
    # two cannot end up disagreeing.
    updates = {"server-port": str(port)}
    if update_query:
        updates["query.port"] = str(port)
    raw = await _read_or_new(server_id)
    await engine.write_file(server_id, PROPERTIES_FILE, render(raw, updates))

    await engine.update_server(server_id, {"server_port": port})

    return {
        "port": port,
        "updated": ["server.properties"] + (["query.port"] if update_query else [])
        + ["server record"],
        "warnings": warnings,
        "restart_required": True,
    }
