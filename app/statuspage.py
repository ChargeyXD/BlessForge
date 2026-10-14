"""The public status page: which servers are up, and who is on.

From the server-manager comparison in design/ (item 8). Off until an admin
turns it on, and then it shows only the servers they pick -- a box with a
private test server and a public one should be able to show just the one.
Player names are hidden unless asked for, and the join address is whatever
host the admin types (BlessForge does not know its public name).

It is the one page anyone can load, so what it reads is cached for a few
seconds: a link posted in a busy Discord is one read of each server per
CACHE seconds, however many people click it.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from sqlalchemy import select

from app import db, engine
from app.db.models import Meta, Server

KEY = "status_page"
CACHE = 15                          # seconds the public view is reused for
MAX_SERVERS = 50
DEFAULTS: dict[str, Any] = {"enabled": False, "title": "Server status", "blurb": "",
                            "servers": [], "show_players": False, "address": ""}

_cache: dict[str, Any] = {"at": 0.0, "view": None}
_lock = asyncio.Lock()


class StatusPageError(ValueError):
    """A setting that will not do (a 400, with the reason)."""


async def settings() -> dict[str, Any]:
    async with db.session() as s:
        row = await s.get(Meta, KEY)
        stored = dict(row.value or {}) if row else {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


async def save(body: dict[str, Any]) -> dict[str, Any]:
    cur = await settings()
    out = dict(cur)
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    if "show_players" in body:
        out["show_players"] = bool(body["show_players"])
    if "title" in body:
        title = str(body["title"] or "").strip()
        out["title"] = title[:80] or DEFAULTS["title"]
    if "blurb" in body:
        out["blurb"] = str(body["blurb"] or "").strip()[:280]
    if "address" in body:
        addr = str(body["address"] or "").strip().lower()
        if addr and not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", addr):
            raise StatusPageError("The address is a host name or IP, without a port "
                                  "(each server's own port is added).")
        out["address"] = addr
    if "servers" in body:
        ids = body["servers"]
        if not isinstance(ids, list):
            raise StatusPageError("servers is a list of server ids.")
        ids = [str(i) for i in ids][:MAX_SERVERS]
        async with db.session() as s:
            known = set((await s.execute(select(Server.uuid).where(
                Server.uuid.in_(ids)))).scalars().all()) if ids else set()
        missing = [i for i in ids if i not in known]
        if missing:
            raise StatusPageError(f"No such server: {missing[0]}")
        out["servers"] = list(dict.fromkeys(ids))
    async with db.session() as s:
        row = await s.get(Meta, KEY)
        if row is None:
            s.add(Meta(key=KEY, value=out))
        else:
            row.value = out
    _cache["at"] = 0.0
    return out


def _state(stats: dict[str, Any] | None) -> str:
    if stats is None:
        return "unknown"
    if stats.get("running"):
        return "starting" if stats.get("state") in ("starting", "queued") else "online"
    return "crashed" if stats.get("crashed") else "offline"


async def _one(row: Server, opts: dict[str, Any]) -> dict[str, Any]:
    try:
        stats = await engine.get_stats(row.uuid)
        if stats.get("node_down"):
            stats = None
    except Exception:       # noqa: BLE001 -- shown as unknown, not as an error
        stats = None
    try:
        manifest = await engine.read_manifest(row.uuid)
    except Exception:       # noqa: BLE001
        manifest = {}
    pack = manifest.get("pack") or {}
    state = _state(stats)
    up = state in ("online", "starting")
    out: dict[str, Any] = {
        "name": row.name, "state": state,
        "players": int((stats or {}).get("online") or 0) if up else 0,
        "max_players": (stats or {}).get("max") if up else None,
        "minecraft": manifest.get("minecraft") or row.mc_version or "",
        "loader": manifest.get("loader") or row.template or "",
        "pack": " ".join(x for x in (pack.get("name"), pack.get("version")) if x) or None,
        "uptime_s": engine.uptime_seconds(stats) if up and stats else None,
    }
    if opts["address"]:
        out["address"] = opts["address"] if row.port == 25565 \
            else f"{opts['address']}:{row.port}"
    if opts["show_players"] and up:
        out["names"] = [p.get("name") if isinstance(p, dict) else str(p)
                        for p in (stats or {}).get("players") or []][:100]
    return out


async def public_view() -> dict[str, Any] | None:
    """What the public page shows, or None when it is switched off."""
    opts = await settings()
    if not opts["enabled"]:
        return None
    if _cache["view"] is not None and time.monotonic() - _cache["at"] < CACHE:
        return _cache["view"]
    async with _lock:
        if _cache["view"] is not None and time.monotonic() - _cache["at"] < CACHE:
            return _cache["view"]
        async with db.session() as s:
            rows = {r.uuid: r for r in (await s.execute(select(Server).where(
                Server.uuid.in_(opts["servers"])))).scalars().all()} if opts["servers"] else {}
        ordered = [rows[i] for i in opts["servers"] if i in rows]
        servers = await asyncio.gather(*(_one(r, opts) for r in ordered))
        view = {"title": opts["title"], "blurb": opts["blurb"], "updated": time.time(),
                "servers": servers,
                "online": sum(1 for s in servers if s["state"] == "online"),
                "players": sum(s["players"] for s in servers)}
        _cache.update(at=time.monotonic(), view=view)
        return view
