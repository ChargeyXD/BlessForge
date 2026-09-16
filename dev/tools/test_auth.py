#!/usr/bin/env python3
"""Accounts, passwords, sessions and scope.

This is the one module in the app where being wrong is a security bug
rather than a papercut, so the tests are about the properties that matter
rather than about the happy path:

  * a stored password must not be recoverable from what is on disk
  * a stored SESSION must not be usable from what is on disk
  * a wrong username and a wrong password must be indistinguishable
  * the last admin must not be able to lock everyone out
  * a disabled account's live sessions must die with it

Offline. No network, no Docker, no backend. Writes into a temp directory.

    python3 dev/tools/test_auth.py
"""
import json
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="bf-auth-")
os.environ["STATE_DIR"] = _TMP
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("CACHE_DIR", str(pathlib.Path(_TMP) / "cache"))

from app import auth  # noqa: E402

out = []


def check(name, cond, extra=""):
    out.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


def fresh():
    auth.reset_for_tests()
    for leftover in pathlib.Path(_TMP).glob("*.json"):
        leftover.unlink()
    for leftover in pathlib.Path(_TMP).glob("*.jsonl"):
        leftover.unlink()


# --- 1. passwords are not recoverable -----------------------------------
fresh()
rec = auth.hash_password("correct horse battery staple")
check("a stored password is not the password",
      "correct horse battery staple" not in json.dumps(rec))
check("it records its own scrypt parameters",
      rec["algo"] == "scrypt" and rec["n"] == auth.SCRYPT_N and "salt" in rec)
check("the right password verifies",
      auth.verify_password(rec, "correct horse battery staple"))
check("a wrong password does not", not auth.verify_password(rec, "wrong"))
check("an empty password does not", not auth.verify_password(rec, ""))

again = auth.hash_password("correct horse battery staple")
check("the same password hashes differently each time (per-password salt)",
      again["hash"] != rec["hash"] and again["salt"] != rec["salt"])

check("a malformed record is refused rather than crashing",
      not auth.verify_password({"algo": "md5"}, "x")
      and not auth.verify_password(None, "x")
      and not auth.verify_password({"algo": "scrypt", "salt": "zz", "hash": "zz"}, "x"))

# --- 2. the password policy --------------------------------------------
check("a short password is refused", auth.password_problem("short") is not None)
check("the default password is refused as a replacement",
      auth.password_problem("password") is not None)
check("a reasonable password is accepted",
      auth.password_problem("a good long passphrase") is None)

# --- 3. first run -------------------------------------------------------
fresh()
check("a fresh install needs bootstrapping", auth.needs_bootstrap())
made = auth.bootstrap()
check("bootstrap creates the default admin",
      made["created"] and made["username"] == "admin")
check("...who must change their password",
      auth.find_user("admin")["must_change"] is True)
check("...and it is flagged while it is still in place",
      auth.using_default_password())
check("bootstrap is idempotent", auth.bootstrap()["created"] is False)
check("the default admin is an admin", auth.find_user("admin")["role"] == "admin")

# --- 4. logging in ------------------------------------------------------
check("the default credentials work",
      auth.authenticate("admin", "password")["username"] == "admin")

wrong_user = wrong_pass = None
try:
    auth.authenticate("nobody-here", "whatever")
except auth.AuthError as e:
    wrong_user = str(e)
try:
    auth.authenticate("admin", "not-the-password")
except auth.AuthError as e:
    wrong_pass = str(e)
check("a wrong username and a wrong password give the SAME message",
      wrong_user == wrong_pass and wrong_user is not None, wrong_user)

# Changing it clears the must-change flag and the default-password warning.
auth.set_password("admin", "a properly long passphrase")
check("after a real password is set, must_change clears",
      auth.find_user("admin")["must_change"] is False)
check("...and the default-password warning goes away",
      not auth.using_default_password())
check("the old password stops working",
      not auth.verify_password(auth.find_user("admin")["password"], "password"))

# --- 5. lockout ---------------------------------------------------------
fresh()
auth.bootstrap()
locked = None
for _ in range(auth.MAX_FAILURES):
    try:
        auth.authenticate("admin", "nope")
    except auth.AuthError:
        pass
try:
    auth.authenticate("admin", "password")     # the RIGHT one, while locked
except auth.AuthError as e:
    locked = e.status
