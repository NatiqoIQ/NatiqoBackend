"""Async SQLAlchemy engine, session factory and transaction handling.

The unit of work (:class:`UnitOfWork`) is the *only* sanctioned way for application
code to reach the database. It owns exactly one transaction, exposes the shared
:class:`~app.core.uow.UnitOfWorkProtocol` port, and commits or rolls back as a unit
so that a failure half-way through a lesson completion can never leave progress,
XP, streak and achievements inconsistent (plan §53).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.base import Base
from app.core.config import get_settings
from app.core.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork

logger = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _create_engine() -> AsyncEngine:
    settings = get_settings()
    url = settings.sqlalchemy_dsn
    is_sqlite = url.startswith("sqlite")
    #: An in-memory SQLite database exists only inside the connection that opened it.
    #: With the default pool, the next checkout gets a *different* empty database, so
    #: a schema created on one connection is invisible to the next — the classic
    #: "works in one session, table not found in the next" failure. A single shared
    #: connection is the only way the URL means what it says.
    is_memory_sqlite = is_sqlite and ":memory:" in url
    kwargs: dict[str, Any] = {
        "echo": settings.database_echo,
        "future": True,
        "pool_pre_ping": True,
    }
    if is_memory_sqlite:
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    if not is_sqlite:
        kwargs.update(
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout,
            pool_recycle=settings.database_pool_recycle,
            #: Enforced by PostgreSQL itself, so they also apply to queries the ORM
            #: never sees (a migration, a psql session, a runaway lock wait). A
            #: statement timeout bounds how long a backend can pin a pooled
            #: connection; the lock timeout bounds how long it waits behind another
            #: transaction. Without both, one slow query is indistinguishable from a
            #: database outage to everyone queued behind it.
            connect_args={
                "server_settings": {
                    "application_name": "natiqo-api",
                    "statement_timeout": str(settings.database_statement_timeout_ms),
                    "lock_timeout": str(settings.database_lock_timeout_ms),
                    "idle_in_transaction_session_timeout": str(
                        settings.database_idle_transaction_timeout_ms
                    ),
                }
            },
        )
    engine = create_async_engine(url, **kwargs)
    if is_sqlite:
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def get_engine() -> AsyncEngine:
    """Process-wide async engine. Created lazily so import stays side-effect free."""
    global _engine
    if _engine is None:
        _engine = _create_engine()
        logger.debug("database.engine_created", dialect=_engine.dialect.name)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
    return _session_factory


async def dispose_engine() -> None:
    """Close pooled connections. Called on application shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("database.engine_disposed")
    _engine = None
    _session_factory = None


async def create_all() -> None:
    """Create the schema directly. Used by tests and local bootstrapping only.

    Production schema changes must go through Alembic (``alembic upgrade head``).
    """
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def drop_all() -> None:
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


async def check_database() -> bool:
    """Readiness probe: can we actually round-trip a query?"""
    from sqlalchemy import text

    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        logger.warning("database.check_failed", error=str(exc))
        return False


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Standalone transactional scope for workers, scripts and background jobs."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def make_uow() -> SqlAlchemyUnitOfWork:
    """Build a unit of work bound to a fresh session."""
    from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork

    return SqlAlchemyUnitOfWork(get_session_factory())
