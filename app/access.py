"""Two-factor sign-in, API tokens, and per-server grants.

From the server-manager comparison in design/ (item 7). app/auth.py is who
you are and what your role lets you do; this is the three ways that gets
finer.

Two-factor sign-in (TOTP, RFC 6238)
-----------------------------------
The usual six-digit codes from an authenticator app, 30-second steps, one
step either side allowed for a phone clock that is a little out. The secret
is sealed with the app secret (the same Fernet key as node tokens), so a
copied database alone does not give the codes away. A code works once:
the step it matched is recorded and anything at or before it is refused,
so a code read over a shoulder is spent.

Signing in becomes two requests. The password step answers with a short-
lived *challenge* instead of a session; the code step trades the challenge
and a code for the session. The challenge is signed, not stored: it names
the account and its expiry, and its signature covers the account's password
salt and when 2FA was turned on, so changing either kills every challenge
in flight. Wrong codes count towards the same lockout as wrong passwords,
and a right password no longer resets that count while 2FA is on -- else
"password, seven guesses, password, seven guesses" would never lock.

Ten recovery codes are shown once when 2FA is turned on. They are stored as
HMACs under the app secret and each works once.

API tokens
----------
For scripts, Discord bots and Home Assistant: `Authorization: Bearer
bf_...`. A token acts as its owner with less, never more -- its own subset
of permissions, and optionally only some servers -- and both are checked
against the owner's CURRENT rights on every request, so demoting the owner
demotes their tokens. Tokens cannot reach the admin routes or anything under
/api/auth (no minting tokens with a token, no turning 2FA off with one).

Per-server grants
-----------------
The `server_users` table: permissions one account has on one server on top
of its role, and the server put in its scope. A guest everywhere can be an
operator of the server they run.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from typing import Any, Callable
from urllib.parse import quote

from sqlalchemy import delete, func, select, update

from app import auth, db
from app.auth import AuthError
from app.db.models import ApiToken, Server, ServerUser, User

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_SKEW = 1                       # steps either side accepted
CHALLENGE_TTL = 300                 # seconds to type the code in
RECOVERY_COUNT = 10
ISSUER = "BlessForge"

TOKEN_PREFIX = "bf_"
MAX_TOKENS = 25                     # per account
TOKEN_USED_EVERY = 300              # seconds between last_used writes
MAX_TOKEN_DAYS = 365

# Per-server grants: anything about one server. The global ones (creating
# servers, roulette, racks) are not about a server, so not grantable on one.
GRANTABLE = [p for p in auth.PERMISSIONS if p not in auth.GLOBAL_PERMISSIONS]


# --- sealing and keys --------------------------------------------------------------

def _app_secret() -> bytes:
    from app import nodes
    return nodes._secret()


def _seal(secret: str) -> str:
    """The secret's 20 raw bytes, Fernet-sealed: always 120 characters, which
    fits the column (sealing the base32 text would not)."""
    from app import nodes
    return nodes._fernet().encrypt(_secret_bytes(secret)).decode()


def _open(blob: str) -> str:
    from app import nodes
    try:
        raw = nodes._fernet().decrypt(blob.encode())
    except Exception:
        raise AuthError("This account's two-factor secret cannot be read (the app "
                        "secret changed). An admin can reset two-factor for it.", 500)
    return base64.b32encode(raw).decode().rstrip("=")


def _key(purpose: str) -> bytes:
    return hashlib.sha256(b"blessforge:" + purpose.encode() + b":" + _app_secret()).digest()


# --- TOTP ------------------------------------------------------------------------------

def new_secret() -> str:
    """20 random bytes, base32 without padding (what authenticator apps take)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _secret_bytes(secret: str) -> bytes:
    s = re.sub(r"[\s-]", "", secret).upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(key: bytes, counter: int, digits: int = TOTP_DIGITS) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(n % 10 ** digits).zfill(digits)


