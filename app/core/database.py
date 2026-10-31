"""Async SQLAlchemy engine, session factory and transaction handling.

The unit of work (:class:`UnitOfWork`) is the *only* sanctioned way for application
code to reach the database. It owns exactly one transaction, exposes the shared
:class:`~app.core.uow.UnitOfWorkProtocol` port, and commits or rolls back as a unit
so that a failure half-way through a lesson completion can never leave progress,
XP, streak and achievements inconsistent (plan §53).
"""

from __future__ import annotations

import time
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
_read_engine: AsyncEngine | None = None
_read_session_factory: async_sessionmaker[AsyncSession] | None = None

#: Last measured replica replay delay and when it was measured (monotonic seconds). Kept
#: at module level so every request and the ``/metrics`` renderer share one probe.
_lag_seconds: float = 0.0
_lag_checked_at: float = float("-inf")
_lag_known: bool = False

#: PostgreSQL's replay delay. ``pg_last_xact_replay_timestamp()`` is NULL when the replica
#: has never replayed a transaction, which means "no lag", not "unknown".
_REPLICA_LAG_SQL = "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))"


def _create_engine(*, read: bool = False) -> AsyncEngine:
    settings = get_settings()
    url = settings.read_sqlalchemy_dsn if read else settings.sqlalchemy_dsn
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
                    "application_name": "natiqo-api-read" if read else "natiqo-api",
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


def get_read_engine() -> AsyncEngine:
    """Engine for lag-tolerant reads, or the primary when no replica is configured.

    Sharing the primary when the DSNs are equal is not an optimisation detail: a second
    pool against the same database would double the connection count for no benefit.
    """
    global _read_engine
    settings = get_settings()
    if not settings.database_read_url or settings.read_sqlalchemy_dsn == settings.sqlalchemy_dsn:
        return get_engine()
    if _read_engine is None:
        _read_engine = _create_engine(read=True)
        logger.info("database.read_engine_created", dialect=_read_engine.dialect.name)
    return _read_engine


def get_read_session_factory() -> async_sessionmaker[AsyncSession]:
    global _read_session_factory
    settings = get_settings()
    if not settings.database_read_url or settings.read_sqlalchemy_dsn == settings.sqlalchemy_dsn:
        return get_session_factory()
    if _read_session_factory is None:
        _read_session_factory = async_sessionmaker(
            bind=get_read_engine(),
            expire_on_commit=False,
            autoflush=False,
            class_=AsyncSession,
        )
    return _read_session_factory


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
    global _engine, _session_factory, _read_engine, _read_session_factory
    if _read_engine is not None and _read_engine is not _engine:
        await _read_engine.dispose()
        logger.info("database.read_engine_disposed")
    if _engine is not None:
        await _engine.dispose()
        logger.info("database.engine_disposed")
    _engine = None
    _session_factory = None
    _read_engine = None
    _read_session_factory = None


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


async def probe_replica_lag(engine: AsyncEngine) -> float:
    """Measure how far the replica is behind, in seconds.

    Kept small and separate so a test can exercise the query without a PostgreSQL server.
    """
    from sqlalchemy import text

    async with engine.connect() as connection:
        value = await connection.scalar(text(_REPLICA_LAG_SQL))
    if value is None:
        return 0.0
    return max(float(value), 0.0)


async def replica_lag_seconds(*, force: bool = False) -> float:
    """Cached replica delay. Returns 0 when no replica is configured.

    A probe failure returns ``inf`` rather than 0: an unreachable replica must send reads
    to the primary, not convince the caller that it is perfectly caught up.
    """
    global _lag_seconds, _lag_checked_at, _lag_known
    settings = get_settings()
    if not settings.database_read_url or settings.read_sqlalchemy_dsn == settings.sqlalchemy_dsn:
        return 0.0

    now = time.monotonic()
    if not force and _lag_known and now - _lag_checked_at < settings.replica_lag_cache_seconds:
        return _lag_seconds

    engine = get_read_engine()
    if engine.dialect.name != "postgresql":
        measured = 0.0
    else:
        try:
            measured = await probe_replica_lag(engine)
        except Exception as exc:  # noqa: BLE001 - fall back, never fail the read
            logger.warning("database.replica_lag_probe_failed", error=str(exc)[:200])
            measured = float("inf")
    _lag_seconds = measured
    _lag_checked_at = now
    _lag_known = True
    return measured


async def replica_is_lagging() -> bool:
    """Whether the replica is too far behind to serve even lag-tolerant reads."""
    settings = get_settings()
    return await replica_lag_seconds() > settings.replica_max_lag_seconds


def reset_replica_lag_cache() -> None:
    """Forget the last probe. Tests only."""
    global _lag_seconds, _lag_checked_at, _lag_known
    _lag_seconds = 0.0
    _lag_checked_at = float("-inf")
    _lag_known = False


def make_read_uow() -> SqlAlchemyUnitOfWork:
    """Build a read-only unit of work, on the replica when one is configured.

    Only lag-tolerant reads may use this. A learner's own progress must never be read
    here: a replica that is a second behind would hide the lesson they just finished.
    """
    from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork

    return SqlAlchemyUnitOfWork(get_read_session_factory())
