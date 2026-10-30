"""Alembic environment.

Two things here are load-bearing:

1. **The DSN comes from application settings, not from ``alembic.ini``.** A migration
   that runs against a different database than the app is the worst class of migration
   bug: it succeeds, it is green, and it has done nothing.

2. **Every model module is imported before autogenerate runs** (via
   :func:`app.core.registry.import_all_models`). A model that is never imported is
   absent from ``Base.metadata``, and autogenerate responds by emitting a ``DROP
   TABLE`` for it. The registry exists to make that list reviewable.

Online migrations run through an async engine because the application uses
``asyncpg``; ``run_sync`` bridges Alembic's sync API onto the async driver.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.base import Base
from app.core.config import get_settings
from app.core.registry import import_all_models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Importing the model modules is what fills ``Base.metadata``. The return value is
# deliberately discarded; the import itself is the side effect we need.
import_all_models()

target_metadata = Base.metadata


def _is_sqlite(connection: Connection) -> bool:
    # Ask the dialect, not the URL: an async dialect exposes no ``url`` attribute, and
    # ``dialect.name`` is what actually decides whether batch mode is needed.
    return connection.dialect.name == "sqlite"


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``--sql`` mode).

    Uses the *sync* DSN because the output is consumed by a separate tool
    (``psql``, a migration runner) rather than by this process.
    """
    settings = get_settings()
    context.configure(
        url=settings.sync_postgres_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place, so batch mode recreates the table
        # under a temporary name instead. Harmless on PostgreSQL, essential if anyone
        # runs migrations against a SQLite file.
        render_as_batch=_is_sqlite(connection),
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        #: One migration at a time, cluster-wide. The API used to run ``alembic upgrade
        #: head`` on every container start, so a rolling deploy had several replicas
        #: racing the same DDL: both read the current revision, both try to create the
        #: same table, one wins and the other crashes — or worse, half-applies a
        #: migration. A transaction-scoped advisory lock serialises them and is released
        #: automatically on commit, rollback or crash, so no stale lock can survive.
        if not _is_sqlite(connection):
            connection.exec_driver_sql("SELECT pg_advisory_xact_lock(7592001)")
        context.run_migrations()


async def _run_async_migrations() -> None:
    settings = get_settings()
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = settings.sqlalchemy_dsn

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
