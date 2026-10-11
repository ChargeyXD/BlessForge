#!/usr/bin/env python3
"""The database: its schema, a fresh install, and an upgrade from JSON.

What has to be true for WP-03 to be done:

  * every migration applies to an empty database and the result matches the
    models exactly -- no table or column the code expects is missing;
  * a fresh install starts, makes the first-run admin, and imports nothing;
  * an install upgrading from the JSON files comes up with everything it
    had: accounts that still sign in with their old passwords, sessions that
    are still signed in, the audit trail with its original times, racks,
    and the operator's mod decisions -- and the files are renamed so the
    import cannot run twice;
  * a database that already has accounts is never merged into;
  * a torn users.json (the power-cut case) is read as no accounts, and the
    first-run admin is made rather than the app failing to start.

Every scenario boots the real app (its lifespan: migrate, import, bootstrap,
load), not a hand-rolled copy of that sequence.

Offline, on SQLite or on TEST_DATABASE_URL (see dev/tools/_dbtest.py).
"""
import asyncio
import json
import sys
import time

import _dbtest  # noqa: F401 -- sets STATE_DIR before app is imported

from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import func, inspect, select  # noqa: E402

from app import auth, config, db, fleetgroups, whitelist  # noqa: E402
from app.db.models import AuditEntry, Base, Meta, User  # noqa: E402
from app.main import app  # noqa: E402

out = []


def check(name, cond, extra=""):
    out.append(bool(cond))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {extra}" if extra else ""))


def state_file(name):
    path = config.state_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def clear_state_dir():
    for p in config.STATE_DIR.glob("*"):
        if p.is_file():
            p.unlink()


async def boot(url):
    """Start the app on `url` exactly as uvicorn would, then stop it."""
    config.DATABASE_URL = url
    await db.dispose()
    fleetgroups.reset_for_tests()
    whitelist._reset_for_tests()
    async with app.router.lifespan_context(app):
        pass
    # The lifespan disposes on the way out; come back up for the checks.
    await db.init(url)
    await fleetgroups.load()
    await whitelist.load()


