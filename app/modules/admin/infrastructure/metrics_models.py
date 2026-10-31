"""Daily rollups for the admin dashboard (F19).

The overview is the only screen that reads *every* table it summarises. Nineteen
aggregates on a growing dataset are cheap when the product is small and become the
database's busiest client once ``quiz_answers`` and ``ai_usage_records`` pass tens of
millions of rows. The fix is not a faster query; it is not running the query.

One row per UTC day holds the four event families whose rows are append-only and whose
totals the screen shows as lifetime figures:

* completed quiz sessions and the XP they awarded,
* graded answers and how many were correct,
* spaced-repetition reviews, passes and the quality sum,
* AI calls, failures, tokens and cost.

Everything else on the screen is either small (catalogue stock), a live state count
(active sessions, streaks, active accounts) or a one-per-user row. Those stay live.

``metrics_meta`` is the rollout switch: until an operator has run the backfill, the
dashboard keeps using the original live queries. That is deliberate — a half-filled
rollup would quietly under-report every historical number, and a dashboard that lies is
worse than one that is slow.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, Numeric, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base


class DailyMetricModel(Base):
    """One UTC day of append-only event totals."""

    __tablename__ = "daily_metrics"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    sessions_completed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    xp_awarded: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    answers_graded: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    correct_answers: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    reviews_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    reviews_passed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    quality_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    ai_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    ai_errors: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    ai_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    ai_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MetricsMetaModel(Base):
    """Single-row rollout marker: which days the rollup is known to cover.

    ``backfilled_through`` is the last *complete* day written by the backfill. A rollup is
    only trusted when it reaches yesterday; after that the worker keeps it current.
    """

    __tablename__ = "metrics_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    backfilled_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    backfilled_through: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


__all__ = ["DailyMetricModel", "MetricsMetaModel"]
