"""Who is using this BlessForge, and what they are allowed to touch.

BlessForge shipped with no authentication at all: anyone who could reach
the port could delete a world. This adds accounts, sessions, a permission
model and an audit trail, without adding a database or a dependency --
`hashlib.scrypt` and `secrets` are enough to do it properly.

The shape of it
---------------
  users.json     accounts, hashed passwords, permissions and scope
  sessions.json  live sessions, keyed by a HASH of the token
  audit.jsonl    append-only record of everything anyone did

All three go through `config.write_state`, so a power cut cannot leave a
half-written account file (see the note there; it has happened).

Passwords
---------
scrypt, which is memory-hard and therefore expensive to attack with a GPU
farm in a way PBKDF2 is not. n=2^14 costs about 60ms per verification on
the kind of box this runs on: slow enough to make an offline attack on a
stolen file painful, fast enough that nobody notices logging in. Every
password gets its own 16-byte salt, and the parameters are stored WITH the
hash so they can be raised later without invalidating existing accounts.

Verification is `hmac.compare_digest`, not `==`. A plain comparison
returns early on the first wrong byte, and the time it takes is a
measurement of how much of the hash you guessed.

Sessions
--------
A token is 32 bytes from `secrets.token_urlsafe`. The SERVER STORES ONLY
ITS SHA-256: a leaked sessions.json is then a list of useless hashes
rather than a set of working keys, exactly as for passwords. Tokens expire
after 30 days and are checked against that on every request.

What is deliberately NOT here
-----------------------------
No self-service signup and no password reset by email. Both are ways in
for someone who is not supposed to be here, and this is a control panel
for a handful of people who know each other. An admin sets passwords. If
the last admin is locked out, `dev/tools/reset_admin.py` is the answer,
and it requires filesystem access to the box -- which is the correct level
of proof for that operation.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import unicodedata
from typing import Any

from .config import read_state, state_path, write_state

USERS_FILE = "users.json"
SESSIONS_FILE = "sessions.json"
AUDIT_FILE = "audit.jsonl"

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

AUDIT_MAX = 5000            # lines kept; the file is trimmed past this

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
        "roulette.use",
    ],
    "member": ["server.view", "console.read", "console.command", "files.read"],
    "guest": ["server.view"],
}


class AuthError(Exception):
    """Something the user is allowed to be told."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- storage ------------------------------------------------------------

_users: dict[str, Any] | None = None
_sessions: dict[str, Any] | None = None


def _blank_users() -> dict[str, Any]:
    return {"version": 1, "users": [], "created_at": time.time()}


def _load_users() -> dict[str, Any]:
    global _users
    if _users is not None:
        return _users
    text = read_state(state_path(USERS_FILE))
    try:
        raw = json.loads(text) if text else None
    except ValueError:
        raw = None
    if not isinstance(raw, dict) or not isinstance(raw.get("users"), list):
        _users = _blank_users()
    else:
        _users = {"version": 1, "users": [u for u in raw["users"]
                                          if isinstance(u, dict) and u.get("username")],
                  "created_at": raw.get("created_at") or time.time()}
    return _users


def _save_users() -> bool:
    return write_state(state_path(USERS_FILE),
                       json.dumps(_load_users(), indent=1))


def _load_sessions() -> dict[str, Any]:
    global _sessions
    if _sessions is not None:
        return _sessions
    text = read_state(state_path(SESSIONS_FILE))
    try:
        raw = json.loads(text) if text else None
    except ValueError:
        raw = None
    live = {}
    now = time.time()
    if isinstance(raw, dict) and isinstance(raw.get("sessions"), dict):
        for key, rec in raw["sessions"].items():
            if isinstance(rec, dict) and (rec.get("expires") or 0) > now:
                live[str(key)] = rec
    _sessions = {"version": 1, "sessions": live}
    return _sessions


def _save_sessions() -> bool:
    return write_state(state_path(SESSIONS_FILE),
                       json.dumps(_load_sessions(), indent=1))