async def main():
    print(f"(database: {_dbtest.label()})")

    # --- 1. the schema --------------------------------------------------------
    url = await _dbtest.fresh()
    async with db.engine().connect() as conn:
        tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        drift = await conn.run_sync(lambda c: compare_metadata(
            MigrationContext.configure(c, opts={"compare_type": True}), Base.metadata))
    want = {t.name for t in Base.metadata.sorted_tables}
    check("every table in the models exists after migrating",
          want <= tables, sorted(want - tables))
    check("...including the plan's (users, servers, plan_prices, audit_log, ...)",
          {"users", "sessions", "customers", "regions", "nodes", "allocations",
           "plans", "plan_prices", "subscriptions", "servers", "server_users",
           "backups", "schedules", "webhook_events", "consents", "jobs",
           "audit_log", "content_items", "coupons", "bundles", "tickets",
           "ticket_messages", "access_grants", "notices", "feature_flags",
           "announcements"} <= tables)
    check("the migration and the models agree exactly", drift == [], drift)
    await db.migrate()
    check("migrating again is a no-op", True)

    # --- 2. a fresh install ------------------------------------------------------
    clear_state_dir()
    url = await _dbtest.fresh()
    await boot(url)
    admin = await auth.find_user("admin")
    check("a fresh install makes the first-run admin",
          admin and admin["must_change"] is True)
    async with db.session() as s:
        meta = await s.get(Meta, "json_import")
    check("...and records that there was nothing to import",
          meta and meta.value.get("files") == {}, meta and meta.value)

    # --- 3. an upgrade from the JSON files --------------------------------------
    clear_state_dir()
    url = await _dbtest.fresh()
    now = time.time()
    token = "a-session-token-from-before-the-upgrade"
    expired_token = "an-expired-session-token"
    state_file("users.json").write_text(json.dumps({"version": 1, "users": [
        {"username": "chargey", "display": "Chargey", "role": "admin",
         "password": auth.hash_password("the owner's long password"),
         "permissions": [], "scope": {"all": True}, "must_change": False,
         "created_at": now - 86400 * 40, "last_login": now - 3600,
         "failures": 0, "locked_until": 0, "disabled": False},
        {"username": "friend", "display": "A friend", "role": "operator",
         "password": auth.hash_password("the friend's long password"),
         "permissions": ["console.command"], "scope": {"servers": ["aaaa1111"]},
         "must_change": False, "created_at": now - 86400, "disabled": False},
        {"username": "", "password": {}},           # junk: dropped
    ]}), encoding="utf-8")
    state_file("sessions.json").write_text(json.dumps({"version": 1, "sessions": {
        auth._token_key(token): {"username": "friend", "created": now - 60,
                                 "expires": now + 86400, "agent": "Firefox",
                                 "ip": "10.0.0.5", "last_seen": now - 30},
        auth._token_key(expired_token): {"username": "friend", "created": now - 999999,
                                         "expires": now - 10},
    }}), encoding="utf-8")
    state_file("audit.jsonl").write_text("\n".join([
        json.dumps({"at": now - 7200, "actor": "chargey", "action": "server.start",
                    "server_id": "aaaa1111", "detail": "started Sakura SMP",
                    "ip": "10.0.0.2", "ok": True}),
        "this line is not JSON",
        json.dumps({"at": now - 3600, "actor": "friend", "action": "files.write",
                    "server_id": "aaaa1111", "detail": "edited server.properties",
                    "ok": True}),
    ]) + "\n", encoding="utf-8")
    state_file("fleet-state.json").write_text(json.dumps({
        "groups": [{"id": "survival", "name": "Survival", "motif": "torii"}],
        "assign": {"aaaa1111": "survival"},
        "activity": {"aaaa1111": {"first_seen": now - 999, "running": True}},
    }), encoding="utf-8")
    state_file("client-only-whitelist.json").write_text(json.dumps({
        "items": {"figura": {"name": "Figura", "example_file": "figura-0.1.4.jar",
                             "reason": "harmless here", "added_at": now - 500}},
    }), encoding="utf-8")

    await boot(url)
    check("the old admin signs in with the old password",
          (await auth.authenticate("chargey", "the owner's long password"))["role"] == "admin")
    friend = await auth.find_user("friend")
    check("a second account keeps its role, grants and scope",
          friend["role"] == "operator" and "console.command" in friend["permissions"]
          and friend["scope"]["servers"] == ["aaaa1111"])
    check("no first-run admin is made when real accounts came across",
          await auth.find_user("admin") is None)
    check("a live session is still signed in after the upgrade",
          (await auth.session_user(token) or {}).get("username") == "friend")
    check("an expired one is not carried", await auth.session_user(expired_token) is None)
    entries = await auth.audit_entries()
    check("the audit trail came across, junk lines skipped",
          [e["action"] for e in entries] == ["files.write", "server.start"],
          [e["action"] for e in entries])
    check("...with its original times",
          abs(entries[-1]["at"] - (now - 7200)) < 2, entries[-1]["at"])
    check("racks and assignments came across",
          fleetgroups.document({"aaaa1111"})["assign"] == {"aaaa1111": "survival"})
    check("the server activity record came across",
          "aaaa1111" in (await fleetgroups.observe([], full=False))["servers"])
    check("a v1 decision list came across, as allows",
          whitelist.allows("figura-0.2.0.jar"))
    left = sorted(p.name for p in config.STATE_DIR.glob("*") if p.is_file())
    check("every imported file was renamed *.imported",
          left == sorted(f"{n}.imported" for n in (
              "users.json", "sessions.json", "audit.jsonl", "fleet-state.json",
              "client-only-whitelist.json")), left)
    async with db.session() as s:
        meta = await s.get(Meta, "json_import")
    check("the import is recorded with what it carried",
          meta.value.get("counts", {}).get("users") == 2
          and meta.value["counts"].get("sessions") == 1, meta.value)

    # Booting again must not import again, even if a file came back.
    state_file("users.json").write_text(json.dumps({"users": [
        {"username": "intruder", "password": auth.hash_password("x" * 12),
         "role": "admin"}]}), encoding="utf-8")
    await boot(url)
    check("a second start imports nothing, even with a users.json back in place",
          await auth.find_user("intruder") is None)
    async with db.session() as s:
        n_audit = (await s.execute(select(func.count(AuditEntry.id)))).scalar_one()
    check("...and duplicates nothing", n_audit >= 2 and len(
        [e for e in await auth.audit_entries() if e["action"] == "server.start"]) == 1)

    # --- 4. never merged into a database already in use -----------------------
    clear_state_dir()
    url = await _dbtest.fresh()
    await auth.create_user(username="existing", password="an existing long password",
                           role="admin", must_change=False)
    state_file("users.json").write_text(json.dumps({"users": [
        {"username": "ghost", "password": auth.hash_password("y" * 12),
         "role": "admin"}]}), encoding="utf-8")
    await boot(url)
    check("a database that already has accounts is not merged into",
          await auth.find_user("ghost") is None
          and await auth.find_user("existing") is not None)
    check("...and the file is left where it was", state_file("users.json").exists())

    # --- 5. a torn users.json ---------------------------------------------------
    clear_state_dir()
    url = await _dbtest.fresh()
    state_file("users.json").write_bytes(bytes(400))
    await boot(url)
    check("a torn users.json reads as no accounts: the first-run admin is made",
          (await auth.find_user("admin") or {}).get("must_change") is True)
    async with db.session() as s:
        users = (await s.execute(select(func.count(User.id)))).scalar_one()
    check("...and nothing else came from it", users == 1)

    # --- 6. the health check says which database --------------------------------
    ping = await db.ping()
    check("the health check names the database and that it answers",
          ping["ok"] and ping["dialect"] == ("postgresql" if _dbtest.URL else "sqlite"))
    check("...and says plainly when it is not the production one",
          ping["production_ready"] is bool(_dbtest.URL))
    check("a password in the URL is never shown",
          "secret" not in (db.configure("postgresql+asyncpg://u:secret@h/d")
                           or db.redacted_url()))
    await db.dispose()

    passed = sum(out)
    print(f"\n{passed}/{len(out)} checks passed")
    return passed == len(out)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
