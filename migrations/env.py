"""Alembic async migration environment.

Integration fix applied (PR#22 merge pass):
    GAP-10: PERPLEXITY_API_KEY and API_SECRET_KEY are set to placeholder
            defaults before Settings() is instantiated so that alembic
            upgrade head does not require runtime secrets in CI/CD.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

os.environ.setdefault("PERPLEXITY_API_KEY", "alembic-placeholder")
os.environ.setdefault("API_SECRET_KEY", "alembic-placeholder")

from app.core.config import get_settings
from app.services.pg_models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Configure and run, in one sync callable, inside one transaction.

    Both halves must share the same sync Connection, and `run_migrations()`
    must be wrapped in `context.begin_transaction()` — that block is what
    commits. Without it `alembic upgrade head` logs "Running upgrade -> ..."
    and then rolls the DDL back at connection exit, so the command reports
    success against a database where nothing was created. Splitting configure
    and run across two `run_sync` calls hides the same bug, because the second
    callable discards the connection it is handed.

    Shape follows alembic's own templates/async/env.py.
    """
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(get_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
