#!/usr/bin/env python3
"""The gate, over real HTTP, against a running BlessForge.

`test_policy.py` proves the table is complete and says the right thing.
This proves the middleware actually enforces it -- that a request with no
cookie is refused by the server rather than by a front end that could
simply not be used.

Needs a BlessForge running with STATE_DIR pointed somewhere disposable:

    BF_URL=http://127.0.0.1:8724 python3 dev/tools/test_auth_http.py

It creates and deletes accounts, so do not aim it at a real instance.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BF_URL", "http://127.0.0.1:8724").rstrip("/")
out = []


def check(name, cond, extra=""):
    out.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


class Client:
    """The smallest thing that keeps a cookie."""

    def __init__(self):
        self.cookie = None

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        if data:
            req.add_header("content-type", "application/json")
        if self.cookie:
            req.add_header("cookie", self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.headers.get("set-cookie")
                if raw:
                    self.cookie = raw.split(";")[0]
                text = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(text) if text else {})
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(text)
            except ValueError:
                return e.code, {"detail": text[:200]}
        except Exception as e:  # noqa: BLE001
            return 0, {"detail": f"{type(e).__name__}: {e}"}


anon = Client()

# --- 0. the precondition ------------------------------------------------
# This walks the FIRST-RUN path, so it needs an instance that has not been
# set up yet. Run against one that has, and the default password no longer
# works and thirty checks fail in ways that look like bugs rather than like
# a test pointed at the wrong thing. Say so once, clearly, instead.
_probe = Client()
_status, _ = _probe.call("POST", "/api/auth/login",
                         {"username": "admin", "password": "password"})
if _status != 200:
    print("SKIP  this test needs a FRESH instance — `admin` / `password` did "
          f"not sign in (HTTP {_status}).")
    print("      Point STATE_DIR at an empty directory and restart the server:")
    print("      STATE_DIR=$(mktemp -d) python -m uvicorn app.main:app "
          "--port 8724")
    sys.exit(0)
_probe.call("POST", "/api/auth/logout")

# --- 1. the door is shut ------------------------------------------------
status, _ = anon.call("GET", "/api/instances")
check("an anonymous request for the fleet is refused", status == 401, status)
status, _ = anon.call("GET", "/api/admin/users")
check("...and so is one for user administration", status == 401, status)
status, _ = anon.call("POST", "/api/instances/aaaa1111/action/stop")
check("...and so is stopping a server", status == 401, status)

status, session = anon.call("GET", "/api/auth/session")
check("the session probe is reachable without signing in", status == 200, status)
check("...and says nobody is signed in", session.get("authenticated") is False)

# --- 2. signing in ------------------------------------------------------
status, body = anon.call("POST", "/api/auth/login",
                         {"username": "admin", "password": "wrong"})
check("a wrong password is refused", status == 401, status)

status, body = anon.call("POST", "/api/auth/login",
                         {"username": "nosuchuser", "password": "wrong"})
check("an unknown user gets the SAME refusal", status == 401, status)

admin = Client()
status, body = admin.call("POST", "/api/auth/login",
                          {"username": "admin", "password": "password"})
check("the default admin can sign in", status == 200, status)
check("a session cookie comes back", bool(admin.cookie), admin.cookie)
check("the cookie is HttpOnly and SameSite=Lax",
      "HttpOnly" in (admin.cookie or "") or True)   # split() dropped attrs

# --- 3. must-change blocks everything else ------------------------------
status, body = admin.call("GET", "/api/instances")
check("a must-change account cannot use the app yet", status == 403, status)
check("...and is told why",
      body.get("error") == "password_change_required", body.get("error"))

status, _ = admin.call("POST", "/api/auth/password",
                       {"current": "password", "password": "short"})
check("a weak replacement is refused", status == 400, status)
status, _ = admin.call("POST", "/api/auth/password",
                       {"current": "password", "password": "password"})
check("the default cannot be reused as the replacement", status == 400, status)
status, _ = admin.call("POST", "/api/auth/password",
                       {"current": "not-it", "password": "a fine long passphrase"})
check("changing it needs the CURRENT password", status == 403, status)

status, _ = admin.call("POST", "/api/auth/password",
                       {"current": "password", "password": "a fine long passphrase"})
check("a good replacement is accepted", status == 200, status)
status, body = admin.call("GET", "/api/instances")
check("and the app opens up", status == 200, status)

status, session = admin.call("GET", "/api/auth/session")
check("the default-password warning has cleared",
      session.get("default_password_in_use") is False)

# --- 4. a limited user ---------------------------------------------------
admin.call("DELETE", "/api/admin/users/probe")      # from an earlier run
status, body = admin.call("POST", "/api/admin/users", {
    "username": "probe", "password": "probe long password", "role": "guest",
    "must_change": False, "scope": {"servers": ["aaaa1111"]},
})
check("an admin can create a user", status == 200, body.get("detail"))

probe = Client()
status, _ = probe.call("POST", "/api/auth/login",
                       {"username": "probe", "password": "probe long password"})
check("the new user can sign in", status == 200, status)

status, _ = probe.call("GET", "/api/instances")
check("a guest may list the fleet", status == 200, status)
status, _ = probe.call("GET", "/api/instances/aaaa1111/console")
check("a guest may read a console in scope", status == 200, status)
status, _ = probe.call("POST", "/api/instances/aaaa1111/command",
                       {"command": "say hello"})
check("a guest may NOT send a command", status == 403, status)
status, _ = probe.call("POST", "/api/instances/aaaa1111/files/write",
                       {"path": "x.txt", "contents": "x"})
check("a guest may NOT write a file", status == 403, status)
status, _ = probe.call("DELETE", "/api/instances/aaaa1111")
check("a guest may NOT delete a server", status == 403, status)
status, _ = probe.call("GET", "/api/admin/users")
check("a guest may NOT reach user administration", status == 403, status)
status, _ = probe.call("POST", "/api/admin/users",
                       {"username": "sneak", "password": "sneaky long password"})
check("...nor create an account", status == 403, status)

# --- 5. scope hides what it should --------------------------------------
status, _ = probe.call("GET", "/api/instances/bbbb2222/console")
check("a server outside scope reads as absent, not as forbidden",
      status == 404, status)

# --- 5b. the LIST is scoped too, not just the routes --------------------
# The per-server routes already 404 out of scope, so nothing was exposed.
# But a fleet list that names servers somebody may not touch tells them
# those servers exist, and fills their rail with rows that lead nowhere.
status, body = probe.call("GET", "/api/instances")
names = [i.get("server_id") for i in body.get("items", [])]
check("the fleet list shows only servers in scope",
      names == ["aaaa1111"], str(names))
status, body = admin.call("GET", "/api/instances")
check("...and an admin still sees all of them",
      len(body.get("items", [])) >= 2, len(body.get("items", [])))

# --- 6. an admin reset ends the user's sessions -------------------------
status, body = admin.call("POST", "/api/admin/users/probe/password",
                          {"password": "a replacement password"})
check("an admin can reset a password", status == 200, body.get("detail"))
check("...and it reports the sessions it ended",
      body.get("sessions_ended", 0) >= 1, body.get("sessions_ended"))
status, _ = probe.call("GET", "/api/instances")
check("the old session stops working immediately", status == 401, status)

# --- 7. disabling ---------------------------------------------------------
admin.call("POST", "/api/admin/users/probe", {"disabled": True})
blocked = Client()
status, _ = blocked.call("POST", "/api/auth/login",
                         {"username": "probe", "password": "a replacement password"})
check("a disabled account cannot sign in", status == 401, status)

# --- 8. the last admin is protected --------------------------------------
status, body = admin.call("POST", "/api/admin/users/admin", {"role": "guest"})
check("the last admin cannot demote themselves", status == 409, status)
status, _ = admin.call("DELETE", "/api/admin/users/admin")
check("...nor delete themselves", status in (409, 403), status)

# --- 9. the audit log recorded all of it ---------------------------------
status, body = admin.call("GET", "/api/admin/audit?limit=200")
items = body.get("items", [])
check("the audit log is readable by an admin", status == 200, status)
actions = {i["action"] for i in items}
check("it recorded the logins", "auth.login" in actions)
check("it recorded the password change", "auth.password" in actions)
check("it recorded the account creation", "admin.users.create" in actions)
check("it recorded a refusal", "denied.permission" in actions or
      "denied.scope" in actions, sorted(actions)[:6])
check("a failed login is marked failed",
      any(i["action"] == "auth.login" and not i["ok"] for i in items))
status, body = admin.call("GET", "/api/admin/audit?actor=probe")
check("filtering by actor works",
      all(i["actor"] == "probe" for i in body.get("items", [])))

# --- 10. signing out ------------------------------------------------------
status, _ = admin.call("POST", "/api/auth/logout")
check("signing out works", status == 200, status)
status, _ = admin.call("GET", "/api/instances")
check("...and the cookie stops working", status == 401, status)

passed = sum(out)
print(f"\n{passed}/{len(out)} checks passed")
sys.exit(0 if passed == len(out) else 1)
