"""Who is using this BlessForge, and what they are allowed to touch.

BlessForge shipped with no authentication at all: anyone who could reach
the port could delete a world. This is the accounts, sessions, permission
model and audit trail that fixed that.

The shape of it
---------------
  users       accounts, scrypt password records, permissions and scope
  sessions    live sessions, keyed by a HASH of the token
  audit_log   everything anyone did

All three are database tables (app/db/models.py). They used to be JSON
files under /data/state; app/db/importer.py carries an existing install's
files across on its first start with a database, and renames them
`*.imported` so the import cannot run twice.

This module is security code, so it keeps NO cache of accounts or sessions:
every request reads the session row and its user. A cache would make
"disable this account" mean "disable it in a while".

Passwords
---------
scrypt, which is memory-hard and therefore expensive to attack with a GPU
farm in a way PBKDF2 is not. n=2^14 costs about 60ms per verification on
the kind of box this runs on: slow enough to make an offline attack on a
stolen database painful, fast enough that nobody notices logging in. Every
password gets its own 16-byte salt, and the parameters are stored WITH the
hash so they can be raised later without invalidating existing accounts.

Verification is `hmac.compare_digest`, not `==`. A plain comparison
returns early on the first wrong byte, and the time it takes is a
measurement of how much of the hash you guessed.

Sessions
--------
A token is 32 bytes from `secrets.token_urlsafe`. The SERVER STORES ONLY
ITS SHA-256: a leaked sessions table is then a list of useless hashes
rather than a set of working keys, exactly as for passwords. Tokens expire
after 30 days and are checked against that on every request.

Two-factor sign-in, API tokens and per-server grants live in app/access.py;
the permission checks here take them into account.

What is deliberately NOT here (yet)
-----------------------------------
No self-service signup and no password reset by email: those arrive with
public accounts (WP-07), with email verification and rate limits in front of
them. Until then an admin sets passwords.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets
import time
import unicodedata
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from app import db
from app.db.models import AuditEntry, ServerUser, Session as SessionRow, User

# scrypt parameters. Stored alongside every hash so these can be raised
# without locking anyone out -- an old hash keeps verifying with the
# numbers it was made with, and is re-hashed on the next correct login.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

SESSION_DAYS = 30
SESSION_TTL = SESSION_DAYS * 24 * 3600
TOKEN_BYTES = 32

# Failed-login throttling. Per account, not per IP: an attacker behind a
# rotating proxy defeats an IP counter, and locking the ACCOUNT is what
# actually protects the account. Admins can clear it.
MAX_FAILURES = 8
LOCKOUT_SECONDS = 15 * 60

# How often a session's `last_seen` is written. It changes on every
# request, and a write per API call for a field nobody reads in real time
# is load for nothing.
LAST_SEEN_EVERY = 300

DEFAULT_ADMIN = "admin"
DEFAULT_PASSWORD = "password"


# --- permissions --------------------------------------------------------

# Named for the action, not for the screen: a permission that means "can
# see the Files tab" rots the moment the tab moves.
PERMISSIONS: dict[str, str] = {
    "server.view": "See the server, its status and its details",
    "server.power": "Start, stop and restart",
    "server.kill": "Force-kill a server that will not stop",
    "console.read": "Read the console",
    "console.command": "Send commands to the console",
    "files.read": "Browse and download files",
    "files.write": "Edit, upload, rename and delete files",
    "mods.manage": "Install, update, toggle and remove mods or plugins",
    "config.edit": "Edit server.properties and mod configs",
    "players.manage": "Ops, whitelist, bans and kicks",
    "backups.make": "Take backups",
    "backups.restore": "Restore a backup over the live server",
    "tune.apply": "Change memory, JVM flags and tuning",
    "schedules.manage": "Create, change and run scheduled tasks",
    "server.create": "Create new servers",
    "server.delete": "Delete servers and worlds",
    "roulette.use": "Use Mod Roulette",
    "racks.manage": "Make and rearrange racks",
}

# What a new account gets when nobody says otherwise: enough to look, not
# enough to break anything.
DEFAULT_PERMISSIONS = ["server.view", "console.read", "files.read"]

# Permissions that are not about one server, so scope does not apply.
GLOBAL_PERMISSIONS = {"server.create", "roulette.use", "racks.manage"}

# Roles are a shorthand over the permission set, not a parallel system.
ROLES: dict[str, str] = {
    "admin": "Full control, including accounts and the audit log",
    "operator": "Run and maintain the servers they are given",
    "member": "Look, and use the console",
    "guest": "Look only",
}
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "admin": list(PERMISSIONS),
    "operator": [
        "server.view", "server.power", "server.kill", "console.read",
        "console.command", "files.read", "files.write", "mods.manage",
        "config.edit", "players.manage", "backups.make", "tune.apply",
        "schedules.manage", "roulette.use",
    ],
    "member": ["server.view", "console.read", "console.command", "files.read"],
    "guest": ["server.view"],
}


class AuthError(Exception):
    """Something the user is allowed to be told."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- passwords ----------------------------------------------------------

