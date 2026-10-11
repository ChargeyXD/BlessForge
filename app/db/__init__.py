"""The database: Postgres in production, SQLite when nothing else is set.

`DATABASE_URL` names it (`postgresql+asyncpg://user:pass@host/db`). Left
blank, BlessForge falls back to a SQLite file under STATE_DIR so a node-less
dev run and the offline tests work with nothing installed -- the shipped
compose files always set a Postgres URL, and `/api/health` says which one is
in use so the fallback can never be mistaken for production.

The schema is owned by Alembic (`alembic/`). `init()` runs every migration
up to head on start, inside the app, so an upgrade is "pull the new image".

    from app import db
    async with db.session() as s:
        ...
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import pathlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (AsyncEngine, AsyncSession,
                                    async_sessionmaker, create_async_engine)

from app import config

log = logging.getLogger(__name__)

ROOT = pathlib.Path(__file__).resolve().parents[2]

_engine: AsyncEngine | None = None
_maker: async_sessionmaker[AsyncSession] | None = None
_url: str | None = None
_ready = False
_init_lock = asyncio.Lock()


def default_url() -> str:
    if config.DATABASE_URL:
        return config.DATABASE_URL
    path = config.STATE_DIR / "blessforge.db"
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def url() -> str:
    return _url or default_url()


def dialect() -> str:
    """"postgresql" or "sqlite"."""
    return url().split("+", 1)[0].split(":", 1)[0]


def redacted_url() -> str:
    """The URL with any password removed, for logs and the health check."""
    u = url()
    if "@" in u and "://" in u:
        scheme, rest = u.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return u


def configure(database_url: str | None = None) -> None:
    """Create the engine. Idempotent for the same URL; replaces it otherwise."""
    global _engine, _maker, _url, _ready
    wanted = database_url or default_url()
    if _engine is not None and wanted == _url:
        return
    _url = wanted
    _ready = False
    kwargs: dict = {"pool_pre_ping": True}
    if wanted.startswith("sqlite"):
        pathlib.Path(wanted.split("///", 1)[-1]).parent.mkdir(parents=True,
                                                             exist_ok=True)
    else:
        kwargs.update(pool_size=10, max_overflow=10)
    _engine = create_async_engine(wanted, **kwargs)
    if wanted.startswith("sqlite"):
        @event.listens_for(_engine.sync_engine, "connect")
        def _sqlite_pragmas(conn, _record):      # pragma: no cover - driver hook
            cur = conn.cursor()
            # SQLite ignores foreign keys unless asked, and its default
            # journal makes a reader wait on every writer.
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    _maker = async_sessionmaker(_engine, expire_on_commit=False)


def engine() -> AsyncEngine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """A session that commits on success and rolls back on any error."""
    if not _ready:
        await init()
    assert _maker is not None
    async with _maker() as s:
        try:
            yield s
            await s.commit()
        except BaseException:
            await s.rollback()
            raise


def _alembic_config(connection=None):
    from alembic.config import Config
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["connection"] = connection
    return cfg


def _upgrade(connection) -> None:
    from alembic import command
    command.upgrade(_alembic_config(connection), "head")


async def migrate() -> None:
    """Run every migration up to head, on this app's own connection."""
    async with engine().begin() as conn:
        await conn.run_sync(_upgrade)


async def init(database_url: str | None = None) -> None:
    """Configure, migrate, and mark ready. Safe to call more than once."""
    global _ready
    async with _init_lock:
        configure(database_url)
        if _ready:
            return
        await migrate()
        _ready = True
        log.info("database ready: %s", redacted_url())


async def dispose() -> None:
    """Close every pooled connection -- a test's "restart", or shutdown."""
    global _engine, _maker, _ready, _url
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _maker = None
    _ready = False
    _url = None


async def ping() -> dict:
    """For the health check: which database, and is it answering."""
    try:
        async with engine().connect() as conn:
            await conn.execute(text("select 1"))
        return {"ok": True, "dialect": dialect(), "url": redacted_url(),
                "production_ready": dialect() == "postgresql"}
    except Exception as e:      # noqa: BLE001 -- reported, not raised
        return {"ok": False, "dialect": dialect(), "url": redacted_url(),
                "error": str(e)[:300]}


# --- time at the boundary ------------------------------------------------------
#
# The app and the front end speak epoch seconds; the database speaks
# timezone-aware timestamps. These two are the only place the two meet.


def epoch(value: dt.datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:            # SQLite returns naive; it was UTC
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.timestamp()


def from_epoch(value: float | int | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