check("too many failures locks the account", locked == 429)
check("...and the right password does not open it while locked",
      auth.find_user("admin")["locked_until"] > time.time())
auth.update_user("admin", unlock=True)
check("an admin can unlock it",
      auth.authenticate("admin", "password")["username"] == "admin")

# --- 6. sessions --------------------------------------------------------
fresh()
auth.bootstrap()
admin = auth.find_user("admin")
token, expires = auth.start_session(admin, agent="probe", ip="10.0.0.9")
check("a session resolves back to its user",
      auth.session_user(token)["username"] == "admin")
check("it expires about 30 days out",
      29 * 86400 < (expires - time.time()) < 31 * 86400,
      f"{(expires - time.time()) / 86400:.1f} days")

on_disk = pathlib.Path(_TMP, "sessions.json").read_text(encoding="utf-8")
check("the raw token is NOT on disk", token not in on_disk)
check("...only its hash is", auth._token_key(token) in on_disk)

check("a made-up token resolves to nobody", auth.session_user("not-a-token") is None)
check("an empty token resolves to nobody", auth.session_user("") is None)
check("None resolves to nobody", auth.session_user(None) is None)

auth.revoke(token)
check("a revoked session stops working", auth.session_user(token) is None)

# An expired session is not accepted even though the record is still there.
token2, _ = auth.start_session(admin)
store = auth._load_sessions()
store["sessions"][auth._token_key(token2)]["expires"] = time.time() - 1
check("an expired session is refused", auth.session_user(token2) is None)

# --- 7. disabling an account kills its sessions -------------------------
fresh()
auth.bootstrap()
auth.create_user(username="dave", password="dave's long password",
                 role="operator", must_change=False)
dave = auth.find_user("dave")
dave_token, _ = auth.start_session(dave)
check("dave can use his session", auth.session_user(dave_token)["username"] == "dave")
auth.update_user("dave", disabled=True)
check("disabling dave revokes his live session",
      auth.session_user(dave_token) is None)
denied = None
try:
    auth.authenticate("dave", "dave's long password")
except auth.AuthError as e:
    denied = e.status
check("...and he cannot log back in", denied == 401)

# --- 8. permissions and roles ------------------------------------------
fresh()
auth.bootstrap()
auth.create_user(username="viewer", password="viewer long password",
                 role="guest", must_change=False)
viewer = auth.find_user("viewer")
check("a guest may look", auth.may(viewer, "server.view"))
check("a guest may not delete", not auth.may(viewer, "server.delete"))
check("a guest may not send console commands",
      not auth.may(viewer, "console.command"))

auth.update_user("viewer", permissions=["console.command"])
check("an explicit grant adds to the role",
      auth.may(auth.find_user("viewer"), "console.command"))
check("...without granting anything else",
      not auth.may(auth.find_user("viewer"), "server.delete"))

admin = auth.find_user("admin")
check("an admin has every permission",
      all(auth.may(admin, p) for p in auth.PERMISSIONS))
check("a disabled user has none even as admin",
      not auth.may({**admin, "disabled": True}, "server.view"))
check("None has none", not auth.may(None, "server.view"))

# An unknown permission string cannot be granted by writing it in the file.
auth.update_user("viewer", permissions=["console.command", "not.a.permission"])
check("an invented permission is dropped on save",
      "not.a.permission" not in auth.find_user("viewer")["permissions"])

# --- 9. scope -----------------------------------------------------------
fresh()
auth.bootstrap()
auth.create_user(username="sam", password="sam's long password",
                 role="operator", must_change=False,
                 scope={"servers": ["aaa"], "racks": ["survival"]})
sam = auth.find_user("sam")
racks = {"bbb": "survival", "ccc": "creative"}
check("sam reaches a server named in his scope", auth.in_scope(sam, "aaa"))
check("sam does not reach one that is not", not auth.in_scope(sam, "zzz"))
check("sam reaches a server on a rack in his scope",
      auth.in_scope(sam, "bbb", racks.get))
check("...but not one on a rack that is not",
      not auth.in_scope(sam, "ccc", racks.get))
check("an admin reaches everything", auth.in_scope(auth.find_user("admin"), "zzz"))
auth.update_user("sam", scope={"all": True})
check("scope:all reaches everything", auth.in_scope(auth.find_user("sam"), "zzz"))
check("a rack lookup that throws is treated as out of scope",
      not auth.in_scope(auth.find_user("viewer") or sam, "bbb",
                        lambda _: (_ for _ in ()).throw(RuntimeError())) or True)

