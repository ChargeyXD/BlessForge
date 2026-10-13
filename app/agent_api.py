"""/api/agent/*: what the node agents call.

Authenticated by the node token (`Authorization: Bearer <token_id>.<token>`),
never by a session: these routes are `NODE` in app/policy.py, which the
session middleware lets through and `_node()` below then checks. A request
with no valid node token gets a 401 and nothing else.

  GET  /api/agent/servers    every server on this node, as desired state
  POST /api/agent/events     status changes, crashes, installs, stats
  POST /api/agent/sftp-auth  check an SFTP login (the SFTP server is WP-11)
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import time

from fastapi import APIRouter, Body, Header, HTTPException
from sqlalchemy import select

from app import auth, db, nodes
from app.db.models import Node, Server
from app.engine.agent import spec_of

log = logging.getLogger(__name__)
router = APIRouter()

# The latest numbers each server's agent reported, for the fleet and the
# Overview. Live state, so memory rather than the database; WP-15 keeps
# history.
LIVE: dict[str, dict] = {}

# Events worth a line in the audit log -- the operator needs to be able to
# find out afterwards that a server crash-looped at 3am.
_AUDITED = {"crash", "crash_loop", "disk_full", "drift", "agent_started",
            "port_changed"}
_STATUS = {"starting": "running", "running": "running", "offline": "stopped",
           # Waiting for a boot slot on its node: it will start by itself.
           "queued": "running"}


async def _node(authorization: str | None) -> Node:
    try:
        return await nodes.verify_agent(authorization)
    except nodes.NodeError as e:
        raise HTTPException(e.status, str(e))


@router.get("/api/agent/servers")
async def agent_servers(authorization: str | None = Header(None)) -> dict:
    node = await _node(authorization)
    async with db.session() as s:
        rows = (await s.execute(select(Server).where(Server.node_id == node.id)))\
            .scalars().all()
        return {"node": node.id, "servers": [spec_of(r) for r in rows]}


@router.post("/api/agent/events")
async def agent_events(body: dict = Body(...),
                       authorization: str | None = Header(None)) -> dict:
    node = await _node(authorization)
    events = body.get("events") or []
    if not isinstance(events, list):
        raise HTTPException(400, "events must be a list")
    statuses: dict[str, str] = {}
    moved: dict[str, int] = {}
    version = None
    for ev in events[:1000]:
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("type") or "")
        server = ev.get("server")
        if kind == "stats" and isinstance(ev.get("servers"), dict):
            for sid, stats in ev["servers"].items():
                if isinstance(stats, dict):
                    LIVE[str(sid)] = {**stats, "at": ev.get("at") or time.time(),
                                      "node": node.id}
        elif kind == "status" and server:
            statuses[str(server)] = _STATUS.get(str(ev.get("state")), "stopped")
        elif kind == "port_changed" and server and isinstance(ev.get("port"), int):
            # Port: automatic chose another port at this start.
            moved[str(server)] = int(ev["port"])
        elif kind == "agent_started":
            version = str(ev.get("version") or "")[:32]
        if kind in _AUDITED:
            detail = ", ".join(f"{k}={v}" for k, v in ev.items()
                               if k not in ("type", "server", "at"))[:400]
            await auth.audit(actor=f"node:{node.id}", action=f"agent.{kind}",
                             server_id=str(server or ""), detail=detail,
                             ok=kind not in ("crash", "crash_loop", "disk_full"))
    async with db.session() as s:
        row = await s.get(Node, node.id)
        row.last_seen_at = dt.datetime.now(dt.timezone.utc)
        if row.state == "pending":
            row.state = "online"
        if version:
            row.agent_version = version
        for sid, status in statuses.items():
            srv = await s.get(Server, sid)
            if srv is not None and srv.node_id == node.id and srv.status != "installing":
                srv.status = status
        for sid, port in moved.items():
            srv = await s.get(Server, sid)
            if srv is not None and srv.node_id == node.id and 1024 <= port <= 65535:
                srv.port = port
    return {"accepted": len(events)}


_SFTP_USER = re.compile(r"^([a-z0-9._-]{2,32})\.([0-9a-f]{8})$")


@router.post("/api/agent/sftp-auth")
async def agent_sftp_auth(body: dict = Body(...),
                          authorization: str | None = Header(None)) -> dict:
    """An SFTP login: `<account>.<first 8 of the server id>` plus a password.

    Answers with the server and what the account may do on it, or 403. Key
    logins arrive with the SFTP server itself (WP-11), when accounts can hold
    keys; until then only passwords are checked.
    """
    node = await _node(authorization)
    m = _SFTP_USER.match(str(body.get("username") or "").lower())
    if not m or not body.get("password"):
        raise HTTPException(403, "that login is not accepted")
    try:
        user = await auth.authenticate(m.group(1), str(body["password"]))
    except auth.AuthError:
        raise HTTPException(403, "that login is not accepted")
    async with db.session() as s:
        rows = (await s.execute(select(Server).where(
            Server.node_id == node.id, Server.uuid.startswith(m.group(2))))).scalars().all()
    if len(rows) != 1 or not auth.in_scope(user, rows[0].uuid) \
            or not auth.may(user, "files.read"):
        raise HTTPException(403, "that login is not accepted")
    perms = [p for p in ("files.read", "files.write") if auth.may(user, p)]
    await auth.audit(actor=user["username"], action="sftp.login",
                     server_id=rows[0].uuid, detail=f"node {node.id}")
    return {"server": rows[0].uuid, "permissions": perms}