def reset_for_tests() -> None:
    global _users, _sessions
    _users = None
    _sessions = None


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
    line in users.json must not become a way in, and must not become a
    500 that tells an attacker the account exists.
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
    leaving it in place is the whole risk this migration creates.
    """
    if not isinstance(password, str) or len(password) < 10:
        return "Use at least 10 characters."
    if password.lower() == DEFAULT_PASSWORD:
        return "That is the default password. Pick another one."
    if password.strip() != password:
        return "It cannot start or end with a space."
    return None


# --- users --------------------------------------------------------------

def normalise_username(name: str) -> str:
    base = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{2,32}", base):
        raise AuthError(
            "A username is 2-32 characters, letters, digits, dot, dash or "
            "underscore.")
    return base


def find_user(username: str) -> dict[str, Any] | None:
    try:
        want = normalise_username(username)
    except AuthError:
        return None
    for user in _load_users()["users"]:
        if user.get("username") == want:
            return user
    return None


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
    }


def effective_permissions(user: dict[str, Any]) -> set[str]:
    """Role grants, plus anything named explicitly.

    An admin is every permission by definition rather than by a list that
    can drift out of date as permissions are added.
    """
    if user.get("role") == "admin":
        return set(PERMISSIONS)
    granted = set(ROLE_PERMISSIONS.get(user.get("role", "member"), []))
    extra = user.get("permissions")
    if isinstance(extra, list):
        granted |= {p for p in extra if p in PERMISSIONS}
    denied = user.get("denied")
    if isinstance(denied, list):
        granted -= set(denied)
    return granted


def may(user: dict[str, Any] | None, permission: str) -> bool:
    if not user or user.get("disabled"):
        return False
    return permission in effective_permissions(user)


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
    if user.get("role") == "admin":
        return True
    scope = user.get("scope") or {}
    if scope.get("all"):
        return True
    if server_id is None:
        return True                     # not a per-server action
    if server_id in (scope.get("servers") or []):
        return True
    racks = scope.get("racks") or []
    if racks and callable(rack_of):
        try:
            return rack_of(server_id) in racks
        except Exception:
            return False
    return False


def create_user(*, username: str, password: str, role: str = "member",
                display: str = "", permissions: list[str] | None = None,
                scope: dict[str, Any] | None = None,
                must_change: bool = True,
                enforce_policy: bool = True) -> dict[str, Any]:
    """Make an account.

    `enforce_policy` exists for exactly one caller. `bootstrap` has to
    create an account whose password is `password`, which is precisely
    what `password_problem` refuses -- the policy and the migration want
    opposite things, and the honest way to say so is a named exemption
    rather than a policy with a hole in it. Everything else goes through
    the gate.
    """
    name = normalise_username(username)
    if find_user(name):
        raise AuthError(f"There is already a user called {name}.", 409)
    if role not in ROLES:
        raise AuthError(f"Unknown role {role!r}.")
    if enforce_policy:
        problem = password_problem(password)
        if problem:
            raise AuthError(problem)
    user = {
        "username": name,
        "display": str(display or "").strip()[:60] or name,
        "role": role,
        "password": hash_password(password),
        "permissions": [p for p in (permissions or DEFAULT_PERMISSIONS)
                        if p in PERMISSIONS],
        "scope": _clean_scope(scope),
        "must_change": bool(must_change),
        "created_at": time.time(),
        "last_login": None,
        "failures": 0,
        "locked_until": 0,
        "disabled": False,
    }
    _load_users()["users"].append(user)
    _save_users()
    return user


def _clean_scope(scope: Any) -> dict[str, Any]:
    if not isinstance(scope, dict):
        return {"all": False, "servers": [], "racks": []}
    return {
        "all": bool(scope.get("all")),
        "servers": [str(s) for s in (scope.get("servers") or [])][:200],
        "racks": [str(r) for r in (scope.get("racks") or [])][:80],
    }


def update_user(username: str, **changes: Any) -> dict[str, Any]:
    user = find_user(username)
    if not user:
        raise AuthError("No such user.", 404)
    if "role" in changes and changes["role"] is not None:
        if changes["role"] not in ROLES:
            raise AuthError(f"Unknown role {changes['role']!r}.")
        if user.get("role") == "admin" and changes["role"] != "admin":
            _guard_last_admin(user)
        user["role"] = changes["role"]
    if changes.get("display") is not None:
        user["display"] = str(changes["display"]).strip()[:60] or user["username"]
    if changes.get("permissions") is not None:
        user["permissions"] = [p for p in changes["permissions"] if p in PERMISSIONS]
    if changes.get("scope") is not None:
        user["scope"] = _clean_scope(changes["scope"])
    if changes.get("disabled") is not None:
        if changes["disabled"] and user.get("role") == "admin":
            _guard_last_admin(user)
        user["disabled"] = bool(changes["disabled"])
        if user["disabled"]:
            revoke_all(user["username"])
    if changes.get("unlock"):
        user["failures"] = 0
        user["locked_until"] = 0
    _save_users()
    return user


def _guard_last_admin(user: dict[str, Any]) -> None:
    """Refuse to remove the last way back in.

    Locking every admin out of a self-hosted panel is unrecoverable from
    the UI by design -- there is no password reset email to fall back on.
    """
    admins = [u for u in _load_users()["users"]
              if u.get("role") == "admin" and not u.get("disabled")]
    if len(admins) <= 1 and any(a.get("username") == user.get("username")
                                for a in admins):
        raise AuthError(
            "That is the only admin left. Make somebody else an admin first, "
            "or you will lock yourself out with no way back in from here.", 409)


def set_password(username: str, password: str, *,
                 must_change: bool = False) -> dict[str, Any]:
    user = find_user(username)
    if not user:
        raise AuthError("No such user.", 404)
    problem = password_problem(password)
    if problem:
        raise AuthError(problem)
    user["password"] = hash_password(password)
    user["must_change"] = bool(must_change)
    user["failures"] = 0
    user["locked_until"] = 0
    _save_users()
    return user


def delete_user(username: str) -> None:
    user = find_user(username)
    if not user:
        raise AuthError("No such user.", 404)
    if user.get("role") == "admin":
        _guard_last_admin(user)
    store = _load_users()
    store["users"] = [u for u in store["users"]
                      if u.get("username") != user["username"]]
    revoke_all(user["username"])
    _save_users()


def list_users() -> list[dict[str, Any]]:
    return [public_user(u) for u in
            sorted(_load_users()["users"], key=lambda u: u.get("username", ""))]


# --- sessions -----------------------------------------------------------

def _token_key(token: str) -> str:
    """Sessions are stored under the token's hash, never the token.

    Same argument as for passwords: a leaked sessions.json should be a
    list of useless strings, not a drawer of working keys.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def start_session(user: dict[str, Any], *, agent: str = "",
                  ip: str = "") -> tuple[str, float]:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires = time.time() + SESSION_TTL
    _load_sessions()["sessions"][_token_key(token)] = {
        "username": user["username"],
        "created": time.time(),
        "expires": expires,
        "agent": str(agent or "")[:180],
        "ip": str(ip or "")[:60],
        "last_seen": time.time(),
    }
    _save_sessions()
    return token, expires


