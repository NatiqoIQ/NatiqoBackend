"""Batched AI metering (P2-4).

One transaction per AI call makes cost attribution the busiest consumer of the connection
pool. These tests pin the replacement: many records, one write, and a shutdown path that
does not lose the rows still waiting for the next window.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.ai.infrastructure.models import AiUsageRecord
from app.modules.ai.infrastructure.usage import drain, flush, record_ai_usage
from app.modules.users.infrastructure.models import UserModel
from app.platform.ai.port import AIFeature, AIUsage


async def _user(session: AsyncSession) -> UserModel:
    row = UserModel(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", status="active")
    session.add(row)
    await session.commit()
    return row


def _usage(user_id: uuid.UUID, *, cost: str = "0.01", status: str = "ok") -> AIUsage:
    return AIUsage(
        user_id=user_id,  # type: ignore[arg-type]
        feature=AIFeature.TUTOR_CHAT,
        provider="openai",
        model="test-model",
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal(cost),
        status=status,
    )


@pytest.mark.asyncio
async def test_many_records_are_written_by_one_flush(session: AsyncSession) -> None:
    user = await _user(session)
    for _ in range(3):
        record_ai_usage(_usage(user.id))

    written = await flush()

    assert written == 3
    session.expunge_all()
    rows = (await session.execute(select(AiUsageRecord))).scalars().all()
    assert len(rows) == 3
    assert {row.status for row in rows} == {"ok"}
    assert sum(row.cost_usd for row in rows) == Decimal("0.03")


@pytest.mark.asyncio
async def test_shutdown_flushes_the_pending_window_without_waiting(session: AsyncSession) -> None:
    user = await _user(session)
    record_ai_usage(_usage(user.id, cost="0.02"))

    await drain(timeout_seconds=2)

    session.expunge_all()
    total = await session.scalar(select(func.count()).select_from(AiUsageRecord))
    assert total == 1


@pytest.mark.asyncio
async def test_a_flushed_batch_is_not_written_twice(session: AsyncSession) -> None:
    user = await _user(session)
    record_ai_usage(_usage(user.id))

    await flush()
    written_again = await flush()

    assert written_again == 0
    session.expunge_all()
    total = await session.scalar(select(func.count()).select_from(AiUsageRecord))
    assert total == 1
