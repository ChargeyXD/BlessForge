"""Alembic's entry point.

Two ways in, one schema:

  * from the app (app/db/__init__.py `migrate`): a live connection is handed
    over in `config.attributes["connection"]` and used as-is;
  * from the CLI: an async engine is built from DATABASE_URL (or the same
    SQLite fallback the app uses) and run here.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context

from app.db import default_url
from app.db.models import Base

config = context.config
if config.config_file_name and not config.attributes.get("connection"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER most things; batch mode rebuilds the table.
        render_as_batch=connection.dialect.name == "sqlite",
    )


def run_with(connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(default_url())
    async with engine.begin() as conn:
        await conn.run_sync(run_with)
    await engine.dispose()


def run_offline() -> None:
    context.configure(url=default_url(), target_metadata=target_metadata,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_offline()
elif config.attributes.get("connection") is not None:
    run_with(config.attributes["connection"])
else:
    asyncio.run(run_async())