def session_user(token: str | None) -> dict[str, Any] | None:
    """The account behind a token, or None. Expired tokens are dropped."""
    if not token:
        return None
    store = _load_sessions()
    rec = store["sessions"].get(_token_key(token))
    if not rec:
        return None
    if (rec.get("expires") or 0) <= time.time():
        store["sessions"].pop(_token_key(token), None)
        _save_sessions()
        return None
    user = find_user(rec.get("username", ""))
    if not user or user.get("disabled"):
        return None
    # `last_seen` is only persisted occasionally: it changes on every
    # request, and writing users' session file on every request would be
    # a disk write per API call for a field nobody reads in real time.
    if time.time() - (rec.get("last_seen") or 0) > 300:
        rec["last_seen"] = time.time()
        _save_sessions()
    return user


def revoke(token: str) -> None:
    store = _load_sessions()
    if store["sessions"].pop(_token_key(token), None) is not None:
        _save_sessions()


def revoke_all(username: str) -> int:
    """Drop every session for one account. Used on disable and on reset."""
    store = _load_sessions()
    gone = [k for k, v in store["sessions"].items()
            if v.get("username") == username]
    for key in gone:
        store["sessions"].pop(key, None)
    if gone:
        _save_sessions()
    return len(gone)


def sessions_for(username: str) -> list[dict[str, Any]]:
    return [{"created": v.get("created"), "expires": v.get("expires"),
             "agent": v.get("agent"), "ip": v.get("ip"),
             "last_seen": v.get("last_seen")}
            for v in _load_sessions()["sessions"].values()
            if v.get("username") == username]


# --- logging in ---------------------------------------------------------

