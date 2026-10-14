#!/usr/bin/env python3
"""Access control (app/access.py): two-factor sign-in, API tokens, and
per-server grants, down to the HTTP guard.

    python dev/tools/test_access.py
    TEST_DATABASE_URL=postgresql+asyncpg://... python dev/tools/test_access.py

Offline: a fresh database and the in-memory engine. Checks the RFC 6238
vectors, that a code works once, that a challenge cannot be forged, reused
past its time or outlive a password change, that wrong codes lock the
account even between right passwords, that a token never has more than its
owner, and that a grant opens one server and no other.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dev" / "tools"))

import _dbtest  # noqa: E402 -- a temp STATE_DIR and database

import httpx  # noqa: E402

from app import access, auth, db, engine  # noqa: E402
from app.db.models import Server  # noqa: E402
from app.engine.mock import MockEngine  # noqa: E402

results: list[bool] = []
PW = "a long enough password"


def check(name, cond, extra=""):
    results.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {extra}" if extra else ""),
          flush=True)


async def status_of(coro) -> int:
    try:
        await coro
        return 200
    except auth.AuthError as e:
        return e.status


def totp_maths():
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    key = b"12345678901234567890"
    check("RFC 6238 vectors (SHA-1): 59s and 1111111109s",
          access.hotp(key, 59 // 30, 8) == "94287082"
          and access.hotp(key, 1111111109 // 30, 8) == "07081804")
    now = 1_800_000_000.0
    code = access.code_at(secret, now)
    check("a code matches its own step, and the steps either side (clock drift)",
          access.match_code(secret, code, now) == int(now // 30)
          and access.match_code(secret, code, now + 30) == int(now // 30)
          and access.match_code(secret, code, now - 30) == int(now // 30))
    check("...but not two steps away, and not a used step",
          access.match_code(secret, code, now + 60) is None
          and access.match_code(secret, code, now, last_step=int(now // 30)) is None)
    check("letters, short codes and spaces-only are refused",
          access.match_code(secret, "12a456", now) is None
          and access.match_code(secret, "12345", now) is None
          and access.match_code(secret, "      ", now) is None)
    check("a code typed with a space in the middle still works",
          access.match_code(secret, code[:3] + " " + code[3:], now) is not None)
    uri = access.otpauth_uri(secret, "sam")
    check("the otpauth link names the issuer and the account",
          uri.startswith("otpauth://totp/BlessForge%3Asam?secret=" + secret)
          and "issuer=BlessForge" in uri, uri)
    plain, hashes = access.new_recovery_codes()
    check("ten recovery codes, stored only as keyed hashes",
          len(plain) == 10 == len(set(hashes)) and all(c not in h for c, h in zip(plain, hashes))
          and all(len(c) == 11 and c[5] == "-" for c in plain), plain[:2])


async def two_factor():
    await auth.create_user(username="sam", password=PW, role="operator", must_change=False)
    check("setting up needs the current password",
          await status_of(access.begin_setup("sam", "wrong")) == 403)
    setup = await access.begin_setup("sam", PW)
    secret = setup["secret"]
    st = await access.status("sam")
    check("a setup started is pending, not on", st["pending"] and not st["enabled"], st)
    user = await auth.authenticate("sam", PW)
    check("...so signing in still takes only the password", not user["two_factor"])
    check("a wrong code does not turn it on",
          await status_of(access.enable("sam", "000000")) == 400
          and not (await access.status("sam"))["enabled"])
    t0 = time.time()
    out = await access.enable("sam", access.code_at(secret, t0), now=t0)
    check("the right code turns it on and gives ten recovery codes once",
          out["enabled"] and len(out["recovery_codes"]) == 10)
    codes = out["recovery_codes"]
    stored = await _stored_secret("sam")
    check("the secret is sealed in the database (and fits its column), not stored as typed",
          stored not in (None, secret) and secret not in stored and len(stored) <= 128,
          stored and len(stored))

    user = await auth.authenticate("sam", PW)
    check("now the password step says two-factor is on", user["two_factor"])
    ch, _ = access.make_challenge(user)
    check("...and the code it was turned on with is already spent",
          await status_of(access.finish_login(ch, access.code_at(secret, t0), now=t0)) == 401)
    t1 = t0 + 30
    signed, how, _ = await access.finish_login(ch, access.code_at(secret, t1), now=t1)
    check("the next code signs in", signed["username"] == "sam" and how == "totp")
    ch2, _ = access.make_challenge(user)
    check("...and that code cannot be used twice",
          await status_of(access.finish_login(ch2, access.code_at(secret, t1), now=t1)) == 401)

    body, mac = ch2.rsplit(".", 1)
    forged = f"{body}.{'0' * len(mac)}"
    other = await auth.create_user(username="eve", password=PW, must_change=False)
    eve_body = body.replace(body.split(".")[0], str(other["id"]), 1)
    t2 = t1 + 30
    check("a forged signature, or a challenge moved to another account, is refused",
          await status_of(access.finish_login(forged, access.code_at(secret, t2), now=t2)) == 401
          and await status_of(access.finish_login(f"{eve_body}.{mac}",
                                                  access.code_at(secret, t2), now=t2)) == 401)
    check("an expired challenge is refused",
          await status_of(access.finish_login(ch2, access.code_at(secret, t2),
                                              now=t2 + access.CHALLENGE_TTL + 60)) == 401)
    old_ch, _ = access.make_challenge(user)
    await auth.set_password("sam", PW + "!")
    check("changing the password kills challenges already handed out",
          await status_of(access.finish_login(old_ch, access.code_at(secret, t2), now=t2)) == 401)
    await auth.set_password("sam", PW)
    user = await auth.authenticate("sam", PW)

    ch3, _ = access.make_challenge(user)
    signed, how, left = await access.finish_login(ch3, codes[0].upper(), now=t2)
    check("a recovery code signs in (case and dash do not matter) and is used up",
          how == "recovery" and left == 9)
    ch4, _ = access.make_challenge(user)
    check("...the same recovery code a second time is refused",
          await status_of(access.finish_login(ch4, codes[0], now=t2)) == 401)

    # Wrong codes lock the account, and a right password does not clear them.
    await auth.update_user("sam", unlock=True)
    tries, locked = 0, False
    for _ in range(auth.MAX_FAILURES + 2):
        try:
            u = await auth.authenticate("sam", PW)      # the right password, each time
        except auth.AuthError as e:
            locked = e.status == 429
            break
        ch5, _ = access.make_challenge(u)
        await status_of(access.finish_login(ch5, "111111", now=t2))
        tries += 1
    t3 = t2 + 30
    check("eight wrong codes lock the account, with the right password before each",
          locked and tries == auth.MAX_FAILURES, (locked, tries))
    await auth.update_user("sam", unlock=True)

    user = await auth.authenticate("sam", PW)
    check("turning it off needs the password and a code",
          await status_of(access.disable("sam", PW, "222222", now=t3)) == 400
          and await status_of(access.disable("sam", "nope", access.code_at(secret, t3),
                                             now=t3)) == 403)
    fresh = await access.regenerate_recovery("sam", PW, access.code_at(secret, t3), now=t3)
    ch6, _ = access.make_challenge(user)
    check("new recovery codes replace the old ones",
          len(fresh) == 10
          and await status_of(access.finish_login(ch6, codes[1], now=t3)) == 401)
    t4 = t3 + 30
    await access.disable("sam", PW, access.code_at(secret, t4), now=t4)
    check("...and with password and code it goes off; sign-in is the password again",
          not (await auth.authenticate("sam", PW))["two_factor"])

    s2 = await access.begin_setup("sam", PW)
    t5 = t4 + 30
    await access.enable("sam", access.code_at(s2["secret"], t5), now=t5)
    was = await access.admin_reset("sam")
    check("an admin can reset it (a lost phone)",
          was and not (await access.status("sam"))["enabled"])


async def _stored_secret(name: str):
    from sqlalchemy import select
    from app.db.models import User
    async with db.session() as s:
        return (await s.execute(select(User.totp_secret).where(User.username == name))).scalar()


async def tokens_and_grants(a: str, b: str):
    owner = await auth.find_user("sam")             # an operator, scoped to server a
    await auth.update_user("sam", scope={"servers": [a]})
    owner = await auth.find_user("sam")
    check("a token cannot have a permission its owner lacks",
          await status_of(access.create_token(owner, name="x",
                                              permissions=["server.delete"])) == 400)
    check("...nor a server outside the owner's scope",
          await status_of(access.create_token(owner, name="x", permissions=["server.view"],
                                              servers=[b])) == 400)
    check("...and needs a name and at least one permission",
          await status_of(access.create_token(owner, name="", permissions=["server.view"])) == 400
          and await status_of(access.create_token(owner, name="x", permissions=[])) == 400)
    made = await access.create_token(owner, name="Discord bot",
                                     permissions=["server.view", "server.power"],
                                     expires_days=30)
    tok = made["token"]
    listed = await access.list_tokens(owner["id"])
    check("the token is shown once; the list has its prefix, never the token",
          tok.startswith("bf_") and listed[0]["prefix"] == tok[:11]
          and all(tok not in str(v) for v in listed[0].values()), listed[0])
    via = await access.token_user(tok, "10.0.0.9")
    check("the token acts as its owner with only its own permissions",
          via["username"] == "sam" and auth.may(via, "server.power", a)
          and not auth.may(via, "console.command", a)
          and auth.may(await auth.find_user("sam"), "console.command"))
    await auth.update_user("sam", role="guest")
    via = await access.token_user(tok)
    check("demoting the owner demotes the token", not auth.may(via, "server.power", a))
    await auth.update_user("sam", role="operator")
    check("a garbled or unknown token is nobody",
          await access.token_user("bf_nope") is None and await access.token_user("x") is None)

    admin = await auth.find_user("admin")
    kept = await access.create_token(admin, name="Home Assistant",
                                     permissions=["server.view"], servers=[a])
    via_admin = await access.token_user(kept["token"])
    check("an admin's token kept to one server cannot reach another",
          auth.in_scope(via_admin, a) and not auth.in_scope(via_admin, b)
          and not auth.may(via_admin, "server.delete", a))

    short = await access.create_token(owner, name="short", permissions=["server.view"],
                                      expires_days=1)
    from sqlalchemy import update
    from app.db.models import ApiToken
    async with db.session() as s:
        await s.execute(update(ApiToken).where(ApiToken.id == short["item"]["id"])
                        .values(expires_at=db.from_epoch(time.time() - 5)))
    check("an expired token is nobody", await access.token_user(short["token"]) is None)
    await auth.update_user("sam", disabled=True)
    check("...and so is the token of a disabled account", await access.token_user(tok) is None)
    await auth.update_user("sam", disabled=False)
    await access.revoke_token(made["item"]["id"], owner["id"])
    check("a revoked token is nobody", await access.token_user(tok) is None)

    # Grants: a guest everywhere, an operator of one server.
    await auth.create_user(username="guest1", password=PW, role="guest", must_change=False)
    check("a global permission cannot be granted on one server",
          await status_of(access.set_grant("guest1", a, ["racks.manage"])) == 400)
    g = await access.set_grant("guest1", a, ["console.command", "server.power"])
    check("a grant always includes seeing the server",
          g["permissions"][0] == "server.view", g)
    from sqlalchemy import select
    from app.db.models import User
    async with db.session() as s:
        gid = (await s.execute(select(User.id).where(User.username == "guest1"))).scalar()
        grants = await auth._grants_of(s, gid)
    guest = {**(await auth.find_user("guest1")), "grants": grants}
    check("the grant opens that server, with its permissions there only",
          auth.in_scope(guest, a) and not auth.in_scope(guest, b)
          and auth.may(guest, "server.power", a) and not auth.may(guest, "server.power", b)
          and not auth.may(guest, "server.power"))
    who = {u["username"]: u for u in await access.server_access(a)}
    check("the server's access list names the admin, the scoped operator and the grant",
          who.get("admin", {}).get("how") == "admin" and who.get("sam", {}).get("how") == "scope"
          and who.get("guest1", {}).get("how") == "grant"
          and "server.power" in who["guest1"]["permissions"] and "eve" not in who,
          {k: v["how"] for k, v in who.items()})
    await access.set_grant("guest1", a, [])
    check("an empty grant takes it away", await access.grants_of("guest1") == [])
    return kept["token"]


async def through_http(a: str, b: str, admin_token: str):
    from app.main import app
    await access.set_grant("guest1", a, ["console.read"])
    owner = await auth.find_user("sam")
    made = await access.create_token(owner, name="script", permissions=["server.view"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bf.test") as c:
        h = {"Authorization": f"Bearer {made['token']}"}
        r_in = await c.get(f"/api/instances/{a}/history?hours=1", headers=h)
        r_out = await c.get(f"/api/instances/{b}/history?hours=1", headers=h)
        check("a bearer token reaches its owner's server and not another (404)",
              r_in.status_code == 200 and r_out.status_code == 404,
              (r_in.status_code, r_out.status_code))
        r_power = await c.post(f"/api/instances/{a}/action/start", headers=h)
        check("...and is refused what the token was not given",
              r_power.status_code == 403, r_power.status_code)
        r_mint = await c.post("/api/auth/tokens", headers=h,
                              json={"name": "more", "permissions": ["server.view"]})
        r_admin = await c.get("/api/admin/users",
                              headers={"Authorization": f"Bearer {admin_token}"})
        check("a token cannot mint tokens, nor use admin routes even an admin's",
              r_mint.status_code == 403 and r_admin.status_code == 403,
              (r_mint.status_code, r_admin.status_code))

        # The two-step sign-in over HTTP.
        s3 = await access.begin_setup("sam", PW)
        t = time.time()
        await access.enable("sam", access.code_at(s3["secret"], t - 30), now=t - 30)
        r1 = await c.post("/api/auth/login", json={"username": "sam", "password": PW})
        j1 = r1.json()
        check("the password step answers with a challenge and no session",
              r1.status_code == 200 and j1.get("two_factor") and j1.get("challenge")
              and "bf_session" not in str(r1.headers.get("set-cookie", "")).lower()
              and not (await c.get("/api/auth/session")).json()["authenticated"], j1)
        r2 = await c.post("/api/auth/login/2fa",
                          json={"challenge": j1["challenge"], "code": "999999"})
        r3 = await c.post("/api/auth/login/2fa",
                          json={"challenge": j1["challenge"],
                                "code": access.code_at(s3["secret"], time.time())})
        sess = (await c.get("/api/auth/session")).json()
        check("a wrong code is a 401; the right one signs in",
              r2.status_code == 401 and r3.status_code == 200 and sess["authenticated"]
              and sess["user"]["two_factor"], (r2.status_code, r3.status_code))

        await c.post("/api/auth/logout")
        r4 = await c.post("/api/auth/login", json={"username": "guest1", "password": PW})
        mine = (await c.get("/api/auth/session")).json()["user"]
        r5 = await c.get(f"/api/instances/{a}/history?hours=1")
        r6 = await c.get(f"/api/instances/{b}/history?hours=1")
        check("a guest with a grant on one server sees that server and only it",
              r4.status_code == 200 and r5.status_code == 200 and r6.status_code == 404
              and a in mine["server_permissions"], (r5.status_code, r6.status_code))


async def admin_race():
    await _dbtest.fresh()
    await auth.bootstrap()
    await auth.create_user(username="other", password="other long password",
                           role="admin", must_change=False)
    out = await asyncio.gather(status_of(auth.update_user("admin", role="member")),
                               status_of(auth.update_user("other", role="member")))
    from sqlalchemy import select
    from app.db.models import User
    async with db.session() as s:
        admins = (await s.execute(select(User).where(User.role == "admin"))).scalars().all()
    check("two admins demoting each other at once leave one standing (both databases)",
          len(admins) == 1 and sorted(out) == [200, 409], (len(admins), out))


async def main():
    totp_maths()
    await _dbtest.fresh()
    mock = MockEngine(seed=False)
    engine.configure("mock", drivers={"local": mock})
    await auth.bootstrap()
    await auth.set_password("admin", "admin long password")
    a = await engine.create_server(name="Alpha", port=25801, memory_mb=1024)
    b = await engine.create_server(name="Beta", port=25802, memory_mb=1024)
    async with db.session() as s:
        s.add(Server(uuid=a, name="Alpha", port=25801))
        s.add(Server(uuid=b, name="Beta", port=25802))
    await two_factor()
    admin_token = await tokens_and_grants(a, b)
    await through_http(a, b, admin_token)
    await admin_race()
    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed ({_dbtest.label()})")
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
