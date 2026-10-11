#!/usr/bin/env python3
"""Accounts, passwords, sessions and scope.

This is the one module in the app where being wrong is a security bug
rather than a papercut, so the tests are about the properties that matter
rather than about the happy path:

  * a stored password must not be recoverable from what is in the database
  * a stored SESSION must not be usable from what is in the database
  * a wrong username and a wrong password must be indistinguishable
  * the last admin must not be able to lock everyone out
  * a disabled account's live sessions must die with it

Offline. Runs on a throwaway SQLite file, or on a real Postgres when
TEST_DATABASE_URL names one (see dev/tools/_dbtest.py):

    python dev/tools/test_auth.py
    TEST_DATABASE_URL=postgresql+asyncpg://blessforge@127.0.0.1:55432/bf_test \\
        python dev/tools/test_auth.py
"""
import asyncio
import json
import sys
import time

import _dbtest  # noqa: F401 -- sets STATE_DIR before app is imported

from sqlalchemy import select, text  # noqa: E402

from app import auth, db  # noqa: E402
from app.db.models import Session as SessionRow, User  # noqa: E402

out = []


def check(name, cond, extra=""):
    out.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


async def status_of(coro):
    try:
        await coro
    except auth.AuthError as e:
        return e.status
    return None


async def main():
    print(f"(database: {_dbtest.label()})")

    # --- 1. passwords are not recoverable -----------------------------------
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
    url = await _dbtest.fresh()
    check("a fresh install needs bootstrapping", await auth.needs_bootstrap())
    made = await auth.bootstrap()
    check("bootstrap creates the default admin",
          made["created"] and made["username"] == "admin")
    check("...who must change their password",
          (await auth.find_user("admin"))["must_change"] is True)
    check("...and it is flagged while it is still in place",
          await auth.using_default_password())
    check("bootstrap is idempotent", (await auth.bootstrap())["created"] is False)
    check("the default admin is an admin",
          (await auth.find_user("admin"))["role"] == "admin")

    # --- 4. logging in ------------------------------------------------------
    check("the default credentials work",
          (await auth.authenticate("admin", "password"))["username"] == "admin")
    wrong_user = wrong_pass = None
    try:
        await auth.authenticate("nobody-here", "whatever")
    except auth.AuthError as e:
        wrong_user = str(e)
    try:
        await auth.authenticate("admin", "not-the-password")
    except auth.AuthError as e:
        wrong_pass = str(e)
    check("a wrong username and a wrong password give the SAME message",
          wrong_user == wrong_pass and wrong_user is not None, wrong_user)
    check("...and the wrong password was counted",
          (await auth.find_user("admin"))["failures"] == 1)

    await auth.set_password("admin", "a properly long passphrase")
    check("after a real password is set, must_change clears",
          (await auth.find_user("admin"))["must_change"] is False)
    check("...and the default-password warning goes away",
          not await auth.using_default_password())
    check("the old password stops working",
          not auth.verify_password((await auth.find_user("admin"))["password"], "password"))

    # --- 5. lockout ---------------------------------------------------------
    url = await _dbtest.fresh()
    await auth.bootstrap()
    for _ in range(auth.MAX_FAILURES):
        await status_of(auth.authenticate("admin", "nope"))
    locked = await status_of(auth.authenticate("admin", "password"))
    check("too many failures locks the account", locked == 429, locked)
    check("...and the right password does not open it while locked",
          (await auth.find_user("admin"))["locked_until"] > time.time())
    await auth.update_user("admin", unlock=True)
    check("an admin can unlock it",
          (await auth.authenticate("admin", "password"))["username"] == "admin")

    # --- 6. sessions --------------------------------------------------------
    url = await _dbtest.fresh()
    await auth.bootstrap()
    admin = await auth.find_user("admin")
    token, expires = await auth.start_session(admin, agent="probe", ip="10.0.0.9")
    check("a session resolves back to its user",
          (await auth.session_user(token))["username"] == "admin")
    check("it expires about 30 days out",
          29 * 86400 < (expires - time.time()) < 31 * 86400,
          f"{(expires - time.time()) / 86400:.1f} days")

    async with db.session() as s:
        dump = json.dumps([
            [str(v) for v in row] for row in
            (await s.execute(text("select * from sessions"))).all()])
    check("the raw token is NOT in the database", token not in dump)
    check("...only its hash is", auth._token_key(token) in dump)

    check("a made-up token resolves to nobody",
          await auth.session_user("not-a-token") is None)
    check("an empty token resolves to nobody", await auth.session_user("") is None)
    check("None resolves to nobody", await auth.session_user(None) is None)

    await auth.revoke(token)
    check("a revoked session stops working", await auth.session_user(token) is None)

    token2, _ = await auth.start_session(admin)
    async with db.session() as s:
        row = await s.get(SessionRow, auth._token_key(token2))
        row.expires_at = db.from_epoch(time.time() - 1)
    check("an expired session is refused", await auth.session_user(token2) is None)
    async with db.session() as s:
        gone = await s.get(SessionRow, auth._token_key(token2))
    check("...and its row is cleared away", gone is None)

    # --- 7. disabling an account kills its sessions -------------------------
    url = await _dbtest.fresh()
    await auth.bootstrap()
    await auth.create_user(username="dave", password="dave's long password",
                           role="operator", must_change=False)
    dave = await auth.find_user("dave")
    dave_token, _ = await auth.start_session(dave)
    check("dave can use his session",
          (await auth.session_user(dave_token))["username"] == "dave")
    await auth.update_user("dave", disabled=True)
    check("disabling dave revokes his live session",
          await auth.session_user(dave_token) is None)
    denied = await status_of(auth.authenticate("dave", "dave's long password"))
    check("...and he cannot log back in", denied == 401, denied)

    # --- 8. permissions and roles ------------------------------------------
    url = await _dbtest.fresh()
    await auth.bootstrap()
    await auth.create_user(username="viewer", password="viewer long password",
                           role="guest", must_change=False)
    viewer = await auth.find_user("viewer")
    check("a guest may look", auth.may(viewer, "server.view"))
    check("a guest may not delete", not auth.may(viewer, "server.delete"))
    check("a guest may not send console commands",
          not auth.may(viewer, "console.command"))
    await auth.update_user("viewer", permissions=["console.command"])
    check("an explicit grant adds to the role",
          auth.may(await auth.find_user("viewer"), "console.command"))
    check("...without granting anything else",
          not auth.may(await auth.find_user("viewer"), "server.delete"))
    admin = await auth.find_user("admin")
    check("an admin has every permission",
          all(auth.may(admin, p) for p in auth.PERMISSIONS))
    check("a disabled user has none even as admin",
          not auth.may({**admin, "disabled": True}, "server.view"))
    check("None has none", not auth.may(None, "server.view"))
    await auth.update_user("viewer", permissions=["console.command", "not.a.permission"])
    check("an invented permission is dropped on save",
          "not.a.permission" not in (await auth.find_user("viewer"))["permissions"])

    # --- 9. scope -----------------------------------------------------------
    url = await _dbtest.fresh()
    await auth.bootstrap()
    await auth.create_user(username="sam", password="sam's long password",
                           role="operator", must_change=False,
                           scope={"servers": ["aaa"], "racks": ["survival"]})
    sam = await auth.find_user("sam")
    racks = {"bbb": "survival", "ccc": "creative"}
    check("sam reaches a server named in his scope", auth.in_scope(sam, "aaa"))
    check("sam does not reach one that is not", not auth.in_scope(sam, "zzz"))
    check("sam reaches a server on a rack in his scope",
          auth.in_scope(sam, "bbb", racks.get))
    check("...but not one on a rack that is not",
          not auth.in_scope(sam, "ccc", racks.get))
    check("an admin reaches everything",
          auth.in_scope(await auth.find_user("admin"), "zzz"))
    await auth.update_user("sam", scope={"all": True})
    check("scope:all reaches everything",
          auth.in_scope(await auth.find_user("sam"), "zzz"))
    check("a rack lookup that throws is treated as out of scope",
          not auth.in_scope(sam, "bbb", lambda _: (_ for _ in ()).throw(RuntimeError())))

    # --- 10. the last admin cannot lock everyone out ------------------------
    url = await _dbtest.fresh()
    await auth.bootstrap()
    check("the only admin cannot demote themselves",
          await status_of(auth.update_user("admin", role="member")) == 409)
    check("the only admin cannot disable themselves",
          await status_of(auth.update_user("admin", disabled=True)) == 409)
    check("the only admin cannot delete themselves",
          await status_of(auth.delete_user("admin")) == 409)
    check("...and after all three refusals, admin is still an admin",
          (await auth.find_user("admin"))["role"] == "admin"
          and not (await auth.find_user("admin"))["disabled"])
    await auth.create_user(username="second", password="second long password",
                           role="admin", must_change=False)
    await auth.update_user("admin", role="member")
    check("...but with a second admin in place, they can",
          (await auth.find_user("admin"))["role"] == "member")

    # Two admins demoting each other at the same moment: the admin rows are
    # locked while they are counted, so exactly one demotion may win.
    url = await _dbtest.fresh()
    await auth.bootstrap()
    await auth.create_user(username="other", password="other long password",
                           role="admin", must_change=False)
    results = await asyncio.gather(
        status_of(auth.update_user("admin", role="member")),
        status_of(auth.update_user("other", role="member")),
        return_exceptions=True)
    async with db.session() as s:
        admins = (await s.execute(select(User).where(User.role == "admin"))).scalars().all()
    check("two admins demoting each other at once leave one admin standing",
          len(admins) == 1, f"{len(admins)} admins left, results {results}")
    check("...and the loser is told why (a 409), not a crash",
          sorted(map(str, results)) == ["409", "None"], results)

    # --- 11. usernames ------------------------------------------------------
    for bad in ("", "a", "has space", "UPPER CASE!", "x" * 40, "semi;colon"):
        refused = False
        try:
            auth.normalise_username(bad)
        except auth.AuthError:
            refused = True
        check(f"username {bad!r} is refused", refused)
    check("a username is lowercased", auth.normalise_username("MixedCase") == "mixedcase")
    url = await _dbtest.fresh()
    await auth.bootstrap()
    await auth.create_user(username="taken", password="a long enough password",
                           must_change=False)
    dupe = await status_of(auth.create_user(username="TAKEN",
                                            password="another long password"))
    check("a username cannot be taken twice, case-insensitively", dupe == 409)

    # --- 12. nothing secret reaches the front end ---------------------------
    shown = auth.public_user(await auth.find_user("admin"))
    check("the public view carries no password material",
          "password" not in shown and "hash" not in json.dumps(shown)
          and "salt" not in json.dumps(shown))
    check("...but does carry what the UI needs",
          shown["username"] == "admin" and "permissions" in shown
          and "scope" in shown and shown["is_admin"] is True)

    # --- 13. the audit log --------------------------------------------------
    url = await _dbtest.fresh()
    await auth.bootstrap()
    await auth.audit(actor="admin", action="server.start", server_id="aaa",
                     detail="started Sakura SMP", ip="10.0.0.1")
    await auth.audit(actor="dave", action="files.write", server_id="bbb",
                     detail="edited server.properties")
    await auth.audit(actor="dave", action="server.stop", server_id="bbb", ok=False,
                     detail="refused: not in scope")
    entries = await auth.audit_entries(limit=10)
    check("the audit log records entries", len(entries) >= 3)
    check("newest first", entries[0]["action"] == "server.stop")
    check("filtering by actor works",
          all(e["actor"] == "dave" for e in await auth.audit_entries(actor="dave")))
    check("filtering by server works",
          all(e["server_id"] == "bbb" for e in await auth.audit_entries(server_id="bbb")))
    check("filtering by action prefix works",
          len(await auth.audit_entries(action="server.")) == 2)
    check("free-text search works",
          len(await auth.audit_entries(query="server.properties")) == 1)
    check("a failed action is recorded as failed",
          (await auth.audit_entries(action="server.stop"))[0]["ok"] is False)
    check("the action list is offered for the filter",
          set(await auth.audit_actions()) >= {"server.start", "files.write", "server.stop"})
    check("audit never raises, even with junk",
          await auth.audit(actor="x" * 500, action="y" * 500, detail="z" * 50000) is None)
    await auth.audit(actor="admin", action="admin.users.update", server_id="",
                     detail="role", before={"role": "member"}, after={"role": "operator"})
    change = (await auth.audit_entries(action="admin.users.update"))[0]
    check("an admin write keeps its before and after values",
          change.get("before") == {"role": "member"}
          and change.get("after") == {"role": "operator"})
    check("an action prefix with a LIKE wildcard in it matches literally",
          await auth.audit_entries(action="server%") == [])

    # --- 14. it survives a restart -----------------------------------------
    url = await _dbtest.fresh()
    await auth.bootstrap()
    await auth.create_user(username="persist", password="persisted long password",
                           role="operator", must_change=False, scope={"servers": ["aaa"]})
    tok, _ = await auth.start_session(await auth.find_user("persist"))
    await auth.audit(actor="persist", action="server.start", server_id="aaa")
    await _dbtest.restart(url)              # every connection closed, rebuilt
    check("accounts survive a restart", await auth.find_user("persist") is not None)
    check("...with their scope",
          (await auth.find_user("persist"))["scope"]["servers"] == ["aaa"])
    check("...and live sessions survive too",
          (await auth.session_user(tok))["username"] == "persist")
    check("...and so does the audit trail",
          any(e["actor"] == "persist" for e in await auth.audit_entries()))

    await db.dispose()
    passed = sum(out)
    print(f"\n{passed}/{len(out)} checks passed")
    return passed == len(out)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
