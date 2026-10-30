"""The first real job, against a real schema.

The job is small, and that is the point: it exists to prove the loop — a job arrives,
a handler runs, a unit of work commits, and the row count changes because of it. What
only the database can decide is the *selection*: which rows are dead enough to delete.

Three rows go in and only one may come out. An expired row is history with no reader.
A revoked-but-unexpired row is a reuse signal and stays. A live row is a session.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_all, dispose_engine, get_session_factory, make_uow
from app.core.registry import import_all_models
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.users.infrastructure.models import RefreshTokenModel, UserModel
from app.platform.queue.port import Job, JobName
from app.workers.handlers import WorkerContext, build_handlers

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 19, 3, 0, 0)


@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest.fixture
def session(uow: SqlAlchemyUnitOfWork) -> AsyncSession:
    return uow.session


def _token_rows(session: AsyncSession) -> list[RefreshTokenModel]:
    """One user holding three tokens: expired, revoked-but-live, and live."""
    user = UserModel(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", status="active")
    session.add(user)
    rows = [
        RefreshTokenModel(
            id=uuid.uuid4(),
            user_id=user.id,
            jti_hash=uuid.uuid4().hex * 2,
            family_id=uuid.uuid4().hex[:16],
            expires_at=_NOW - timedelta(hours=1),
        ),
        RefreshTokenModel(
            id=uuid.uuid4(),
            user_id=user.id,
            jti_hash=uuid.uuid4().hex * 2,
            family_id=uuid.uuid4().hex[:16],
            expires_at=_NOW + timedelta(days=30),
            revoked_at=_NOW - timedelta(days=1),
        ),
        RefreshTokenModel(
            id=uuid.uuid4(),
            user_id=user.id,
            jti_hash=uuid.uuid4().hex * 2,
            family_id=uuid.uuid4().hex[:16],
            expires_at=_NOW + timedelta(days=30),
        ),
    ]
    session.add_all(rows)
    return rows


async def _run_cleanup() -> None:
    """Invoke the real handler exactly as the worker would."""
    handlers = build_handlers(WorkerContext(uow_factory=make_uow, now=lambda: _NOW))
    await handlers[JobName.CLEANUP_EXPIRED_TOKENS](Job(name=JobName.CLEANUP_EXPIRED_TOKENS))


@pytest.mark.asyncio
async def test_only_the_expired_token_is_deleted(uow: SqlAlchemyUnitOfWork) -> None:
    _, revoked_but_live, live = _token_rows(uow.session)
    await uow.commit()

    await _run_cleanup()

    async with make_uow() as fresh:
        remaining = {
            row.jti_hash
            for row in (await fresh.session.execute(select(RefreshTokenModel))).scalars()
        }
    assert remaining == {revoked_but_live.jti_hash, live.jti_hash}


@pytest.mark.asyncio
async def test_the_job_is_idempotent(uow: SqlAlchemyUnitOfWork) -> None:
    """A missed schedule, a restart, a retried attempt: a second run must be a no-op."""
    _token_rows(uow.session)
    await uow.commit()

    await _run_cleanup()
    await _run_cleanup()

    async with make_uow() as fresh:
        remaining = await fresh.session.scalar(select(func.count()).select_from(RefreshTokenModel))
    assert remaining == 2


@pytest.mark.asyncio
async def test_an_empty_table_is_not_an_error(uow: SqlAlchemyUnitOfWork) -> None:
    """The common case on a fresh deployment: nothing to delete, and that is success."""
    await uow.commit()

    await _run_cleanup()

    async with make_uow() as fresh:
        remaining = await fresh.session.scalar(select(func.count()).select_from(RefreshTokenModel))
    assert remaining == 0


@pytest.mark.asyncio
async def test_purge_is_bounded_per_statement(uow: SqlAlchemyUnitOfWork) -> None:
    """A big purge must not be one DELETE that locks the table and writes one huge WAL.

    The worker loops until a batch comes back short, committing between batches, so each
    statement touches at most ``limit`` rows and releases its locks before the next.
    """
    session = uow.session
    user = UserModel(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", status="active")
    session.add(user)
    for _ in range(12):
        session.add(
            RefreshTokenModel(
                id=uuid.uuid4(),
                user_id=user.id,
                jti_hash=uuid.uuid4().hex * 2,
                family_id=uuid.uuid4().hex[:16],
                expires_at=_NOW - timedelta(hours=1),
            )
        )
    await session.commit()

    removed = 0
    batches = 0
    while True:
        count = await uow.refresh_tokens.purge_expired(before=_NOW, limit=5)
        await uow.commit()
        batches += 1
        removed += count
        if count < 5:
            break

    assert removed == 12
    assert batches == 3  # 5 + 5 + 2
    async with make_uow() as fresh:
        remaining = await fresh.session.scalar(select(func.count()).select_from(RefreshTokenModel))
    assert remaining == 0
