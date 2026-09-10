"""Player management for one instance: ops, whitelist, bans, who is on.

Minecraft keeps this in five JSON files next to `server.properties`, and a
running server keeps its own copy in memory. That split is the whole problem:
editing `ops.json` while the server is up does nothing until it restarts, and
running `/op` while it is down is impossible. Getting it wrong silently is the
normal outcome, and it is why "I opped them and it didn't work" is such a
common thing to hear.

So every mutation here picks its route from the server's actual state:

  * **running** -> the console command, which the server applies immediately
    *and* writes to the file itself. Authoritative.
  * **stopped**  -> the JSON file, written directly, which the server will
    read on its next start.

The route taken is reported back, because "opped, effective now" and "opped,
takes effect when the server next starts" are different facts and the person
clicking the button needs to know which one they got.

Names are resolved to UUIDs through Mojang. When that is unreachable -- or the
server runs in offline mode -- the offline UUID is derived locally with the
exact algorithm the server itself uses, so an entry written here matches the
one the server would have written.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid as uuidlib
from typing import Any

import httpx

from app import crafty, properties

MOJANG_PROFILE = "https://api.mojang.com/users/profiles/minecraft/{name}"
SESSION_PROFILE = ("https://sessionserver.mojang.com/session/minecraft/"
                   "profile/{uuid}")
UA = "BlessForge/2.1 (+https://github.com/ChargeyXD/BlessForge)"

WHITELIST_FILE = "whitelist.json"
OPS_FILE = "ops.json"
BANNED_PLAYERS_FILE = "banned-players.json"
BANNED_IPS_FILE = "banned-ips.json"
USERCACHE_FILE = "usercache.json"
NOTES_FILE = ".blessforge-players.json"

VALID_NAME = re.compile(r"^[A-Za-z0-9_]{1,16}$")
VALID_IP = re.compile(r"^[0-9A-Fa-f:.]{3,45}$")

_profile_cache: dict[str, tuple[float, dict]] = {}
_PROFILE_TTL = 60 * 60 * 6


class PlayerError(ValueError):
    """Bad input from the caller -- surfaced as a 400."""


def guard_name(name: str) -> str:
    n = (name or "").strip()
    if not VALID_NAME.match(n):
        raise PlayerError(
            f"'{name}' is not a Minecraft username — 1 to 16 letters, digits "
            "or underscores."
        )
    return n


def guard_ip(ip: str) -> str:
    n = (ip or "").strip()
    if not VALID_IP.match(n):
        raise PlayerError(f"'{ip}' is not an IP address")
    return n


def offline_uuid(name: str) -> str:
    """The UUID an offline-mode server derives for a username.

    Java's `UUID.nameUUIDFromBytes("OfflinePlayer:<name>")` -- an MD5 v3 UUID.
    Reproduced exactly so an entry written while the server is down matches
    the one the server would write itself.
    """
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuidlib.UUID(bytes=bytes(digest)))


def dashed(raw: str) -> str:
    """Mojang returns UUIDs undashed; every server file wants them dashed."""
    s = (raw or "").replace("-", "").strip()
    if len(s) != 32:
        return raw
    return f"{s[:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:]}"


async def resolve_profile(name: str, *, online_mode: bool = True) -> dict:
    """`{name, uuid, source}` for a username."""
    name = guard_name(name)
    key = name.lower()
    hit = _profile_cache.get(key)
    now = time.time()
    if hit and now - hit[0] < _PROFILE_TTL:
        return hit[1]

    profile: dict | None = None
    if online_mode:
        try:
            async with httpx.AsyncClient(
                timeout=8, follow_redirects=True,
                headers={"User-Agent": UA},
            ) as c:
                r = await c.get(MOJANG_PROFILE.format(name=name))
                if r.status_code == 200 and r.content:
                    data = r.json()
                    profile = {"name": data.get("name") or name,
                               "uuid": dashed(data.get("id") or ""),
                               "source": "mojang"}
                elif r.status_code in (204, 404):
                    raise PlayerError(
                        f"Mojang has no account called '{name}'. Check the "
                        "spelling, or turn off online-mode if this is a "
                        "cracked server."
                    )
        except PlayerError:
            raise
        except Exception:
            profile = None      # unreachable: fall through to the offline id

    if profile is None:
        profile = {"name": name, "uuid": offline_uuid(name),
                   "source": "offline" if not online_mode else "offline-fallback"}
    _profile_cache[key] = (now, profile)
    return profile


# --- reading the files -------------------------------------------------


async def _read_json_list(server_id: str, path: str) -> list[dict]:
    try:
        raw = await crafty.read_file(server_id, path)
    except crafty.CraftyError:
        return []
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


async def _write_json_list(server_id: str, path: str, items: list) -> None:
    payload = json.dumps(items, indent=2)
    try:
        await crafty.write_file(server_id, path, payload)
    except crafty.CraftyError:
        await crafty.create_entry(server_id, ".", path, directory=False)
        await crafty.write_file(server_id, path, payload)


async def _read_notes(server_id: str) -> dict:
    try:
        raw = await crafty.read_file(server_id, NOTES_FILE)
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def _write_notes(server_id: str, data: dict) -> None:
    try:
        await crafty.write_file(server_id, NOTES_FILE,
                                json.dumps(data, indent=2))
    except crafty.CraftyError:
        try:
            await crafty.create_entry(server_id, ".", NOTES_FILE,
                                      directory=False)
            await crafty.write_file(server_id, NOTES_FILE,
                                    json.dumps(data, indent=2))
        except crafty.CraftyError:
            pass


_LIST_LINE = re.compile(
    r"There are (\d+)(?: of a max(?: of)? (\d+))? players online[:.]?\s*(.*)",
    re.I,
)


async def online_players(server_id: str, *, running: bool | None = None
                         ) -> dict:
    """Who is on the server right now.

    Crafty's own stats carry the count, and on most versions the names too.
    When they do not, the server's `list` output is the only source -- so it
    is parsed out of the console tail rather than sending a command and
    hoping (which would print `list` into everyone's chat log every refresh).
    """
    names: list[str] = []
    count = maximum = None
    try:
        stats = await crafty.get_stats(server_id)
        if running is None:
            running = bool(stats.get("running"))
        count = stats.get("online")
        maximum = stats.get("max")
        raw = stats.get("players")
        if isinstance(raw, str):
            raw = raw.strip()
            if raw.startswith("["):
                try:
                    raw = json.loads(raw.replace("'", '"'))
                except ValueError:
                    raw = [p.strip() for p in raw.strip("[]").split(",")]
            else:
                raw = [p.strip() for p in raw.split(",")] if raw else []
        if isinstance(raw, list):
            names = [str(p).strip().strip("'\"") for p in raw
                     if str(p).strip().strip("'\"")]
    except Exception:
        running = bool(running)

    if running and not names and (count or 0) > 0:
        try:
            lines = await crafty.console_lines(server_id)
            for line in reversed(lines[-400:]):
                m = _LIST_LINE.search(line)
                if not m:
                    continue
                tail = (m.group(3) or "").strip()
                if tail:
                    names = [n.strip() for n in tail.split(",") if n.strip()]
                break
        except Exception:
            pass

    return {"running": bool(running), "count": count if count is not None
            else len(names), "max": maximum, "names": names}


async def snapshot(server_id: str) -> dict:
    """Everything the Players tab needs, in one round of reads."""
    (whitelist_raw, ops_raw, bans_raw, ip_bans_raw, cache_raw, notes,
     props, live) = await asyncio.gather(
        _read_json_list(server_id, WHITELIST_FILE),
        _read_json_list(server_id, OPS_FILE),
        _read_json_list(server_id, BANNED_PLAYERS_FILE),
        _read_json_list(server_id, BANNED_IPS_FILE),
        _read_json_list(server_id, USERCACHE_FILE),
        _read_notes(server_id),
        _safe_properties(server_id),
        online_players(server_id),
        return_exceptions=False,
    )

    online_names = {n.lower() for n in live["names"]}
    op_by_uuid = {dashed(o.get("uuid", "")): o for o in ops_raw if isinstance(o, dict)}
    op_names = {str(o.get("name", "")).lower() for o in ops_raw
                if isinstance(o, dict)}
    wl_names = {str(w.get("name", "")).lower() for w in whitelist_raw
                if isinstance(w, dict)}
    ban_by_name = {str(b.get("name", "")).lower(): b for b in bans_raw
                   if isinstance(b, dict)}

    # One row per person, merged from every source, so the tab is a list of
    # players rather than four lists of files.
    people: dict[str, dict] = {}

    def touch(name: str, uuid_: str = "") -> dict:
        key = (name or uuid_).lower()
        row = people.setdefault(key, {
            "name": name, "uuid": dashed(uuid_) if uuid_ else "",
            "online": False, "op": False, "op_level": None,
            "whitelisted": False, "banned": False, "ban_reason": None,
            "ban_expires": None, "last_seen": None, "note": "",
        })
        if name and not row["name"]:
            row["name"] = name
        if uuid_ and not row["uuid"]:
            row["uuid"] = dashed(uuid_)
        return row

    for entry in cache_raw:
        if isinstance(entry, dict) and entry.get("name"):
            row = touch(entry["name"], entry.get("uuid", ""))
            row["last_seen"] = entry.get("expiresOn")
    for entry in whitelist_raw:
        if isinstance(entry, dict) and entry.get("name"):
            touch(entry["name"], entry.get("uuid", ""))["whitelisted"] = True
    for entry in ops_raw:
        if isinstance(entry, dict) and entry.get("name"):
            row = touch(entry["name"], entry.get("uuid", ""))
            row["op"] = True
            row["op_level"] = entry.get("level", 4)
            row["bypasses_player_limit"] = bool(entry.get("bypassesPlayerLimit"))
    for entry in bans_raw:
        if isinstance(entry, dict) and entry.get("name"):
            row = touch(entry["name"], entry.get("uuid", ""))
            row["banned"] = True
            row["ban_reason"] = entry.get("reason")
            row["ban_expires"] = entry.get("expires")
            row["banned_by"] = entry.get("source")
            row["banned_at"] = entry.get("created")
    for name in live["names"]:
        touch(name)["online"] = True

    for key, row in people.items():
        row["online"] = row["online"] or key in online_names
        row["note"] = (notes.get(key) or {}).get("note", "")
        row["avatar"] = avatar_url(row["uuid"], row["name"])

    ordered = sorted(
        people.values(),
        key=lambda p: (not p["online"], not p["op"], p["banned"],
                       (p["name"] or "").lower()),
    )

    whitelist_on = str(props.get("white-list", "false")).lower() == "true"
    enforce = str(props.get("enforce-whitelist", "false")).lower() == "true"
    return {
        "running": live["running"],
        "online": live["count"],
        "max_players": live["max"] or props.get("max-players"),
        "online_names": live["names"],
        "players": ordered,
        "counts": {
            "known": len(ordered),
            "online": sum(1 for p in ordered if p["online"]),
            "ops": sum(1 for p in ordered if p["op"]),
            "whitelisted": sum(1 for p in ordered if p["whitelisted"]),
            "banned": sum(1 for p in ordered if p["banned"]),
            "banned_ips": len(ip_bans_raw),
        },
        "banned_ips": [
            {"ip": b.get("ip"), "reason": b.get("reason"),
             "created": b.get("created"), "source": b.get("source"),
             "expires": b.get("expires")}
            for b in ip_bans_raw if isinstance(b, dict)
        ],
        "settings": {
            "whitelist_enabled": whitelist_on,
            "enforce_whitelist": enforce,
            "online_mode": str(props.get("online-mode", "true")).lower() == "true",
            "max_players": props.get("max-players"),
            "pvp": str(props.get("pvp", "true")).lower() == "true",
            "difficulty": props.get("difficulty"),
        },
        "note": None if live["running"] else (
            "This server is stopped, so changes are written to its player "
            "files and take effect the next time it starts."
        ),
        "unused": {"op_names": sorted(op_names), "wl_names": sorted(wl_names),
                   "ban_names": sorted(ban_by_name)},
    }


def avatar_url(uuid_: str, name: str) -> str:
    """A face for the row. Falls back to the name when there is no UUID."""
    who = (uuid_ or name or "steve").replace("-", "")
    return f"https://mc-heads.net/avatar/{who}/64"


async def _safe_properties(server_id: str) -> dict:
    try:
        raw = await crafty.read_file(server_id, "server.properties")
        values, _ = properties.parse(raw)
        return values
    except Exception:
        return {}


# --- mutations ---------------------------------------------------------


async def _is_running(server_id: str) -> bool:
    try:
        return bool((await crafty.get_stats(server_id)).get("running"))
    except Exception:
        return False


async def _command(server_id: str, line: str) -> None:
    await crafty.send_command(server_id, line)


def _result(action: str, target: str, live: bool, extra: str = "") -> dict:
    return {
        "ok": True, "action": action, "target": target,
        "applied": "console" if live else "file",
        "effective": "now" if live else "next start",
        "message": (
            f"{extra or action} — applied to the running server."
            if live else
            f"{extra or action} — written to the server's files; it takes "
            "effect the next time this server starts."
        ),
    }


async def set_op(server_id: str, name: str, on: bool, *, level: int = 4
                 ) -> dict:
    name = guard_name(name)
    live = await _is_running(server_id)
    if live:
        await _command(server_id, f"{'op' if on else 'deop'} {name}")
        return _result("op" if on else "deop", name, True,
                       f"{name} is {'now an operator' if on else 'no longer an operator'}")

    ops = await _read_json_list(server_id, OPS_FILE)
    ops = [o for o in ops
           if str((o or {}).get("name", "")).lower() != name.lower()]
    if on:
        profile = await resolve_profile(
            name, online_mode=await _online_mode(server_id))
        ops.append({
            "uuid": profile["uuid"], "name": profile["name"],
            "level": max(1, min(int(level or 4), 4)),
            "bypassesPlayerLimit": False,
        })
    await _write_json_list(server_id, OPS_FILE, ops)
    return _result("op" if on else "deop", name, False,
                   f"{name} {'added to' if on else 'removed from'} ops.json")


async def set_whitelist(server_id: str, name: str, on: bool) -> dict:
    name = guard_name(name)
    live = await _is_running(server_id)
    if live:
        await _command(server_id, f"whitelist {'add' if on else 'remove'} {name}")
        return _result("whitelist", name, True,
                       f"{name} {'added to' if on else 'removed from'} the whitelist")

    items = await _read_json_list(server_id, WHITELIST_FILE)
    items = [w for w in items
             if str((w or {}).get("name", "")).lower() != name.lower()]
    if on:
        profile = await resolve_profile(
            name, online_mode=await _online_mode(server_id))
        items.append({"uuid": profile["uuid"], "name": profile["name"]})
    await _write_json_list(server_id, WHITELIST_FILE, items)
    return _result("whitelist", name, False,
                   f"{name} {'added to' if on else 'removed from'} whitelist.json")


async def set_ban(server_id: str, name: str, on: bool, *, reason: str = ""
                  ) -> dict:
    name = guard_name(name)
    reason = (reason or "Banned by an operator").strip()[:200]
    live = await _is_running(server_id)
    if live:
        await _command(
            server_id,
            f"ban {name} {reason}" if on else f"pardon {name}")
        return _result("ban" if on else "pardon", name, True,
                       f"{name} was {'banned' if on else 'unbanned'}")

    items = await _read_json_list(server_id, BANNED_PLAYERS_FILE)
    items = [b for b in items
             if str((b or {}).get("name", "")).lower() != name.lower()]
    if on:
        profile = await resolve_profile(
            name, online_mode=await _online_mode(server_id))
        items.append({
            "uuid": profile["uuid"], "name": profile["name"],
            "created": time.strftime("%Y-%m-%d %H:%M:%S +0000",
                                     time.gmtime()),
            "source": "BlessForge", "expires": "forever", "reason": reason,
        })
    await _write_json_list(server_id, BANNED_PLAYERS_FILE, items)
    return _result("ban" if on else "pardon", name, False,
                   f"{name} {'added to' if on else 'removed from'} "
                   "banned-players.json")


async def set_ip_ban(server_id: str, ip: str, on: bool, *, reason: str = ""
                     ) -> dict:
    ip = guard_ip(ip)
    reason = (reason or "Banned by an operator").strip()[:200]
    live = await _is_running(server_id)
    if live:
        await _command(server_id,
                       f"ban-ip {ip} {reason}" if on else f"pardon-ip {ip}")
        return _result("ban-ip" if on else "pardon-ip", ip, True,
                       f"{ip} was {'banned' if on else 'unbanned'}")

    items = await _read_json_list(server_id, BANNED_IPS_FILE)
    items = [b for b in items if str((b or {}).get("ip", "")) != ip]
    if on:
        items.append({
            "ip": ip,
            "created": time.strftime("%Y-%m-%d %H:%M:%S +0000", time.gmtime()),
            "source": "BlessForge", "expires": "forever", "reason": reason,
        })
    await _write_json_list(server_id, BANNED_IPS_FILE, items)
    return _result("ban-ip" if on else "pardon-ip", ip, False)


async def kick(server_id: str, name: str, reason: str = "") -> dict:
    """Only meaningful on a running server -- say so rather than pretending."""
    name = guard_name(name)
    if not await _is_running(server_id):
        raise PlayerError(
            "This server is not running, so there is nobody to kick."
        )
    await _command(server_id,
                   f"kick {name} {reason}".strip())
    return _result("kick", name, True, f"{name} was kicked")


async def toggle_whitelist(server_id: str, on: bool) -> dict:
    """Turn whitelist enforcement on or off, in both places it lives.

    `whitelist on` changes the running server; `white-list=true` is what it
    reads at the next start. Writing only one is the classic way to have a
    whitelist that silently stops applying after a restart.
    """
    live = await _is_running(server_id)
    if live:
        await _command(server_id, f"whitelist {'on' if on else 'off'}")
    try:
        await properties.save(server_id, {"white-list": "true" if on else "false"})
    except Exception as e:
        raise PlayerError(f"could not write server.properties: {e}")
    return {
        "ok": True, "enabled": on, "applied": "both" if live else "file",
        "message": (
            f"Whitelist {'enabled' if on else 'disabled'}"
            + (" and applied to the running server." if live
               else "; it applies from the next start.")
        ),
    }


async def reload_whitelist(server_id: str) -> dict:
    """Make a running server re-read whitelist.json from disk."""
    if not await _is_running(server_id):
        raise PlayerError("This server is not running, so there is nothing to "
                          "reload — it reads the file on start anyway.")
    await _command(server_id, "whitelist reload")
    return {"ok": True, "message": "The server re-read whitelist.json."}


async def set_note(server_id: str, name: str, note: str) -> dict:
    name = guard_name(name)
    notes = await _read_notes(server_id)
    key = name.lower()
    text = (note or "").strip()[:500]
    if text:
        notes[key] = {"note": text, "name": name, "at": time.time()}
    else:
        notes.pop(key, None)
    await _write_notes(server_id, notes)
    return {"ok": True, "name": name, "note": text}


async def _online_mode(server_id: str) -> bool:
    props = await _safe_properties(server_id)
    return str(props.get("online-mode", "true")).lower() == "true"


async def bulk(server_id: str, action: str, names: list[str], *,
               reason: str = "") -> dict:
    """Apply one action to several players, reporting each outcome."""
    handlers = {
        "op": lambda n: set_op(server_id, n, True),
        "deop": lambda n: set_op(server_id, n, False),
        "whitelist": lambda n: set_whitelist(server_id, n, True),
        "unwhitelist": lambda n: set_whitelist(server_id, n, False),
        "ban": lambda n: set_ban(server_id, n, True, reason=reason),
        "pardon": lambda n: set_ban(server_id, n, False),
        "kick": lambda n: kick(server_id, n, reason),
    }
    if action not in handlers:
        raise PlayerError(f"'{action}' is not a player action")
    if not names:
        raise PlayerError("no players were selected")
    done, failed = [], []
    for name in names[:100]:
        try:
            done.append(await handlers[action](name))
        except Exception as e:
            failed.append({"name": name, "error": str(e)})
    return {"action": action, "applied": done, "failed": failed,
            "count": len(done)}
