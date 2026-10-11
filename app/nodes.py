"""Nodes: registering them, trusting them, and signing for browsers.

A node is a machine running the BlessForge agent. Registering one mints the
credentials both sides need and writes the agent's config file:

  * **the node token** (`<token_id>.<token>`). The panel sends it to the
    agent on every /v1 request; the agent sends it back on /api/agent/*. It
    is stored here encrypted with the app secret (BLESSFORGE_SECRET), so the
    database alone does not hand anybody the keys to every node.
  * **the panel's Ed25519 public key**, which the agent uses to check the
    short-lived tokens a browser brings to the console websocket and to file
    transfers. The private half never leaves the panel -- it lives in the
    database, encrypted the same way -- so a node that is broken into cannot
    mint a token for any other node or server.

The token is shown once, inside the config file, at registration. Losing it
means registering the node again, which mints a new one.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from app import config, db
from app.db.models import Meta, Node

log = logging.getLogger(__name__)

SIGNING_META = "browser_signing_key"
BROWSER_TOKEN_SECONDS = 300          # the agent refuses anything over 600


class NodeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- the app secret --------------------------------------------------------------


def _secret() -> bytes:
    """BLESSFORGE_SECRET, or a generated one kept in STATE_DIR for a dev box.

    Production sets the variable (the control-plane compose requires it).
    Without it, a random secret is generated once and kept beside the state,
    so a single-box install still encrypts node tokens -- but it says so,
    because losing that file means re-registering every node.
    """
    if config.SECRET_KEY:
        return config.SECRET_KEY.encode()
    path = config.STATE_DIR / "secret.key"
    try:
        return path.read_bytes().strip()
    except FileNotFoundError:
        value = secrets.token_urlsafe(48).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        log.warning("BLESSFORGE_SECRET is not set; generated %s. Back it up: "
                    "without it every node has to be registered again.", path)
        return value


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(b"blessforge:" + _secret()).digest())
    return Fernet(key)


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(blob: str) -> str:
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except InvalidToken:
        raise NodeError("a stored secret cannot be decrypted -- BLESSFORGE_SECRET "
                        "changed since it was written", 500)


# --- the browser signing key --------------------------------------------------------

_signing: Ed25519PrivateKey | None = None


async def signing_key() -> Ed25519PrivateKey:
    """The panel's Ed25519 key, made once and shared by every panel process."""
    global _signing
    if _signing is not None:
        return _signing
    async with db.session() as s:
        row = await s.get(Meta, SIGNING_META)
        if row is None:
            key = Ed25519PrivateKey.generate()
            pem = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()).decode()
            s.add(Meta(key=SIGNING_META, value={"key": encrypt(pem),
                                                "created": time.time()}))
            try:
                await s.flush()
            except Exception:       # noqa: BLE001 -- another process made it first
                await s.rollback()
                row = await s.get(Meta, SIGNING_META)
            else:
                _signing = key
                return key
        pem = decrypt(row.value["key"])
    _signing = serialization.load_pem_private_key(pem.encode(), password=None)
    return _signing


async def public_key_pem() -> str:
    key = await signing_key()
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()


async def sign_browser_token(*, user: dict, server_uuid: str, node_id: str,
                             perms: list[str], path: str | None = None,
                             seconds: int = BROWSER_TOKEN_SECONDS) -> tuple[str, float]:
    now = int(time.time())
    exp = now + max(30, min(int(seconds), 600))
    claims: dict[str, Any] = {"sub": str(user.get("id") or user.get("username")),
                              "srv": server_uuid, "aud": node_id,
                              "perm": sorted(set(perms)), "iat": now, "exp": exp}
    if path is not None:
        claims["path"] = path
    token = jwt.encode(claims, await signing_key(), algorithm="EdDSA")
    return token, float(exp)


# --- nodes ----------------------------------------------------------------------------


