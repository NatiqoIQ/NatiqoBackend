"""SQLAlchemy implementation of :class:`AiUsageRepositoryPort`.

Mappers are explicit functions rather than declarative configuration because the table
is append-only and never hydrated as an object graph: a usage row has no relationships
and no lazy loads, so the round trip is a row in and a frozen dataclass out.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import UserId
from app.modules.ai.domain.usage import AiUsageEntry, AiUsageRepositoryPort
from app.modules.ai.infrastructure.models import AiUsageRecord


def _to_entry(model: AiUsageRecord) -> AiUsageEntry:
    return AiUsageEntry(
        id=model.id,
        user_id=UserId(model.user_id),
        feature=model.feature,
        provider=model.provider,
        model=model.model,
        input_tokens=model.input_tokens,
        output_tokens=model.output_tokens,
        latency_ms=model.latency_ms,
        # `Numeric` comes back from the driver as a Decimal already, but a SQLite
        # build can hand back a float; the cast keeps the domain type honest either way.
        cost_usd=Decimal(str(model.cost_usd)),
        status=model.status,
        error=model.error,
        created_at=model.created_at,
    )


def _to_model(entry: AiUsageEntry) -> AiUsageRecord:
    model = AiUsageRecord(
        id=entry.id,
        user_id=entry.user_id,
        feature=entry.feature,
        provider=entry.provider,
        model=entry.model,
        input_tokens=entry.input_tokens,
        output_tokens=entry.output_tokens,
        latency_ms=entry.latency_ms,
        cost_usd=entry.cost_usd,
        status=entry.status,
        error=entry.error,
    )
    if entry.created_at is not None:
        # Assigned rather than passed to the constructor: the column is NOT NULL with a
        # server default, so an explicit `None` would insert NULL and fail instead of
        # falling back to the database clock.
        model.created_at = entry.created_at
    return model


class SqlAlchemyAiUsageRepository(AiUsageRepositoryPort):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, entry_id: uuid.UUID) -> AiUsageEntry | None:
        model = await self._session.get(AiUsageRecord, entry_id)
        return _to_entry(model) if model is not None else None

    async def add(self, entry: AiUsageEntry) -> None:
        self._session.add(_to_model(entry))

    async def add_if_absent(self, entry: AiUsageEntry) -> bool:
        if await self.get(entry.id) is not None:
            return False
        await self.add(entry)
        return True

    async def purge_before(self, *, before: datetime, limit: int = 5000) -> int:
        stale_ids = (
            select(AiUsageRecord.id).where(AiUsageRecord.created_at < before).limit(max(1, limit))
        )
        result = await self._session.execute(
            delete(AiUsageRecord).where(AiUsageRecord.id.in_(stale_ids))
        )
        return int(getattr(result, "rowcount", 0) or 0)


__all__ = ["SqlAlchemyAiUsageRepository"]