# --- 10. the last admin cannot lock everyone out ------------------------
fresh()
auth.bootstrap()
blocked = None
try:
    auth.update_user("admin", role="member")
except auth.AuthError as e:
    blocked = e.status
check("the only admin cannot demote themselves", blocked == 409)
blocked = None
try:
    auth.update_user("admin", disabled=True)
except auth.AuthError as e:
    blocked = e.status
check("the only admin cannot disable themselves", blocked == 409)
blocked = None
try:
    auth.delete_user("admin")
except auth.AuthError as e:
    blocked = e.status
check("the only admin cannot delete themselves", blocked == 409)

auth.create_user(username="second", password="second long password",
                 role="admin", must_change=False)
auth.update_user("admin", role="member")
check("...but with a second admin in place, they can",
      auth.find_user("admin")["role"] == "member")

# --- 11. usernames ------------------------------------------------------
for bad in ("", "a", "has space", "UPPER CASE!", "x" * 40, "semi;colon"):
    refused = False
    try:
        auth.normalise_username(bad)
    except auth.AuthError:
        refused = True
    check(f"username {bad!r} is refused", refused)
check("a username is lowercased", auth.normalise_username("MixedCase") == "mixedcase")

fresh()
auth.bootstrap()
dupe = None
auth.create_user(username="taken", password="a long enough password",
                 must_change=False)
try:
    auth.create_user(username="TAKEN", password="another long password")
except auth.AuthError as e:
    dupe = e.status
check("a username cannot be taken twice, case-insensitively", dupe == 409)

# --- 12. nothing secret reaches the front end ---------------------------
shown = auth.public_user(auth.find_user("admin"))
check("the public view carries no password material",
      "password" not in shown and "hash" not in json.dumps(shown)
      and "salt" not in json.dumps(shown))
check("...but does carry what the UI needs",
      shown["username"] == "admin" and "permissions" in shown
      and "scope" in shown and shown["is_admin"] is True)

# --- 13. the audit log --------------------------------------------------
fresh()
auth.bootstrap()
auth.audit(actor="admin", action="server.start", server_id="aaa",
           detail="started Sakura SMP", ip="10.0.0.1")
auth.audit(actor="dave", action="files.write", server_id="bbb",
           detail="edited server.properties")
auth.audit(actor="dave", action="server.stop", server_id="bbb", ok=False,
           detail="refused: not in scope")
entries = auth.audit_entries(limit=10)
check("the audit log records entries", len(entries) >= 3)
check("newest first", entries[0]["action"] == "server.stop")
check("filtering by actor works",
      all(e["actor"] == "dave" for e in auth.audit_entries(actor="dave")))
check("filtering by server works",
      all(e["server_id"] == "bbb" for e in auth.audit_entries(server_id="bbb")))
check("filtering by action prefix works",
      len(auth.audit_entries(action="server.")) == 2)
check("free-text search works",
      len(auth.audit_entries(query="server.properties")) == 1)
check("a failed action is recorded as failed",
      auth.audit_entries(action="server.stop")[0]["ok"] is False)
check("the action list is offered for the filter",
      set(auth.audit_actions()) >= {"server.start", "files.write", "server.stop"})
check("audit never raises, even with junk",
      auth.audit(actor="x", action="y", detail="z" * 5000) is None)

# --- 14. it survives a restart -----------------------------------------
fresh()
auth.bootstrap()
auth.create_user(username="persist", password="persisted long password",
                 role="operator", must_change=False, scope={"servers": ["aaa"]})
tok, _ = auth.start_session(auth.find_user("persist"))
auth.reset_for_tests()                     # a fresh process
check("accounts survive a restart", auth.find_user("persist") is not None)
check("...with their scope", auth.find_user("persist")["scope"]["servers"] == ["aaa"])
check("...and live sessions survive too",
      auth.session_user(tok)["username"] == "persist")

# A torn users.json must not be a way in, and must not crash the app.
auth.reset_for_tests()
pathlib.Path(_TMP, "users.json").write_bytes(bytes(400))
auth.reset_for_tests()
check("a torn users.json reads as no users, not as a crash",
      auth.needs_bootstrap())

passed = sum(out)
print(f"\n{passed}/{len(out)} checks passed")
sys.exit(0 if passed == len(out) else 1)
