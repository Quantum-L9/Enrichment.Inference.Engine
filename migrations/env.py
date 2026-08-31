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
from typing import Any

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


def _do_run_migrations(connection: Any) -> None:
    """Configure and run inside ONE sync callable, wrapped in a transaction.

    Both properties matter and neither was present before.

    `context.begin_transaction()` is what commits: SQLAlchemy 2.0 connections
    are commit-as-you-go, so DDL emitted on a bare `engine.connect()` is rolled
    back when the connection closes. `alembic upgrade` therefore logged
    "Running upgrade -> 001" and left the database empty — a silent no-op, and
    the worst possible failure mode for a migration runner, since it reports
    success while the schema never moves.

    Configure and run also have to share one `run_sync` call. Splitting them
    left the second callable relying on module-level context state carried over
    from the first, which is not a contract greenlet-bridged calls make.
    """
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(get_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