def code_at(secret: str, when: float) -> str:
    return hotp(_secret_bytes(secret), int(when // TOTP_STEP))


def match_code(secret: str, code: str, now: float,
               last_step: int | None = None) -> int | None:
    """The time step `code` matches, or None. Steps at or before `last_step`
    are refused: a code works once."""
    code = re.sub(r"\s", "", str(code or ""))
    if not re.fullmatch(rf"\d{{{TOTP_DIGITS}}}", code):
        return None
    key = _secret_bytes(secret)
    step = int(now // TOTP_STEP)
    found = None
    for s in range(step - TOTP_SKEW, step + TOTP_SKEW + 1):
        # Every candidate is computed and compared, so the time taken does
        # not say which step (if any) was close.
        if hmac.compare_digest(hotp(key, s), code) and (last_step is None or s > last_step):
            found = s
    return found


def otpauth_uri(secret: str, username: str) -> str:
    label = quote(f"{ISSUER}:{username}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}"
            f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}")


def qr_svg(text: str) -> str | None:
    """The setup QR code as an SVG, when the QR library is installed. Without
    it the key and the otpauth link are still shown, and every authenticator
    app takes a typed key."""
    try:
        import io
        import segno
    except ImportError:
        return None
    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="svg", scale=5, border=2,
                                     dark="#000", light="#fff", xmldecl=False,
                                     svgns=True, nl=False)
    return out.getvalue().decode()


# --- recovery codes ------------------------------------------------------------------

_RC_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"     # nothing that reads as another


def _rc_norm(code: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(code or "").lower())


def _rc_hash(code: str) -> str:
    return hmac.new(_key("recovery"), _rc_norm(code).encode(), hashlib.sha256).hexdigest()


def new_recovery_codes() -> tuple[list[str], list[str]]:
    plain = []
    while len(plain) < RECOVERY_COUNT:
        raw = "".join(secrets.choice(_RC_ALPHABET) for _ in range(10))
        if not raw.isdigit():           # all digits would read as a TOTP code
            plain.append(f"{raw[:5]}-{raw[5:]}")
    return plain, [_rc_hash(c) for c in plain]


def _looks_like_recovery(code: str) -> bool:
    return len(_rc_norm(code)) == 10 and not _rc_norm(code).isdigit()


# --- locking a row (SQLite has no row locks) -------------------------------------------

async def _locked_user(s, *, user_id: int | None = None,
                       username: str | None = None) -> User | None:
    """The user row, locked for this transaction. On Postgres a row lock; on
    SQLite a write that changes nothing, which takes the database's write
    lock first -- its driver opens the transaction at the first write, so a
    SELECT ... FOR UPDATE there locks nothing."""
    if user_id is None:
        try:
            name = auth.normalise_username(username or "")
        except AuthError:
            return None
        cond = User.username == name
    else:
        cond = User.id == user_id
    if db.dialect() == "sqlite":
        await s.execute(update(User).where(cond).values(failures=User.failures))
    return (await s.execute(select(User).where(cond).with_for_update())).scalar_one_or_none()


def _enabled(row: User) -> bool:
    return bool(row.totp_secret and row.totp_enabled_at)


# --- two-factor: the account's own settings ----------------------------------------------

async def status(username: str) -> dict[str, Any]:
    async with db.session() as s:
        row = await auth._row(s, username)
        if not row:
            raise AuthError("No such user.", 404)
        return {"enabled": _enabled(row),
                "pending": bool(row.totp_secret) and not row.totp_enabled_at,
                "enabled_at": db.epoch(row.totp_enabled_at),
                "recovery_left": len(row.recovery_codes or []) if _enabled(row) else 0}


async def begin_setup(username: str, password: str) -> dict[str, Any]:
    """A new secret, not yet in force: `enable` turns it on once a code from
    it has been typed back, so a half-scanned QR code locks nobody out."""
    async with db.session() as s:
        row = await _locked_user(s, username=username)
        if not row:
            raise AuthError("No such user.", 404)
        if not auth.verify_password(row.password_hash, password):
            raise AuthError("That is not your current password.", 403)
        if _enabled(row):
            raise AuthError("Two-factor sign-in is already on. Turn it off first to "
                            "move it to another device.", 409)
        secret = new_secret()
        row.totp_secret = _seal(secret)
        row.totp_enabled_at = None
        row.totp_last_step = None
        uri = otpauth_uri(secret, row.username)
    return {"secret": secret, "uri": uri, "qr_svg": qr_svg(uri),
            "digits": TOTP_DIGITS, "period": TOTP_STEP}


async def enable(username: str, code: str, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    async with db.session() as s:
        row = await _locked_user(s, username=username)
        if not row:
            raise AuthError("No such user.", 404)
        if _enabled(row):
            raise AuthError("Two-factor sign-in is already on.", 409)
        if not row.totp_secret:
            raise AuthError("Start the setup first.", 409)
        step = match_code(_open(row.totp_secret), code, now)
        if step is None:
            raise AuthError("That code does not match. Check the phone's clock and "
                            "type the newest code.", 400)
        plain, hashes = new_recovery_codes()
        row.totp_enabled_at = db.from_epoch(now)
        row.totp_last_step = step
        row.recovery_codes = hashes
    return {"enabled": True, "recovery_codes": plain}


async def _check_second(row: User, code: str, now: float) -> str:
    """'totp' or 'recovery' when `code` is right (and spends it), else ''."""
    if _looks_like_recovery(code):
        h = _rc_hash(code)
        left = list(row.recovery_codes or [])
        for i, stored in enumerate(left):
            if hmac.compare_digest(stored, h):
                del left[i]
                row.recovery_codes = left
                return "recovery"
        return ""
    step = match_code(_open(row.totp_secret), code, now, row.totp_last_step)
    if step is None:
        return ""
    row.totp_last_step = step
    return "totp"


async def disable(username: str, password: str, code: str,
                  now: float | None = None) -> None:
    now = time.time() if now is None else now
    async with db.session() as s:
        row = await _locked_user(s, username=username)
        if not row:
            raise AuthError("No such user.", 404)
        if not auth.verify_password(row.password_hash, password):
            raise AuthError("That is not your current password.", 403)
        if not _enabled(row):
            row.totp_secret = None          # an abandoned setup, if any
            return
        if not await _check_second(row, code, now):
            raise AuthError("That code does not match.", 400)
        _clear(row)


async def regenerate_recovery(username: str, password: str, code: str,
                              now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    async with db.session() as s:
        row = await _locked_user(s, username=username)
        if not row:
            raise AuthError("No such user.", 404)
        if not auth.verify_password(row.password_hash, password):
            raise AuthError("That is not your current password.", 403)
        if not _enabled(row):
            raise AuthError("Two-factor sign-in is not on.", 409)
        if not await _check_second(row, code, now):
            raise AuthError("That code does not match.", 400)
        plain, hashes = new_recovery_codes()
        row.recovery_codes = hashes
    return plain


def _clear(row: User) -> None:
    row.totp_secret = None
    row.totp_enabled_at = None
    row.totp_last_step = None
    row.recovery_codes = []


async def admin_reset(username: str) -> bool:
    """An admin turns somebody's 2FA off (lost phone, no codes left). Returns
    whether it was on. The caller ends their sessions."""
    async with db.session() as s:
        row = await _locked_user(s, username=username)
        if not row:
            raise AuthError("No such user.", 404)
        was = _enabled(row)
        _clear(row)
        return was


# --- two-factor: signing in -------------------------------------------------------------

def _challenge_mac(body: str, user: dict[str, Any]) -> str:
    pw = user.get("password") or {}
    salt = pw.get("salt", "") if isinstance(pw, dict) else ""
    material = f"{body}|{salt}|{user.get('totp_enabled_at') or 0}"
    return hmac.new(_key("2fa-challenge"), material.encode(), hashlib.sha256).hexdigest()[:40]


def make_challenge(user: dict[str, Any], now: float | None = None) -> tuple[str, float]:
    """What the password step hands back instead of a session."""
    expires = int((time.time() if now is None else now) + CHALLENGE_TTL)
    body = f"{user['id']}.{expires}.{secrets.token_urlsafe(9)}"
    return f"{body}.{_challenge_mac(body, user)}", float(expires)


async def finish_login(challenge: str, code: str,
                       now: float | None = None) -> tuple[dict[str, Any], str, int]:
    """Trade a challenge and a code for the account. Returns (user, how,
    recovery codes left). Raises AuthError (401) for anything wrong."""
    now = time.time() if now is None else now
    bad = AuthError("That sign-in has expired or is not valid. Start again.", 401)
    parts = str(challenge or "").split(".")
    if len(parts) != 4 or not parts[0].isdigit() or not parts[1].isdigit():
        raise bad
    body, mac = ".".join(parts[:3]), parts[3]
    if int(parts[1]) < now:
        raise bad
    failure: AuthError | None = None
    async with db.session() as s:
        row = await _locked_user(s, user_id=int(parts[0]))
        if not row or row.disabled or not _enabled(row):
            raise bad
        if not hmac.compare_digest(_challenge_mac(body, auth._as_dict(row)), mac):
            raise bad
        if (db.epoch(row.locked_until) or 0) > now:
            wait = int((db.epoch(row.locked_until) - now) / 60) + 1
            raise AuthError(f"Too many failed attempts. Try again in {wait} minute"
                            f"{'s' if wait != 1 else ''}, or ask an admin to unlock it.", 429)
        how = await _check_second(row, code, now)
        if not how:
            row.failures = int(row.failures or 0) + 1
            if row.failures >= auth.MAX_FAILURES:
                row.locked_until = db.from_epoch(now + auth.LOCKOUT_SECONDS)
                row.failures = 0
            failure = AuthError("That code is not right.", 401)
        else:
            row.failures = 0
            row.locked_until = None
            row.last_login = db.from_epoch(now)
        await s.flush()
        user = auth._as_dict(row)
        left = len(row.recovery_codes or [])
    if failure:
        raise failure          # after the commit, so the failure counts
    return user, how, left


# --- API tokens --------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _token_view(row: ApiToken, owner: str | None = None) -> dict[str, Any]:
    out = {"id": row.id, "name": row.name, "prefix": row.prefix,
           "permissions": list(row.permissions or []), "servers": list(row.servers or []),
           "created": db.epoch(row.created_at), "expires": db.epoch(row.expires_at),
           "last_used": db.epoch(row.last_used), "last_ip": row.last_ip or ""}
    if owner is not None:
        out["owner"] = owner
    return out


def grantable_for(user: dict[str, Any]) -> set[str]:
    """Every permission this account holds anywhere: its role's, plus any
    server's grant. A token can be given these and no others."""
    out = auth.effective_permissions(user)
    for perms in (user.get("grants") or {}).values():
        out |= {p for p in perms if p in auth.PERMISSIONS}
    return out


async def create_token(owner: dict[str, Any], *, name: str, permissions: Any,
                       servers: Any = None, expires_days: Any = None,
                       rack_of: Callable | None = None) -> dict[str, Any]:
    name = str(name or "").strip()[:60]
    if not name:
        raise AuthError("Give the token a name, so you know later what uses it.")
    if not isinstance(permissions, list) or not permissions:
        raise AuthError("Pick at least one permission for the token.")
    allowed = grantable_for(owner)
    perms = []
    for p in permissions:
        if p not in auth.PERMISSIONS:
            raise AuthError(f"Unknown permission {p!r}.")
        if p not in allowed:
            raise AuthError(f"A token cannot have more than you do: you do not have {p}.")
        if p not in perms:
            perms.append(p)
    sids: list[str] = []
    for sid in servers or []:
        sid = str(sid)
        if not auth.in_scope(owner, sid, rack_of):
            raise AuthError(f"Server {sid} is not one of yours.")
        if sid not in sids:
            sids.append(sid)
    expires = None
    if expires_days not in (None, "", 0, "0"):
        try:
            days = int(expires_days)
        except (TypeError, ValueError):
            raise AuthError("Expiry is a number of days.")
        if not 1 <= days <= MAX_TOKEN_DAYS:
            raise AuthError(f"Expiry is 1 to {MAX_TOKEN_DAYS} days, or none.")
        expires = db.from_epoch(time.time() + days * 86400)
    token = TOKEN_PREFIX + secrets.token_urlsafe(30)
    async with db.session() as s:
        count = (await s.execute(select(func.count(ApiToken.id))
                                 .where(ApiToken.user_id == owner["id"]))).scalar_one()
        if count >= MAX_TOKENS:
            raise AuthError(f"You have {MAX_TOKENS} tokens already. Revoke one first.", 409)
        row = ApiToken(user_id=owner["id"], name=name, prefix=token[:11],
                       token_sha256=_token_hash(token), permissions=perms, servers=sids,
                       expires_at=expires, last_ip="")
        s.add(row)
        await s.flush()
        view = _token_view(row)
    return {"token": token, "item": view}


async def list_tokens(user_id: int | None = None) -> list[dict[str, Any]]:
    """One account's tokens, or (no id) everybody's, with owners."""
    async with db.session() as s:
        q = select(ApiToken, User.username).join(User, User.id == ApiToken.user_id)
        if user_id is not None:
            q = q.where(ApiToken.user_id == user_id)
        rows = (await s.execute(q.order_by(ApiToken.created_at.desc(), ApiToken.id.desc()))).all()
        return [_token_view(t, None if user_id is not None else name) for t, name in rows]


async def revoke_token(token_id: int, user_id: int | None = None) -> bool:
    async with db.session() as s:
        q = delete(ApiToken).where(ApiToken.id == int(token_id))
        if user_id is not None:
            q = q.where(ApiToken.user_id == user_id)
        return bool((await s.execute(q)).rowcount)


async def token_user(token: str | None, ip: str = "") -> dict[str, Any] | None:
    """The account behind a bearer token, carrying the token's limits, or None."""
    if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 120:
        return None
    now = time.time()
    async with db.session() as s:
        found = (await s.execute(
            select(ApiToken, User).join(User, User.id == ApiToken.user_id)
            .where(ApiToken.token_sha256 == _token_hash(token)))).first()
        if not found:
            return None
        tok, row = found
        if tok.expires_at is not None and (db.epoch(tok.expires_at) or 0) <= now:
            return None
        if row.disabled:
            return None
        if now - (db.epoch(tok.last_used) or 0) > TOKEN_USED_EVERY:
            tok.last_used = db.from_epoch(now)
            tok.last_ip = str(ip or "")[:64]
        user = auth._as_dict(row)
        user["grants"] = await auth._grants_of(s, row.id)
        user["via_token"] = {"id": tok.id, "name": tok.name, "prefix": tok.prefix,
                             "permissions": list(tok.permissions or []),
                             "servers": list(tok.servers or [])}
        return user


# --- per-server grants ---------------------------------------------------------------

async def grants_of(username: str) -> list[dict[str, Any]]:
    async with db.session() as s:
        row = await auth._row(s, username)
        if not row:
            raise AuthError("No such user.", 404)
        rows = (await s.execute(
            select(ServerUser, Server.name).join(Server, Server.uuid == ServerUser.server_id)
            .where(ServerUser.user_id == row.id).order_by(Server.name))).all()
        return [{"server_id": g.server_id, "server_name": name,
                 "permissions": list(g.permissions or [])} for g, name in rows]


async def set_grant(username: str, server_id: str, permissions: Any) -> dict[str, Any] | None:
    """Give `username` these permissions on this one server (replacing what
    they had there). An empty list takes the grant away. Returns the grant,
    or None when it was removed."""
    if not isinstance(permissions, list):
        raise AuthError("permissions is a list.")
    perms = []
    for p in permissions:
        if p not in auth.PERMISSIONS:
            raise AuthError(f"Unknown permission {p!r}.")
        if p not in GRANTABLE:
            raise AuthError(f"{p} is not about one server, so it cannot be given on one.")
        if p not in perms:
            perms.append(p)
    if perms and "server.view" not in perms:
        perms.insert(0, "server.view")      # every other right needs to see it
    async with db.session() as s:
        row = await auth._row(s, username)
        if not row:
            raise AuthError("No such user.", 404)
        if await s.get(Server, server_id) is None:
            raise AuthError("No such server.", 404)
        have = await s.get(ServerUser, (server_id, row.id))
        if not perms:
            if have is not None:
                await s.delete(have)
            return None
        if have is None:
            s.add(ServerUser(server_id=server_id, user_id=row.id, permissions=perms))
        else:
            have.permissions = perms
    return {"server_id": server_id, "permissions": perms}


async def server_access(server_id: str, rack_of: Callable | None = None) -> list[dict[str, Any]]:
    """Everybody who can reach one server, how, and with what."""
    async with db.session() as s:
        users = (await s.execute(select(User).order_by(User.username))).scalars().all()
        grants = {g.user_id: list(g.permissions or []) for g in (await s.execute(
            select(ServerUser).where(ServerUser.server_id == server_id))).scalars().all()}
    out = []
    for row in users:
        user = auth._as_dict(row)
        if row.id in grants:
            user["grants"] = {server_id: grants[row.id]}
        if not auth.in_scope(user, server_id, rack_of):
            continue
        scope = user.get("scope") or {}
        if row.role == "admin":
            how = "admin"
        elif scope.get("all"):
            how = "every server"
        elif server_id in (scope.get("servers") or []):
            how = "scope"
        elif row.id in grants and not (rack_of and rack_of(server_id) in (scope.get("racks") or [])):
            how = "grant"
        else:
            how = "rack"
        out.append({"username": row.username, "display": row.display or row.username,
                    "role": row.role, "how": how, "disabled": bool(row.disabled),
                    "grant": grants.get(row.id, []),
                    "permissions": sorted(auth.effective_permissions(user, server_id))})
    return out
