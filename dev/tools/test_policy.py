#!/usr/bin/env python3
"""Every API route has decided who may call it.

The point of this file is the first check: it walks the routes FastAPI
actually serves and asserts each one is matched by a rule in
`app.policy`. Adding an endpoint without deciding who may reach it is
then a failing test rather than an open door that nobody notices for a
month.

The rest is the shape of the policy itself -- that reads are readable by
readers, that writes are not, and that the sentinels behave.

Offline. No network, no Docker.

    python3 dev/tools/test_policy.py
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="bf-policy-")
os.environ["STATE_DIR"] = _TMP
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("CACHE_DIR", str(pathlib.Path(_TMP) / "cache"))

from app import auth, policy  # noqa: E402
from app.main import app  # noqa: E402

out = []


def check(name, cond, extra=""):
    out.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


# --- 1. every route is covered -----------------------------------------
uncovered = []
covered = 0
for route in app.routes:
    path = getattr(route, "path", "")
    if not path.startswith("/api"):
        continue
    for method in sorted(getattr(route, "methods", {"GET"}) - {"HEAD", "OPTIONS"}):
        # The real path has {name} placeholders; substitute something that
        # matches the wildcard the rules compile to.
        concrete = path
        while "{" in concrete:
            start = concrete.index("{")
            end = concrete.index("}", start)
            concrete = concrete[:start] + "x" + concrete[end + 1:]
        perm, _scoped = policy.required(method, concrete)
        if perm == policy.DENY:
            uncovered.append(f"{method} {path}")
        else:
            covered += 1

check("every /api route has a policy rule", not uncovered,
      f"{len(uncovered)} uncovered: " + ", ".join(uncovered[:6]) if uncovered
      else f"{covered} routes covered")

# --- 2. the sentinels ---------------------------------------------------
check("the login endpoint is public",
      policy.required("POST", "/api/auth/login")[0] == policy.PUBLIC)
check("the session probe is public",
      policy.required("GET", "/api/auth/session")[0] == policy.PUBLIC)
check("an unknown route is denied, not allowed",
      policy.required("GET", "/api/something/invented")[0] == policy.DENY)
check("an unknown METHOD on a known path is denied",
      policy.required("DELETE", "/api/health")[0] == policy.DENY)
check("user administration is admin-only",
      policy.required("POST", "/api/admin/users")[0] == policy.ADMIN)
check("the audit log is admin-only",
      policy.required("GET", "/api/admin/audit")[0] == policy.ADMIN)

# --- 3. reads and writes are not the same ------------------------------
pairs = [
    ("GET", "/api/instances/aaa/files", "files.read"),
    ("POST", "/api/instances/aaa/files/write", "files.write"),
    ("GET", "/api/instances/aaa/console", "console.read"),
    ("POST", "/api/instances/aaa/command", "console.command"),
    ("GET", "/api/instances/aaa/players", "server.view"),
    ("POST", "/api/instances/aaa/players/action", "players.manage"),
    ("GET", "/api/instances/aaa/backups", "server.view"),
    ("POST", "/api/instances/aaa/backups", "backups.make"),
    ("POST", "/api/instances/aaa/backups/s1/restore", "backups.restore"),
    ("DELETE", "/api/instances/aaa", "server.delete"),
    ("POST", "/api/instances/aaa/action/start", "server.power"),
]
for method, path, want in pairs:
    got, _ = policy.required(method, path)
    check(f"{method} {path} needs {want}", got == want, got)

check("every per-server rule is scoped",
      all(policy.required(m, p)[1] for m, p, _ in pairs))
check("a global action is not scoped",
      policy.required("POST", "/api/install/modpack")[1] is False)

# --- 4. the server id is found for the scope check ---------------------
check("the server id is pulled out of the path",
      policy.server_id_of("/api/instances/abc123/files/read") == "abc123")
check("...and is None when there is not one",
      policy.server_id_of("/api/health") is None)

# --- 5. the policy is reachable for each role --------------------------
auth.reset_for_tests()
for leftover in pathlib.Path(_TMP).glob("*.json"):
    leftover.unlink()
auth.bootstrap()
auth.create_user(username="op", password="operator long password",
                 role="operator", must_change=False, scope={"all": True})
auth.create_user(username="look", password="guest long password",
                 role="guest", must_change=False, scope={"all": True})
admin = auth.find_user("admin")
op = auth.find_user("op")
guest = auth.find_user("look")


def allowed(user, method, path):
    perm, scoped = policy.required(method, path)
    if perm == policy.DENY:
        return False
    if perm == policy.PUBLIC:
        return True
    if perm == policy.ADMIN:
        return user.get("role") == "admin"
    if perm != policy.ANY and not auth.may(user, perm):
        return False
    if scoped and not auth.in_scope(user, policy.server_id_of(path)):
        return False
    return True


check("a guest can read the console",
      allowed(guest, "GET", "/api/instances/aaa/console"))
check("a guest cannot send a command",
      not allowed(guest, "POST", "/api/instances/aaa/command"))
check("a guest cannot write a file",
      not allowed(guest, "POST", "/api/instances/aaa/files/write"))
check("a guest cannot delete a server",
      not allowed(guest, "DELETE", "/api/instances/aaa"))
check("a guest cannot reach user administration",
      not allowed(guest, "GET", "/api/admin/users"))
check("an operator can restart", allowed(op, "POST", "/api/instances/aaa/action/restart"))
check("an operator cannot delete a server",
      not allowed(op, "DELETE", "/api/instances/aaa"))
check("an operator cannot reach the audit log",
      not allowed(op, "GET", "/api/admin/audit"))
check("an admin can do all of it",
      all(allowed(admin, m, p) for m, p in [
          ("DELETE", "/api/instances/aaa"), ("GET", "/api/admin/audit"),
          ("POST", "/api/admin/users"), ("POST", "/api/instances/aaa/command")]))

# --- 6. scope is enforced separately from permission -------------------
auth.update_user("op", scope={"servers": ["aaa"]})
op = auth.find_user("op")
check("an operator may restart the server they are given",
      allowed(op, "POST", "/api/instances/aaa/action/restart"))
check("...and may NOT restart one they are not, with the same permission",
      not allowed(op, "POST", "/api/instances/zzz/action/restart"))
check("scope does not block a global action",
      allowed(op, "POST", "/api/roulette/roll"))

passed = sum(out)
print(f"\n{passed}/{len(out)} checks passed")
sys.exit(0 if passed == len(out) else 1)
