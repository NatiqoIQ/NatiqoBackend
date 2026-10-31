"""The dashboard's daily rollup (F19).

Two properties matter and neither is visible from a passing query: a day is recomputed
idempotently, and the overview switches to the rollup only after the marker says the
backfill is complete. The overview test deletes the source rows after rolling them up —
if the code ever fell back to live queries, the number would drop and the test would fail.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_all, dispose_engine, get_session_factory, make_uow
from app.core.registry import import_all_models
from app.core.time import utc_now
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.admin.infrastructure.daily_metrics import DailyMetricsRepository
from app.modules.admin.infrastructure.metrics_models import DailyMetricModel
from app.modules.admin.infrastructure.stats_repository import SqlAlchemyAdminStatsRepository
from app.modules.ai.infrastructure.models import AiUsageRecord
from app.modules.users.infrastructure.models import UserModel
from app.platform.queue.port import Job, JobName
from app.workers.handlers import WorkerContext, build_handlers

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 31, 3, 0, 0, tzinfo=UTC)


async def _user(session: AsyncSession) -> UserModel:
    row = UserModel(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", status="active")
    session.add(row)
    await session.flush()
    return row


def _usage(user_id: uuid.UUID, *, created_at: datetime, cost: str) -> AiUsageRecord:
    return AiUsageRecord(
        id=uuid.uuid4(),
        user_id=user_id,
        feature="tutor_chat",
        provider="openai",
        model="test-model",
        input_tokens=60,
        output_tokens=40,
        latency_ms=120,
        cost_usd=Decimal(cost),
        status="ok",
        created_at=created_at,
    )


@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest.mark.asyncio
async def test_a_day_is_recomputed_not_incremented(uow: SqlAlchemyUnitOfWork) -> None:
    day = _NOW.date() - timedelta(days=1)
    user = await _user(uow.session)
    uow.session.add_all(
        [
            _usage(user.id, created_at=_NOW - timedelta(days=1), cost="0.25"),
            _usage(user.id, created_at=_NOW - timedelta(days=1), cost="0.50"),
        ]
    )
    await uow.commit()

    repository = DailyMetricsRepository(uow.session)
    first = await repository.recompute(day, day)
    await uow.commit()
    second = await repository.recompute(day, day)
    await uow.commit()

    assert (first, second) == (1, 1)
    row = await uow.session.get(DailyMetricModel, day)
    assert row is not None
    assert (row.ai_requests, row.ai_tokens, row.ai_cost_usd) == (2, 200, Decimal("0.750000"))
    assert await uow.session.scalar(select(func.count()).select_from(DailyMetricModel)) == 1, (
        "recomputing a day must replace its row, not add another"
    )


@pytest.mark.asyncio
async def test_the_rollout_gate_requires_yesterday(uow: SqlAlchemyUnitOfWork) -> None:
    repository = DailyMetricsRepository(uow.session)
    assert await repository.ready() is False, "an unmarked rollup must never be trusted"

    yesterday = _NOW.date() - timedelta(days=1)
    await repository.mark_backfilled(start=yesterday - timedelta(days=30), through=yesterday)
    await uow.commit()

    assert await repository.ready() is True

    await repository.mark_backfilled(
        start=yesterday - timedelta(days=30), through=yesterday - timedelta(days=1)
    )
    await uow.commit()
    assert await repository.ready() is False, "a marker that stops before yesterday is stale"


@pytest.mark.asyncio
async def test_the_overview_reads_history_from_the_rollup(session: AsyncSession) -> None:
    now = utc_now()
    today = now.date()
    yesterday = today - timedelta(days=1)
    user = await _user(session)
    session.add_all(
        [
            _usage(user.id, created_at=now - timedelta(days=1), cost="0.25"),
            _usage(user.id, created_at=now, cost="0.50"),
        ]
    )
    await session.commit()

    repository = DailyMetricsRepository(session)
    await repository.recompute(yesterday, yesterday)
    await repository.mark_backfilled(start=yesterday, through=yesterday)
    await session.commit()

    # If the overview still ran its original lifetime query, this delete would make the
    # report say one request instead of two.
    await session.execute(delete(AiUsageRecord).where(AiUsageRecord.created_at < _NOW))
    await session.commit()

    overview = await SqlAlchemyAdminStatsRepository(session).overview(since=now - timedelta(days=7))

    assert overview.ai.requests == 2
    assert overview.ai.tokens == 200
    assert overview.ai.cost_usd == pytest.approx(0.75)
    yesterday_point = next(point for point in overview.activity if point.day == yesterday)
    assert yesterday_point.ai_requests == 1
    today_point = next(point for point in overview.activity if point.day == today)
    assert today_point.ai_requests == 1


@pytest.mark.asyncio
async def test_the_worker_recomputes_the_lookback_window(uow: SqlAlchemyUnitOfWork) -> None:
    user = await _user(uow.session)
    uow.session.add_all(
        [
            _usage(user.id, created_at=_NOW - timedelta(days=1), cost="0.10"),
            _usage(user.id, created_at=_NOW - timedelta(days=2), cost="0.20"),
        ]
    )
    await uow.commit()

    # A backfill that finished three days ago: the job must fill the gap, not just its
    # usual lookback window, and then move the marker forward.
    repository = DailyMetricsRepository(uow.session)
    await repository.mark_backfilled(
        start=_NOW.date() - timedelta(days=10), through=_NOW.date() - timedelta(days=4)
    )
    await uow.commit()

    handlers = build_handlers(
        WorkerContext(
            uow_factory=make_uow,
            now=lambda: _NOW,
            metrics_rollup_lookback_days=2,
        )
    )
    await handlers[JobName.ROLLUP_DAILY_METRICS](Job(name=JobName.ROLLUP_DAILY_METRICS))

    async with make_uow() as check:
        rows = {
            row.day: row.ai_requests
            for row in (await check.session.execute(select(DailyMetricModel))).scalars()
        }
        assert await DailyMetricsRepository(check.session).ready() is True
    assert rows == {
        (_NOW.date() - timedelta(days=1)): 1,
        (_NOW.date() - timedelta(days=2)): 1,
    }


@pytest.mark.asyncio
async def test_a_zero_lookback_disables_the_job(uow: SqlAlchemyUnitOfWork) -> None:
    handlers = build_handlers(
        WorkerContext(uow_factory=make_uow, now=lambda: _NOW, metrics_rollup_lookback_days=0)
    )
    await handlers[JobName.ROLLUP_DAILY_METRICS](Job(name=JobName.ROLLUP_DAILY_METRICS))

    async with make_uow() as check:
        assert await check.session.scalar(select(func.count()).select_from(DailyMetricModel)) == 0
