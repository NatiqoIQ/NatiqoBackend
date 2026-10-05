"""AI module infrastructure models."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base


class AiUsageRecord(Base):
    """One AI call. Append-only; never updated.

    Kept as its own table (rather than a column on ``users``) so cost can be
    aggregated per day, per feature and per provider without a schema change each
    time a new AI feature ships.
    """

    __tablename__ = "ai_usage_records"

    # ``Uuid`` maps to PostgreSQL's native UUID and to CHAR(32) elsewhere, so the same
    # model runs on Postgres in production and SQLite in the test suite.
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    feature: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)

    input_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    error: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Budget checks hit (user_id, created_at); cost reports hit (created_at, feature).
        Index("ix_ai_usage_user_created", "user_id", "created_at"),
        Index("ix_ai_usage_feature_created", "feature", "created_at"),
    )