def authenticate(username: str, password: str) -> dict[str, Any]:
    """Check a username and password, with throttling.

    The failure message is the same whether the account does not exist or
    the password is wrong. Saying which would turn the login form into a
    tool for discovering usernames.
    """
    user = find_user(username)
    now = time.time()

    if user and (user.get("locked_until") or 0) > now:
        wait = int((user["locked_until"] - now) / 60) + 1
        raise AuthError(
            f"Too many failed attempts. Try again in {wait} minute"
            f"{'s' if wait != 1 else ''}, or ask an admin to unlock it.", 429)

    ok = bool(user) and not user.get("disabled") \
        and verify_password(user.get("password"), password)

    if not ok:
        if user:
            user["failures"] = int(user.get("failures") or 0) + 1
            if user["failures"] >= MAX_FAILURES:
                user["locked_until"] = now + LOCKOUT_SECONDS
                user["failures"] = 0
            _save_users()
        else:
            # Spend roughly the same time as a real verification would, so
            # a fast rejection cannot be read as "no such user".
            hashlib.scrypt(b"decoy", salt=b"decoy-salt-16byt", n=SCRYPT_N,
                           r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES)
        raise AuthError("That username and password do not match.", 401)

    user["failures"] = 0
    user["locked_until"] = 0
    user["last_login"] = now
    _save_users()
    return user


# --- first run ----------------------------------------------------------

def bootstrap() -> dict[str, Any]:
    """Make sure there is a way in, exactly once.

    An install that predates accounts has no users.json, and the person
    upgrading has no credentials to be given. So the first start creates
    `admin` / `password` and marks it must-change -- the next screen after
    that first login is a password change that cannot be skipped.

    It is a real risk for as long as it lasts, which is why it is reported
    loudly by `/api/auth/session` and why the default password is the one
    value `password_problem` refuses to accept as a replacement.
    """
    store = _load_users()
    if store["users"]:
        return {"created": False, "users": len(store["users"])}
    create_user(username=DEFAULT_ADMIN, password=DEFAULT_PASSWORD,
                role="admin", display="Administrator", must_change=True,
                enforce_policy=False)
    audit(actor="system", action="auth.bootstrap",
          detail="created the first admin account")
    return {"created": True, "username": DEFAULT_ADMIN,
            "password": DEFAULT_PASSWORD}


def needs_bootstrap() -> bool:
    return not _load_users()["users"]


def using_default_password() -> bool:
    """True while any admin still has the shipped password."""
    for user in _load_users()["users"]:
        if user.get("role") == "admin" and user.get("must_change") \
                and verify_password(user.get("password"), DEFAULT_PASSWORD):
            return True
    return False


# --- audit --------------------------------------------------------------

def audit(*, actor: str, action: str, server_id: str = "", detail: str = "",
          ip: str = "", ok: bool = True) -> None:
    """Append one line to the record. Never raises.

    JSONL rather than a JSON array so an append is an append -- a crash
    mid-write costs the last line, not the file. Nothing in the app should
    fail because the audit log could not be written, but everything
    interesting should try.
    """
    line = json.dumps({
        "at": round(time.time(), 3), "actor": actor or "?", "action": action,
        "server_id": server_id or "", "detail": (detail or "")[:400],
        "ip": (ip or "")[:60], "ok": bool(ok),
    }, separators=(",", ":"))
    path = state_path(AUDIT_FILE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return
    _trim_audit(path)


_audit_writes = 0


def _trim_audit(path) -> None:
    """Keep the file bounded, checked occasionally rather than every write."""
    global _audit_writes
    _audit_writes += 1
    if _audit_writes % 200:
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= AUDIT_MAX:
        return
    write_state(path, "\n".join(lines[-AUDIT_MAX:]) + "\n")


def audit_entries(*, limit: int = 200, actor: str = "", action: str = "",
                  server_id: str = "", since: float = 0.0,
                  query: str = "") -> list[dict[str, Any]]:
    """Newest first, filtered. Reads the tail rather than the whole file."""
    path = state_path(AUDIT_FILE)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    needle = (query or "").lower()
    for line in reversed(raw):
        if len(out) >= max(1, min(limit, 2000)):
            break
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if actor and rec.get("actor") != actor:
            continue
        if action and not str(rec.get("action", "")).startswith(action):
            continue
        if server_id and rec.get("server_id") != server_id:
            continue
        if since and (rec.get("at") or 0) < since:
            continue
        if needle and needle not in json.dumps(rec).lower():
            continue
        out.append(rec)
    return out


def audit_actions() -> list[str]:
    """Every action name that actually appears, for the filter dropdown."""
    path = state_path(AUDIT_FILE)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    seen = set()
    for line in raw[-AUDIT_MAX:]:
        try:
            seen.add(json.loads(line).get("action", ""))
        except ValueError:
            continue
    return sorted(a for a in seen if a)
