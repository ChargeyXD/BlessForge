"""A clean database for a test, on whichever engine the run asks for.

    python dev/tools/test_auth.py                         # SQLite, in a temp dir
    TEST_DATABASE_URL=postgresql+asyncpg://u@host/db \\
        python dev/tools/test_auth.py                     # a real Postgres

With TEST_DATABASE_URL set, `fresh()` DROPS EVERY TABLE in that database's
public schema before migrating it back up, so point it at a database that
exists only for tests. `restart()` is a process restart as far as the app
can tell: every pooled connection closed and the engine rebuilt.

Import this before anything from `app`, so STATE_DIR is a temp directory and
a test can never touch a real /data.
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="bf-db-"))
os.environ["STATE_DIR"] = str(TMP / "state")
os.environ.setdefault("DATA_DIR", str(TMP))
os.environ.setdefault("CACHE_DIR", str(TMP / "cache"))
os.environ["DATABASE_URL"] = ""          # never inherit a real one

URL = os.environ.get("TEST_DATABASE_URL", "").strip()


def label() -> str:
    return "postgres" if URL.startswith("postgresql") else "sqlite"


_n = 0


async def fresh() -> str:
    """An empty, fully migrated database. Returns its URL."""
    global _n
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from app import db

    await db.dispose()
    _n += 1
    if URL:
        eng = create_async_engine(URL)
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await eng.dispose()
        url = URL
    else:
        path = TMP / f"test-{_n}.db"
        url = f"sqlite+aiosqlite:///{path.as_posix()}"
    await db.init(url)
    return url


async def restart(url: str) -> None:
    """Close everything and come back up on the same database."""
    from app import db
    await db.dispose()
    await db.init(url)
