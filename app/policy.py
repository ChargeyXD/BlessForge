"""Which permission each API route needs, in one place.

132 routes need gating. Doing that with a decorator per route means the
answer to "what can a guest actually reach" is spread across 2,900 lines
of `main.py` and nobody can audit it. This is the whole policy as one
ordered table, and `dev/tools/test_policy.py` asserts that every route the
app serves is matched by a rule -- so adding a route without deciding who
may call it is a test failure, not a silent hole.

Two independent questions
-------------------------
**May this user do this kind of thing at all** -- the permission.
**May they do it to THIS server** -- the scope.

They are separate because collapsing them means a permission list per
server, which nobody maintains. A rule names a permission; the server id
is pulled out of the path and checked against the user's scope by the
caller.

Fail closed
-----------
`required()` returns `DENY` for anything it does not recognise. A new
route is unreachable until someone adds a rule, which is the safe
direction to be wrong in: a locked-out admin files a bug, an open
endpoint does not.
"""
from __future__ import annotations

import re
from typing import NamedTuple

# Sentinels. `PUBLIC` needs no session at all; `ANY` needs a session but no
# particular permission; `ADMIN` needs the admin role; `DENY` is the
# fail-closed default.
PUBLIC = "@public"
ANY = "@any"
ADMIN = "@admin"
DENY = "@deny"


class Rule(NamedTuple):
    method: str          # "*" for any
    pattern: re.Pattern
    permission: str
    scoped: bool         # does the {server_id} in the path have to be in scope


def _r(method: str, path: str, permission: str, scoped: bool = False) -> Rule:
    """Build a rule from a FastAPI-style path.

    `{name}` becomes a segment wildcard, so the table reads like the route
    table it mirrors rather than like a wall of regex.
    """
    escaped = re.escape(path)
    escaped = re.sub(r"\\\{[a-z_]+\\\}", r"[^/]+", escaped)
    return Rule(method.upper(), re.compile(f"^{escaped}$"), permission, scoped)


