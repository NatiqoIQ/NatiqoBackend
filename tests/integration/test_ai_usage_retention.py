"""AI usage retention, against a real schema.

The usage log is append-only and grows with every model call. Only today's rows feed
the budget check; older rows exist for a rolling cost-attribution window. The job must
delete exactly those, in bounded batches, and must treat a non-positive window as
"keep" rather than "delete everything".
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_all, dispose_engine, get_session_factory, make_uow
from app.core.registry import import_all_models
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.ai.infrastructure.models import AiUsageRecord
from app.modules.users.infrastructure.models import UserModel
from app.platform.queue.port import Job, JobName
from app.workers.handlers import WorkerContext, build_handlers

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 31, 3, 0, 0)


@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


async def _usage_rows(
    session: AsyncSession, *, created_at: datetime, count: int = 1
) -> list[AiUsageRecord]:
    user = UserModel(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", status="active")
    session.add(user)
    await session.flush()
    rows = [
        AiUsageRecord(
            id=uuid.uuid4(),
            user_id=user.id,
            feature="tutor_chat",
            provider="openai",
            model="test-model",
            input_tokens=10,
            output_tokens=5,
            latency_ms=120,
            cost_usd=Decimal("0.001"),
            status="ok",
            created_at=created_at,
        )
        for _ in range(count)
    ]
    session.add_all(rows)
    return rows


async def _run_purge(*, retention_days: int) -> None:
    handlers = build_handlers(
        WorkerContext(
            uow_factory=make_uow,
            now=lambda: _NOW,
            ai_usage_retention_days=retention_days,
        )
    )
    await handlers[JobName.PURGE_AI_USAGE](Job(name=JobName.PURGE_AI_USAGE))


@pytest.mark.asyncio
async def test_only_rows_outside_the_window_are_deleted(uow: SqlAlchemyUnitOfWork) -> None:
    old = await _usage_rows(uow.session, created_at=_NOW - timedelta(days=91))
    fresh = await _usage_rows(uow.session, created_at=_NOW - timedelta(days=89))
    await uow.commit()

    await _run_purge(retention_days=90)

    async with make_uow() as check:
        remaining = {
            row.id for row in (await check.session.execute(select(AiUsageRecord))).scalars()
        }
    assert remaining == {row.id for row in fresh}
    assert old[0].id not in remaining


@pytest.mark.asyncio
async def test_a_non_positive_window_disables_the_job(uow: SqlAlchemyUnitOfWork) -> None:
    """``0`` means keep forever; reading it as "delete everything" would be a footgun."""
    await _usage_rows(uow.session, created_at=_NOW - timedelta(days=3650))
    await uow.commit()

    await _run_purge(retention_days=0)

    async with make_uow() as check:
        count = await check.session.scalar(select(func.count()).select_from(AiUsageRecord))
    assert count == 1


@pytest.mark.asyncio
async def test_purge_is_bounded_per_statement(uow: SqlAlchemyUnitOfWork) -> None:
    """The worker loops and commits; one DELETE must not lock the table for an age."""
    await _usage_rows(uow.session, created_at=_NOW - timedelta(days=400), count=12)
    await uow.commit()

    removed = 0
    batches = 0
    while True:
        count = await uow.ai_usage.purge_before(before=_NOW - timedelta(days=90), limit=5)
        await uow.commit()
        batches += 1
        removed += count
        if count < 5:
            break

    assert removed == 12
    assert batches == 3  # 5 + 5 + 2
