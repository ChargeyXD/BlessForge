"""Who may ask this agent for what.

Two kinds of caller, two kinds of credential:

  * **The panel** sends `Authorization: Bearer <token_id>.<token>`, the node
    token minted at registration. Compared in constant time. Every /v1 route
    except the three browser ones requires it.
  * **A browser** (the console websocket, a file upload or download) brings
    a short-lived token the PANEL signed with its Ed25519 key. The agent
    holds only the public key, so a node that is broken into cannot mint
    tokens for any other node or server. The claims are checked strictly:

        sub   the user id            srv   the server uuid it is for
        perm  what it allows         aud   this node's id
        exp   at most 10 minutes after iat
        path  (transfers) the one file or folder it covers

    `alg` is pinned to EdDSA; a token with any other algorithm -- "none"
    included -- is refused before its claims are read.
"""
from __future__ import annotations

import hmac
import time

import jwt
from cryptography.hazmat.primitives.serialization import load_pem_public_key

MAX_TOKEN_SECONDS = 600


class Denied(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def check_node_token(header: str | None, token_id: str, token: str) -> None:
    raw = (header or "").strip()
    if not raw.lower().startswith("bearer "):
        raise Denied("a node token is required")
    presented = raw[7:].strip()
    want = f"{token_id}.{token}"
    if not hmac.compare_digest(presented.encode(), want.encode()):
        raise Denied("that is not this node's token")


class BrowserTokens:
    def __init__(self, public_key_pem: str, node_id: str):
        self.key = load_pem_public_key(public_key_pem.encode())
        self.node_id = node_id

    def verify(self, token: str | None, *, server_uuid: str, perm: str,
               path: str | None = None) -> dict:
        if not token:
            raise Denied("a token is required")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise Denied("that token is malformed")
        if header.get("alg") != "EdDSA":
            raise Denied("that token is not signed the way the panel signs them")
        try:
            claims = jwt.decode(
                token, self.key, algorithms=["EdDSA"], audience=self.node_id,
                options={"require": ["exp", "iat", "sub", "srv", "perm", "aud"]})
        except jwt.ExpiredSignatureError:
            raise Denied("that token has expired")
        except jwt.InvalidAudienceError:
            raise Denied("that token is for another node", 403)
        except jwt.PyJWTError as e:
            raise Denied(f"that token is not valid ({e})")
        if claims["exp"] - claims["iat"] > MAX_TOKEN_SECONDS:
            raise Denied("that token lives longer than the panel ever signs for", 403)
        if claims["iat"] > time.time() + 60:
            raise Denied("that token is from the future", 403)
        if claims["srv"] != server_uuid:
            raise Denied("that token is for another server", 403)
        perms = claims["perm"] if isinstance(claims["perm"], list) else [claims["perm"]]
        if perm not in perms:
            raise Denied(f"that token does not allow {perm}", 403)
        if path is not None and claims.get("path") != path:
            raise Denied("that token is for another file", 403)
        return claims