# Order matters: the first match wins, so the specific rules come before
# the catch-alls for the same prefix.
RULES: list[Rule] = [
    # --- open, because they are what the login screen itself needs ------
    _r("GET", "/api/healthz", PUBLIC),
    _r("POST", "/api/auth/login", PUBLIC),
    _r("GET", "/api/auth/session", PUBLIC),     # answers "are you logged in"
    _r("POST", "/api/auth/logout", ANY),
    _r("POST", "/api/auth/password", ANY),      # changing your OWN password

    # --- admin only ----------------------------------------------------
    _r("*", "/api/admin/users", ADMIN),
    _r("*", "/api/admin/users/{username}", ADMIN),
    _r("*", "/api/admin/users/{username}/password", ADMIN),
    _r("*", "/api/admin/users/{username}/sessions", ADMIN),
    _r("GET", "/api/admin/audit", ADMIN),
    _r("GET", "/api/admin/audit/actions", ADMIN),
    _r("GET", "/api/admin/permissions", ADMIN),

    # --- reading the fleet ---------------------------------------------
    _r("GET", "/api/health", ANY),
    _r("GET", "/api/instances", ANY),
    _r("POST", "/api/fleet/seen", ANY),
    _r("GET", "/api/fleet/groups", ANY),
    _r("POST", "/api/fleet/groups", "racks.manage"),
    _r("POST", "/api/fleet/groups/plan", "racks.manage"),
    _r("DELETE", "/api/fleet/groups/{group_id}", "racks.manage"),
    _r("POST", "/api/fleet/assign", "racks.manage"),

    # --- one server: power ---------------------------------------------
    _r("GET", "/api/instances/{server_id}", "server.view", True),
    _r("DELETE", "/api/instances/{server_id}", "server.delete", True),
    _r("POST", "/api/instances/{server_id}/action/{action}", "server.power", True),
    _r("GET", "/api/instances/{server_id}/stats", "server.view", True),
    _r("GET", "/api/instances/{server_id}/port", "server.view", True),
    _r("POST", "/api/instances/{server_id}/port", "tune.apply", True),

    # --- console --------------------------------------------------------
    _r("GET", "/api/instances/{server_id}/console", "console.read", True),
    _r("GET", "/api/instances/{server_id}/console/stream", "console.read", True),
    _r("GET", "/api/instances/{server_id}/logs", "console.read", True),
    _r("POST", "/api/instances/{server_id}/command", "console.command", True),

    # --- files ----------------------------------------------------------
    _r("GET", "/api/instances/{server_id}/files", "files.read", True),
    _r("GET", "/api/instances/{server_id}/files/read", "files.read", True),
    _r("GET", "/api/instances/{server_id}/files/download", "files.read", True),
    _r("GET", "/api/instances/{server_id}/files/search", "files.read", True),
    _r("GET", "/api/instances/{server_id}/files/usage", "files.read", True),
    _r("POST", "/api/instances/{server_id}/files/write", "files.write", True),
    _r("POST", "/api/instances/{server_id}/files/create", "files.write", True),
    _r("POST", "/api/instances/{server_id}/files/delete", "files.write", True),
    _r("POST", "/api/instances/{server_id}/files/rename", "files.write", True),
    _r("POST", "/api/instances/{server_id}/files/extract", "files.write", True),
    _r("POST", "/api/instances/{server_id}/files/upload", "files.write", True),

    # --- mods and plugins -----------------------------------------------
    _r("GET", "/api/instances/{server_id}/mods", "server.view", True),
    _r("GET", "/api/instances/{server_id}/mods/updates", "server.view", True),
    _r("GET", "/api/instances/{server_id}/mods/dependencies", "server.view", True),
    _r("POST", "/api/instances/{server_id}/mods/icons", "server.view", True),
    _r("POST", "/api/instances/{server_id}/mods/identify", "server.view", True),
    _r("POST", "/api/instances/{server_id}/mods/resolve", "mods.manage", True),
    _r("POST", "/api/instances/{server_id}/mods/add", "mods.manage", True),
    _r("POST", "/api/instances/{server_id}/mods/toggle", "mods.manage", True),
    _r("POST", "/api/instances/{server_id}/mods/bulk-toggle", "mods.manage", True),
    _r("POST", "/api/instances/{server_id}/mods/delete", "mods.manage", True),
    _r("POST", "/api/instances/{server_id}/plugins/add", "mods.manage", True),
    _r("POST", "/api/instances/{server_id}/plugins/resolve", "mods.manage", True),
    _r("GET", "/api/instances/{server_id}/plugins/audit", "server.view", True),
    _r("POST", "/api/instances/{server_id}/switch-pack-version", "mods.manage", True),
    _r("POST", "/api/instances/{server_id}/client-scan", "server.view", True),
    _r("POST", "/api/instances/{server_id}/client-scan/apply", "mods.manage", True),

    # --- configs --------------------------------------------------------
    _r("GET", "/api/instances/{server_id}/configs", "files.read", True),
    _r("GET", "/api/instances/{server_id}/configs/read", "files.read", True),
    _r("POST", "/api/instances/{server_id}/configs/write", "config.edit", True),
    _r("GET", "/api/instances/{server_id}/properties", "files.read", True),
    _r("POST", "/api/instances/{server_id}/properties", "config.edit", True),

    # --- players --------------------------------------------------------
    _r("GET", "/api/instances/{server_id}/players", "server.view", True),
    _r("POST", "/api/instances/{server_id}/players/action", "players.manage", True),
    _r("POST", "/api/instances/{server_id}/players/bulk", "players.manage", True),
    _r("POST", "/api/instances/{server_id}/players/note", "players.manage", True),
    _r("POST", "/api/instances/{server_id}/players/whitelist-mode",
       "players.manage", True),

    # --- backups --------------------------------------------------------
    _r("GET", "/api/instances/{server_id}/backups", "server.view", True),
    _r("POST", "/api/instances/{server_id}/backups", "backups.make", True),
    _r("POST", "/api/instances/{server_id}/backups/{snap_id}/restore",
       "backups.restore", True),

    # --- diagnosis, tuning, repair --------------------------------------
    _r("GET", "/api/instances/{server_id}/diagnose", "server.view", True),
    _r("GET", "/api/instances/{server_id}/crash-review", "server.view", True),
    _r("POST", "/api/instances/{server_id}/deep-scan", "server.view", True),
    _r("POST", "/api/instances/{server_id}/smoke-test", "server.power", True),
    _r("GET", "/api/instances/{server_id}/optimize", "server.view", True),
    _r("POST", "/api/instances/{server_id}/optimize", "tune.apply", True),
    _r("POST", "/api/instances/{sid}/optimize/advice", "server.view", True),
    _r("POST", "/api/instances/{server_id}/fix/accept-eula", "config.edit", True),
    _r("POST", "/api/instances/{server_id}/fix/java", "tune.apply", True),
    _r("POST", "/api/instances/{server_id}/fix/set-ram", "tune.apply", True),
    _r("POST", "/api/instances/{server_id}/fix/versions", "mods.manage", True),
    _r("POST", "/api/instances/{server_id}/loader/reinstall", "tune.apply", True),
    _r("POST", "/api/instances/{server_id}/export", "files.read", True),

    # The assistant can change a server, so it needs what it would change.
    _r("POST", "/api/instances/{server_id}/ai/analyse", "server.view", True),
    _r("POST", "/api/instances/{server_id}/ai/crash-review", "server.view", True),
    _r("POST", "/api/instances/{server_id}/ai/apply", "tune.apply", True),
    _r("POST", "/api/instances/{server_id}/ai/autofix", "tune.apply", True),

    # --- making servers -------------------------------------------------
    _r("POST", "/api/install/modpack", "server.create"),
    _r("POST", "/api/install/preflight", "server.create"),
    _r("POST", "/api/provision/server", "server.create"),

    # --- the catalogue: read-only, and useless without somewhere to put
    #     what you find, so any signed-in user may browse ----------------
    _r("GET", "/api/browse/modpacks", ANY),
    _r("GET", "/api/browse/mods", ANY),
    _r("GET", "/api/browse/plugins", ANY),
    _r("GET", "/api/modpacks/{mod_id}", ANY),
    _r("GET", "/api/modpacks/{mod_id}/files", ANY),
    _r("GET", "/api/modpacks/{mod_id}/description", ANY),
    _r("GET", "/api/mods/{source}/{project_id}/versions", ANY),
    _r("GET", "/api/mods/{source}/{project_id}/detail", ANY),
    _r("GET", "/api/plugins/{project_id}/versions", ANY),
    _r("GET", "/api/plugins/meta", ANY),
    _r("GET", "/api/plugins/starter", ANY),
    _r("GET", "/api/loaders", ANY),
    _r("GET", "/api/loaders/{family}", ANY),
    _r("GET", "/api/meta/minecraft-versions", ANY),
    _r("GET", "/api/meta/categories", ANY),
    _r("GET", "/api/host/specs", ANY),
    _r("GET", "/api/players/lookup", ANY),
    _r("POST", "/api/diagnose/dependency", ANY),
    _r("GET", "/api/diagnose/dependency/{mod_id}", ANY),
    _r("GET", "/api/browse/modpacks/modrinth", ANY),
    _r("GET", "/api/modpacks/{mod_id}/files/{file_id}/plan", ANY),
    _r("GET", "/api/loaders/{family}/versions", ANY),

    # --- roulette -------------------------------------------------------
    _r("GET", "/api/roulette/meta", ANY),
    _r("POST", "/api/roulette/roll", "roulette.use"),
    _r("POST", "/api/roulette/reroll", "roulette.use"),
    _r("POST", "/api/roulette/pool", "roulette.use"),
    _r("POST", "/api/roulette/export", "roulette.use"),
    _r("GET", "/api/roulette/export/{roll_id}", "roulette.use"),
    _r("POST", "/api/roulette/preview-export", "roulette.use"),
    _r("POST", "/api/roulette/install", "server.create"),

    # --- jobs: a job is followed by whoever started it, and the id is
    #     unguessable, so a session is the bar ---------------------------
    _r("GET", "/api/jobs", ANY),
    _r("GET", "/api/jobs/{job_id}", ANY),
    _r("GET", "/api/jobs/{job_id}/events", ANY),
    _r("POST", "/api/jobs/{job_id}/cancel", ANY),

    # --- uploads, exports, cache ---------------------------------------
    _r("GET", "/api/uploads", "server.create"),
    _r("POST", "/api/uploads/modpack", "server.create"),
    _r("DELETE", "/api/uploads/{upload_id}", "server.create"),
    _r("GET", "/api/exports/{filename}", ANY),
    _r("GET", "/api/cache", ADMIN),
    _r("POST", "/api/cache/prune", ADMIN),
    _r("GET", "/api/updates", ANY),
    _r("POST", "/api/updates/check", ANY),

    # --- the client-only decision list ---------------------------------
    _r("GET", "/api/whitelist", ANY),
    _r("POST", "/api/whitelist", "mods.manage"),
    _r("DELETE", "/api/whitelist/{key}", "mods.manage"),
    _r("POST", "/api/whitelist/check", ANY),
    _r("POST", "/api/whitelist/clear", "mods.manage"),
    _r("GET", "/api/whitelist/export", ANY),
    _r("POST", "/api/whitelist/import", "mods.manage"),

    # --- the assistant's own settings ----------------------------------
    _r("GET", "/api/ai/status", ANY),
    _r("GET", "/api/ai/models", ANY),
    _r("GET", "/api/ai/endpoints", ANY),
    _r("GET", "/api/ai/tuning", ANY),
    _r("POST", "/api/ai/endpoint", ADMIN),
    _r("POST", "/api/ai/pull", ADMIN),
    _r("POST", "/api/ai/warm", ADMIN),
    _r("POST", "/api/ai/tuning", ADMIN),
]

# Where the server id sits in a path, for the scope check.
_SERVER_ID = re.compile(r"^/api/instances/([^/]+)")


def server_id_of(path: str) -> str | None:
    m = _SERVER_ID.match(path or "")
    return m.group(1) if m else None


def required(method: str, path: str) -> tuple[str, bool]:
    """(permission, scoped) for one request. DENY if nothing matches."""
    method = (method or "GET").upper()
    path = (path or "").rstrip("/") or "/"
    for rule in RULES:
        if rule.method not in ("*", method):
            continue
        if rule.pattern.match(path):
            return rule.permission, rule.scoped
    return DENY, False