def hash_password(password: str) -> dict[str, Any]:
    """scrypt, with its parameters recorded beside the hash."""
    if not isinstance(password, str) or len(password) < 1:
        raise AuthError("A password is required.")
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                         r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES)
    return {"algo": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": salt.hex(), "hash": key.hex()}


def verify_password(stored: Any, password: str) -> bool:
    """Constant-time check against a stored hash.

    Returns False for a malformed record rather than raising: a corrupt
    record must not become a way in, and must not become a 500 that tells
    an attacker the account exists.
    """
    if not isinstance(stored, dict) or stored.get("algo") != "scrypt":
        return False
    try:
        salt = bytes.fromhex(stored["salt"])
        expected = bytes.fromhex(stored["hash"])
        key = hashlib.scrypt(
            (password or "").encode("utf-8"), salt=salt,
            n=int(stored.get("n", SCRYPT_N)), r=int(stored.get("r", SCRYPT_R)),
            p=int(stored.get("p", SCRYPT_P)), dklen=len(expected))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(key, expected)


def password_problem(password: str) -> str | None:
    """Why this password will not do, or None.

    Deliberately short of a policy nobody can satisfy. Length is the only
    property that reliably buys anything; forced symbol classes mostly buy
    `Password1!`. The one specific rejection is the default, because
    leaving it in place is the whole risk the first-run account creates.
    """
    if not isinstance(password, str) or len(password) < 10:
        return "Use at least 10 characters."
    if password.lower() == DEFAULT_PASSWORD:
        return "That is the default password. Pick another one."
    if password.strip() != password:
        return "It cannot start or end with a space."
    return None


# --- users: the pure half ----------------------------------------------------

def normalise_username(name: str) -> str:
    base = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{2,32}", base):
        raise AuthError(
            "A username is 2-32 characters, letters, digits, dot, dash or "
            "underscore.")
    return base


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """A user as the front end may see it. Never the password record."""
    return {
        "username": user.get("username"),
        "display": user.get("display") or user.get("username"),
        "role": user.get("role", "member"),
        "permissions": sorted(effective_permissions(user)),
        "scope": user.get("scope") or {"all": False, "servers": [], "racks": []},
        "must_change_password": bool(user.get("must_change")),
        "disabled": bool(user.get("disabled")),
        "created_at": user.get("created_at"),
        "last_login": user.get("last_login"),
        "locked_until": user.get("locked_until") or 0,
        "is_admin": user.get("role") == "admin",
        "two_factor": bool(user.get("two_factor")),
        # Per-server grants (app/access.py), so the front end can show a
        # server's controls to somebody whose role alone would hide them.
        "server_permissions": {sid: sorted(effective_permissions(user, sid))
                               for sid in (user.get("grants") or {})},
        "via_token": ({k: user["via_token"][k] for k in ("name", "prefix")}
                      if user.get("via_token") else None),
    }


def effective_permissions(user: dict[str, Any], server_id: str | None = None) -> set[str]:
    """Role grants, plus anything named explicitly; on one server, plus what
    the account was granted on that server (app/access.py). Through an API
    token, only what the token was given as well.

    An admin is every permission by definition rather than by a list that
    can drift out of date as permissions are added.
    """
    if user.get("role") == "admin":
        granted = set(PERMISSIONS)
    else:
        granted = set(ROLE_PERMISSIONS.get(user.get("role", "member"), []))
        extra = user.get("permissions")
        if isinstance(extra, list):
            granted |= {p for p in extra if p in PERMISSIONS}
        denied = user.get("denied")
        if isinstance(denied, list):
            granted -= set(denied)
        if server_id is not None:
            granted |= {p for p in (user.get("grants") or {}).get(server_id, [])
                        if p in PERMISSIONS}
    token = user.get("via_token")
    if token is not None:
        granted &= set(token.get("permissions") or [])
    return granted


def may(user: dict[str, Any] | None, permission: str,
        server_id: str | None = None) -> bool:
    if not user or user.get("disabled"):
        return False
    return permission in effective_permissions(user, server_id)


def in_scope(user: dict[str, Any] | None, server_id: str | None,
             rack_of: Any = None) -> bool:
    """Whether this user may touch this server at all.

    Scope is separate from permission on purpose: "may restart things" and
    "may restart THAT thing" are different questions, and collapsing them
    means a permission list per server, which nobody will maintain.

    `rack_of` is a callable returning a server's rack id, so a scope given
    as a rack follows servers moved onto it without anybody re-granting.
    """
    if not user or user.get("disabled"):
        return False
    token = user.get("via_token")
    if token and token.get("servers") and server_id is not None \
            and server_id not in token["servers"]:
        return False                    # a token kept to some servers
    if user.get("role") == "admin":
        return True
    scope = user.get("scope") or {}
    if scope.get("all"):
        return True
    if server_id is None:
        return True                     # not a per-server action
    if server_id in (scope.get("servers") or []):
        return True
    if server_id in (user.get("grants") or {}):
        return True                     # granted rights on this one server
    racks = scope.get("racks") or []
    if racks and callable(rack_of):
        try:
            return rack_of(server_id) in racks
        except Exception:
            return False
    return False


def _clean_scope(scope: Any) -> dict[str, Any]:
    if not isinstance(scope, dict):
        return {"all": False, "servers": [], "racks": []}
    return {
        "all": bool(scope.get("all")),
        "servers": [str(s) for s in (scope.get("servers") or [])][:200],
        "racks": [str(r) for r in (scope.get("racks") or [])][:80],
    }


def _as_dict(row: User) -> dict[str, Any]:
    """A user row in the shape the rest of the app has always used."""
    return {
        "id": row.id,
        "username": row.username,
        "email": row.email,
        "display": row.display or row.username,
        "role": row.role,
        "password": row.password_hash,
        "permissions": list(row.permissions or []),
        "denied": list(row.denied or []),
        "scope": dict(row.scope or {}),
        "must_change": bool(row.must_change),
        "created_at": db.epoch(row.created_at),
        "last_login": db.epoch(row.last_login),
        "failures": int(row.failures or 0),
        "locked_until": db.epoch(row.locked_until) or 0,
        "disabled": bool(row.disabled),
        "two_factor": bool(row.totp_secret and row.totp_enabled_at),
        "totp_enabled_at": db.epoch(row.totp_enabled_at),
        "grants": {},
    }


# --- users: storage --------------------------------------------------------------

async def _row(s, username: str, *, lock: bool = False) -> User | None:
    try:
        want = normalise_username(username)
    except AuthError:
        return None
    q = select(User).where(User.username == want)
    if lock:
        q = q.with_for_update()
    return (await s.execute(q)).scalar_one_or_none()


async def find_user(username: str) -> dict[str, Any] | None:
    async with db.session() as s:
        row = await _row(s, username)
        return _as_dict(row) if row else None


async def create_user(*, username: str, password: str, role: str = "member",
                      display: str = "", permissions: list[str] | None = None,
                      scope: dict[str, Any] | None = None,
                      must_change: bool = True,
                      enforce_policy: bool = True) -> dict[str, Any]:
    """Make an account.

    `enforce_policy` exists for exactly one caller. `bootstrap` has to
    create an account whose password is `password`, which is precisely
    what `password_problem` refuses -- the policy and the first run want
    opposite things, and the honest way to say so is a named exemption
    rather than a policy with a hole in it. Everything else goes through
    the gate.
    """
    name = normalise_username(username)
    if role not in ROLES:
        raise AuthError(f"Unknown role {role!r}.")
    if enforce_policy:
        problem = password_problem(password)
        if problem:
            raise AuthError(problem)
    async with db.session() as s:
        if await _row(s, name):
            raise AuthError(f"There is already a user called {name}.", 409)
        row = User(
            username=name,
            display=str(display or "").strip()[:60] or name,
            role=role,
            password_hash=hash_password(password),
            permissions=[p for p in (permissions or DEFAULT_PERMISSIONS)
                         if p in PERMISSIONS],
            denied=[],
            scope=_clean_scope(scope),
            must_change=bool(must_change),
        )
        s.add(row)
        await s.flush()
        return _as_dict(row)


async def _guard_last_admin(s, row: User) -> None:
    """Refuse to remove the last way back in.

    Locking every admin out of a self-hosted panel is unrecoverable from
    the UI by design. The admin rows are locked while they are counted, so
    two admins demoting each other at the same moment cannot both win.
    """
    admins = (await s.execute(
        select(User).where(User.role == "admin", User.disabled.is_(False))
        .order_by(User.id).with_for_update())).scalars().all()
    if len(admins) <= 1 and any(a.id == row.id for a in admins):
        raise AuthError(
            "That is the only admin left. Make somebody else an admin first, "
            "or you will lock yourself out with no way back in from here.", 409)


async def _lock_admins(s) -> None:
    """Lock every admin row, in id order, before touching any one of them.

    A change that could remove an admin has to count the admins with their
    rows locked. Locking the target first and the set second lets two such
    changes each hold one row and wait on the other -- a deadlock, which
    Postgres resolves by failing one of them with a 500. Taking the whole
    set first, always in the same order, makes them queue instead.

    SQLite has no row locks, and its driver only opens a transaction at the
    first write, so there the SELECT ... FOR UPDATE locks nothing and two
    demotions both counted two admins. A write that changes nothing takes
    the database's write lock first; the second change waits for the first
    to commit, then counts what it left.
    """
    if db.dialect() == "sqlite":
        await s.execute(update(User).where(User.role == "admin").values(role=User.role))
    await s.execute(select(User.id).where(User.role == "admin")
                    .order_by(User.id).with_for_update())


async def update_user(username: str, **changes: Any) -> dict[str, Any]:
    async with db.session() as s:
        if changes.get("role") is not None or changes.get("disabled"):
            await _lock_admins(s)
        row = await _row(s, username, lock=True)
        if not row:
            raise AuthError("No such user.", 404)
        if "role" in changes and changes["role"] is not None:
            if changes["role"] not in ROLES:
                raise AuthError(f"Unknown role {changes['role']!r}.")
            if row.role == "admin" and changes["role"] != "admin":
                await _guard_last_admin(s, row)
            row.role = changes["role"]
        if changes.get("display") is not None:
            row.display = str(changes["display"]).strip()[:60] or row.username
        if changes.get("permissions") is not None:
            row.permissions = [p for p in changes["permissions"] if p in PERMISSIONS]
        if changes.get("scope") is not None:
            row.scope = _clean_scope(changes["scope"])
        if changes.get("disabled") is not None:
            if changes["disabled"] and row.role == "admin":
                await _guard_last_admin(s, row)
            row.disabled = bool(changes["disabled"])
            if row.disabled:
                await s.execute(delete(SessionRow).where(SessionRow.user_id == row.id))
        if changes.get("unlock"):
            row.failures = 0
            row.locked_until = None
        await s.flush()
        return _as_dict(row)


async def set_password(username: str, password: str, *,
                       must_change: bool = False) -> dict[str, Any]:
    problem = password_problem(password)
    if problem:
        raise AuthError(problem)
    async with db.session() as s:
        row = await _row(s, username, lock=True)
        if not row:
            raise AuthError("No such user.", 404)
        row.password_hash = hash_password(password)
        row.must_change = bool(must_change)
        row.failures = 0
        row.locked_until = None
        await s.flush()
        return _as_dict(row)


async def delete_user(username: str) -> None:
    async with db.session() as s:
        await _lock_admins(s)
        row = await _row(s, username, lock=True)
        if not row:
            raise AuthError("No such user.", 404)
        if row.role == "admin":
            await _guard_last_admin(s, row)
        await s.delete(row)             # sessions go with it (ON DELETE CASCADE)


async def list_users() -> list[dict[str, Any]]:
    async with db.session() as s:
        rows = (await s.execute(select(User).order_by(User.username))).scalars().all()
        return [public_user(_as_dict(r)) for r in rows]


# --- sessions -----------------------------------------------------------

def _token_key(token: str) -> str:
    """Sessions are stored under the token's hash, never the token.

    Same argument as for passwords: a leaked sessions table should be a
    list of useless strings, not a drawer of working keys.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def start_session(user: dict[str, Any], *, agent: str = "",
                        ip: str = "") -> tuple[str, float]:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires = time.time() + SESSION_TTL
    async with db.session() as s:
        s.add(SessionRow(token_sha256=_token_key(token), user_id=user["id"],
                         expires_at=db.from_epoch(expires),
                         user_agent=str(agent or "")[:200],
                         ip=str(ip or "")[:64]))
    return token, expires


async def session_user(token: str | None) -> dict[str, Any] | None:
    """The account behind a token, or None. Expired sessions are dropped."""
    if not token:
        return None
    key = _token_key(token)
    async with db.session() as s:
        found = (await s.execute(
            select(SessionRow, User).join(User, User.id == SessionRow.user_id)
            .where(SessionRow.token_sha256 == key))).first()
        if not found:
            return None
        rec, user = found
        now = time.time()
        if (db.epoch(rec.expires_at) or 0) <= now:
            await s.delete(rec)
            return None
        if user.disabled:
            return None
        if now - (db.epoch(rec.last_seen) or 0) > LAST_SEEN_EVERY:
            rec.last_seen = _now()
        out = _as_dict(user)
        out["grants"] = await _grants_of(s, user.id)
        return out


async def _grants_of(s, user_id: int) -> dict[str, list[str]]:
    """Per-server grants (server_users), read with the session like the
    rest of the account: no cache, so taking one away takes effect at once."""
    rows = (await s.execute(select(ServerUser.server_id, ServerUser.permissions)
                            .where(ServerUser.user_id == user_id))).all()
    return {sid: list(perms or []) for sid, perms in rows}


async def revoke(token: str) -> None:
    async with db.session() as s:
        await s.execute(delete(SessionRow)
                        .where(SessionRow.token_sha256 == _token_key(token)))


async def revoke_all(username: str) -> int:
    """Drop every session for one account. Used on disable and on reset."""
    async with db.session() as s:
        row = await _row(s, username)
        if not row:
            return 0
        result = await s.execute(delete(SessionRow)
                                 .where(SessionRow.user_id == row.id))
        return int(result.rowcount or 0)


async def sessions_for(username: str) -> list[dict[str, Any]]:
    async with db.session() as s:
        row = await _row(s, username)
        if not row:
            return []
        rows = (await s.execute(select(SessionRow)
                                .where(SessionRow.user_id == row.id))).scalars().all()
        return [{"created": db.epoch(r.created_at), "expires": db.epoch(r.expires_at),
                 "agent": r.user_agent, "ip": r.ip,
                 "last_seen": db.epoch(r.last_seen)} for r in rows]


async def session_counts() -> dict[str, int]:
    """Live sessions per username, in one query (the admin register)."""
    async with db.session() as s:
        rows = (await s.execute(
            select(User.username, func.count(SessionRow.token_sha256))
            .join(SessionRow, SessionRow.user_id == User.id)
            .group_by(User.username))).all()
        return {name: int(n) for name, n in rows}


# --- logging in ---------------------------------------------------------

async def authenticate(username: str, password: str) -> dict[str, Any]:
    """Check a username and password, with throttling.

    The failure message is the same whether the account does not exist or
    the password is wrong. Saying which would turn the login form into a
    tool for discovering usernames.
    """
    now = time.time()
    async with db.session() as s:
        row = await _row(s, username, lock=True)
        if row and (db.epoch(row.locked_until) or 0) > now:
            wait = int((db.epoch(row.locked_until) - now) / 60) + 1
            raise AuthError(
                f"Too many failed attempts. Try again in {wait} minute"
                f"{'s' if wait != 1 else ''}, or ask an admin to unlock it.", 429)

        ok = bool(row) and not row.disabled \
            and verify_password(row.password_hash, password)
        if not ok:
            if row:
                row.failures = int(row.failures or 0) + 1
                if row.failures >= MAX_FAILURES:
                    row.locked_until = db.from_epoch(now + LOCKOUT_SECONDS)
                    row.failures = 0
            else:
                # Spend roughly the same time as a real verification would,
                # so a fast rejection cannot be read as "no such user".
                hashlib.scrypt(b"decoy", salt=b"decoy-salt-16byt", n=SCRYPT_N,
                               r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES)
            failure = AuthError("That username and password do not match.", 401)
        elif row.totp_secret and row.totp_enabled_at:
            # The password was right but the sign-in is not done: the code
            # step (app/access.py) clears the count. Clearing it here would
            # let "password, seven wrong codes, password, ..." run for ever.
            failure = None
        else:
            row.failures = 0
            row.locked_until = None
            row.last_login = db.from_epoch(now)
            failure = None
        await s.flush()
        result = _as_dict(row) if row else None
    # Raised after the session commits, so a failure still counts.
    if failure:
        raise failure
    return result


# --- first run ----------------------------------------------------------

async def bootstrap() -> dict[str, Any]:
    """Make sure there is a way in, exactly once.

    An install with no accounts has no credentials to hand anyone. So the
    first start creates `admin` / `password` and marks it must-change --
    the next screen after that first login is a password change that cannot
    be skipped.

    It is a real risk for as long as it lasts, which is why it is reported
    loudly by `/api/auth/session` and why the default password is the one
    value `password_problem` refuses to accept as a replacement.
    """
    if not await needs_bootstrap():
        async with db.session() as s:
            count = (await s.execute(select(func.count(User.id)))).scalar_one()
        return {"created": False, "users": int(count)}
    try:
        await create_user(username=DEFAULT_ADMIN, password=DEFAULT_PASSWORD,
                          role="admin", display="Administrator", must_change=True,
                          enforce_policy=False)
    except AuthError as e:
        if e.status == 409:             # a concurrent first request got there
            return {"created": False, "users": 1}
        raise
    await audit(actor="system", action="auth.bootstrap",
                detail="created the first admin account")
    return {"created": True, "username": DEFAULT_ADMIN,
            "password": DEFAULT_PASSWORD}


async def needs_bootstrap() -> bool:
    async with db.session() as s:
        return (await s.execute(select(func.count(User.id)))).scalar_one() == 0


async def using_default_password() -> bool:
    """True while any admin still has the shipped password."""
    async with db.session() as s:
        rows = (await s.execute(select(User).where(
            User.role == "admin", User.must_change.is_(True)))).scalars().all()
    return any(verify_password(r.password_hash, DEFAULT_PASSWORD) for r in rows)


# --- audit --------------------------------------------------------------

async def audit(*, actor: str, action: str, server_id: str = "", detail: str = "",
                ip: str = "", ok: bool = True, ticket_id: str | None = None,
                before: dict | None = None, after: dict | None = None) -> None:
    """Record one action. Never raises.

    Nothing in the app should fail because the audit log could not be
    written, but everything interesting should try. `before`/`after` carry
    the values an admin write changed.
    """
    try:
        async with db.session() as s:
            s.add(AuditEntry(
                actor=(actor or "?")[:64], action=(action or "?")[:64],
                target=(server_id or "")[:64], detail=(detail or "")[:2000],
                ip=(ip or "")[:64], ok=bool(ok), ticket_id=ticket_id,
                before=before, after=after))
    except Exception:       # noqa: BLE001 -- the audit must never break an action
        return


def _entry(r: AuditEntry) -> dict[str, Any]:
    out = {"at": round(db.epoch(r.at) or 0, 3), "actor": r.actor,
           "action": r.action, "server_id": r.target, "detail": r.detail,
           "ip": r.ip, "ok": bool(r.ok)}
    if r.ticket_id:
        out["ticket_id"] = r.ticket_id
    if r.before is not None or r.after is not None:
        out["before"], out["after"] = r.before, r.after
    return out


async def audit_entries(*, limit: int = 200, actor: str = "", action: str = "",
                        server_id: str = "", since: float = 0.0,
                        query: str = "") -> list[dict[str, Any]]:
    """Newest first, filtered in the database."""
    q = select(AuditEntry)
    if actor:
        q = q.where(AuditEntry.actor == actor)
    if action:
        q = q.where(AuditEntry.action.startswith(action, autoescape=True))
    if server_id:
        q = q.where(AuditEntry.target == server_id)
    if since:
        q = q.where(AuditEntry.at >= db.from_epoch(since))
    if query:
        needle = f"%{query.lower()}%"
        q = q.where(or_(*(func.lower(col).like(needle) for col in (
            AuditEntry.actor, AuditEntry.action, AuditEntry.target,
            AuditEntry.detail, AuditEntry.ip))))
    q = q.order_by(AuditEntry.at.desc(), AuditEntry.id.desc()).limit(
        max(1, min(int(limit or 200), 2000)))
    async with db.session() as s:
        return [_entry(r) for r in (await s.execute(q)).scalars().all()]


async def audit_actions() -> list[str]:
    """Every action name that actually appears, for the filter dropdown."""
    async with db.session() as s:
        rows = (await s.execute(select(AuditEntry.action).distinct())).scalars().all()
    return sorted(a for a in rows if a)