def _slug(text: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return base[:40] or "node"


def public(node: Node) -> dict:
    return {"id": node.id, "name": node.name, "region": node.region,
            "fqdn": node.fqdn, "agent_port": node.agent_port,
            "sftp_port": node.sftp_port, "scheme": node.scheme,
            "state": node.state, "agent_version": node.agent_version,
            "total_ram_mb": node.total_ram_mb, "cpu_threads": node.cpu_threads,
            "disk_gb": node.disk_gb, "last_seen": db.epoch(node.last_seen_at),
            "token_id": node.token_id}


def agent_url(node: Node) -> str:
    return f"{node.scheme}://{node.fqdn}:{node.agent_port}"


def token_header(node: Node) -> str:
    return f"Bearer {node.token_id}.{decrypt(node.token_encrypted)}"


async def register(*, name: str, fqdn: str, agent_port: int = 8443,
                   scheme: str = "https", region: str | None = None,
                   node_id: str | None = None, panel_url: str | None = None,
                   runtime: str = "docker", extra: dict | None = None) -> dict:
    """Create a node and return its public view plus the agent's config file."""
    fqdn = (fqdn or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9.\-:\[\]]{1,253}", fqdn):
        raise NodeError("that is not a host name or address")
    if scheme not in ("https", "http"):
        raise NodeError("scheme must be https or http")
    if not 1 <= int(agent_port) <= 65535:
        raise NodeError("that is not a port")
    token_id = secrets.token_hex(8)
    token = secrets.token_urlsafe(32)
    nid = _slug(node_id or name)
    async with db.session() as s:
        taken = {r for r in (await s.execute(select(Node.id))).scalars().all()}
        base, n = nid, 2
        while nid in taken:
            nid, n = f"{base}-{n}", n + 1
        node = Node(id=nid, name=(name or nid)[:80], fqdn=fqdn,
                    agent_port=int(agent_port), scheme=scheme, region=region,
                    token_id=token_id, token_encrypted=encrypt(token),
                    state="pending")
        s.add(node)
        await s.flush()
        view = public(node)
    toml = agent_config(node_id=nid, token_id=token_id, token=token,
                        panel_url=panel_url or config.PUBLIC_API_URL,
                        public_key=await public_key_pem(), runtime=runtime,
                        extra=extra or {})
    return {"node": view, "config": toml}


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def agent_config(*, node_id: str, token_id: str, token: str, panel_url: str,
                 public_key: str, runtime: str = "docker", extra: dict | None = None
                 ) -> str:
    """The node's /etc/blessforge-agent/agent.toml, ready to copy."""
    lines = [
        "# BlessForge node agent -- generated by the panel. Keep it secret:",
        "# it holds this node's token. Owner root, mode 600.",
        "[agent]",
        f"node_id = {_toml_value(node_id)}",
        f"token_id = {_toml_value(token_id)}",
        f"token = {_toml_value(token)}",
        f"panel_url = {_toml_value(panel_url)}",
        f"runtime = {_toml_value(runtime)}",
    ]
    for key, value in (extra or {}).items():
        if isinstance(value, dict):
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    lines.append(f'panel_public_key = """\n{public_key.strip()}\n"""')
    for key, value in (extra or {}).items():
        if isinstance(value, dict):
            lines.append(f"\n[agent.{key}]")
            for k, v in value.items():
                lines.append(f'"{k}" = {_toml_value(v)}')
    return "\n".join(lines) + "\n"


async def list_nodes() -> list[dict]:
    async with db.session() as s:
        return [public(n) for n in (await s.execute(select(Node).order_by(Node.id)))
                .scalars().all()]


async def all_rows() -> list[Node]:
    async with db.session() as s:
        return list((await s.execute(select(Node))).scalars().all())


async def verify_agent(header: str | None) -> Node:
    """The node presenting this Authorization header, or NodeError(401)."""
    raw = (header or "").strip()
    if not raw.lower().startswith("bearer "):
        raise NodeError("a node token is required", 401)
    token_id, _, token = raw[7:].strip().partition(".")
    if not token_id or not token:
        raise NodeError("a node token is required", 401)
    async with db.session() as s:
        node = (await s.execute(select(Node).where(Node.token_id == token_id))
                ).scalar_one_or_none()
    if node is None:
        hmac.compare_digest(token.encode(), b"x" * len(token))   # same work either way
        raise NodeError("that is not a node token", 401)
    if not hmac.compare_digest(token.encode(), decrypt(node.token_encrypted).encode()):
        raise NodeError("that is not a node token", 401)
    return node
