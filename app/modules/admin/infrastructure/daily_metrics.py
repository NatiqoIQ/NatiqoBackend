"""Daily rollup writer and reader (F19).

The writer recomputes a date range from the four append-only event tables and replaces the
matching ``daily_metrics`` rows in one transaction. Recomputing a day is idempotent, which
is what lets the worker rebuild yesterday after a late write without a delta ledger.

Day boundaries are UTC half-open ``[start, end)`` intervals. ``func.date(...)`` groups in
the database's timezone, and the deployment pins PostgreSQL to UTC; using the same
function as the activity series keeps the rollup and the chart from disagreeing at
midnight.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.modules.admin.infrastructure.metrics_models import DailyMetricModel, MetricsMetaModel
from app.modules.ai.infrastructure.models import AiUsageRecord
from app.modules.quiz.domain.entities import SessionStatus
from app.modules.quiz.domain.review import PASSING_QUALITY
from app.modules.quiz.infrastructure.models import QuizAnswerModel, QuizSessionModel
from app.modules.quiz.infrastructure.review_models import ReviewLogModel

__all__ = ["DailyMetricsRepository", "RollupTotals"]

_AI_OK = "ok"


@dataclass(frozen=True, slots=True)
class RollupTotals:
    """The rollup's columns as one value, so callers never add them by index."""

    sessions_completed: int = 0
    xp_awarded: int = 0
    answers_graded: int = 0
    correct_answers: int = 0
    reviews_total: int = 0
    reviews_passed: int = 0
    quality_total: int = 0
    ai_requests: int = 0
    ai_errors: int = 0
    ai_tokens: int = 0
    ai_cost_usd: Decimal = Decimal("0")


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """Half-open UTC bounds for an inclusive calendar-day range."""
    return (
        datetime.combine(start, time.min, tzinfo=UTC),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
    )


class DailyMetricsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ rollout gate
    async def ready(self) -> bool:
        """Whether the rollup covers every complete day up to yesterday.

        A missing marker means the backfill has not been run. Until it has, callers must
        use the live queries: a partial rollup under-reports history without failing.
        """
        meta = await self._session.get(MetricsMetaModel, 1)
        if meta is None or meta.backfilled_through is None:
            return False
        return meta.backfilled_through >= utc_now().date() - timedelta(days=1)

    async def coverage(self) -> tuple[date | None, date | None]:
        meta = await self._session.get(MetricsMetaModel, 1)
        if meta is None:
            return None, None
        return meta.backfilled_from, meta.backfilled_through

    async def advance(self, through: date) -> None:
        """Move the marker forward after a successful daily recompute.

        Only called when the rollup already had a complete backfill. Leaving the marker
        behind when the worker stops is what makes the overview fall back to the live
        queries instead of silently omitting the days nobody rolled up.
        """
        meta = await self._session.get(MetricsMetaModel, 1)
        if meta is None:
            return
        meta.backfilled_through = max(meta.backfilled_through or through, through)
        await self._session.flush()

    async def mark_backfilled(self, *, start: date, through: date) -> None:
        meta = await self._session.get(MetricsMetaModel, 1)
        if meta is None:
            meta = MetricsMetaModel(id=1)
            self._session.add(meta)
        meta.backfilled_from = start
        meta.backfilled_through = through
        await self._session.flush()

    # ---------------------------------------------------------------------- readers
    async def totals_before(self, day: date) -> RollupTotals:
        """Lifetime totals for every complete day before ``day`` (today is live)."""
        row = (
            await self._session.execute(
                select(
                    func.sum(DailyMetricModel.sessions_completed),
                    func.sum(DailyMetricModel.xp_awarded),
                    func.sum(DailyMetricModel.answers_graded),
                    func.sum(DailyMetricModel.correct_answers),
                    func.sum(DailyMetricModel.reviews_total),
                    func.sum(DailyMetricModel.reviews_passed),
                    func.sum(DailyMetricModel.quality_total),
                    func.sum(DailyMetricModel.ai_requests),
                    func.sum(DailyMetricModel.ai_errors),
                    func.sum(DailyMetricModel.ai_tokens),
                    func.sum(DailyMetricModel.ai_cost_usd),
                ).where(DailyMetricModel.day < day)
            )
        ).one()
        return RollupTotals(
            sessions_completed=int(row[0] or 0),
            xp_awarded=int(row[1] or 0),
            answers_graded=int(row[2] or 0),
            correct_answers=int(row[3] or 0),
            reviews_total=int(row[4] or 0),
            reviews_passed=int(row[5] or 0),
            quality_total=int(row[6] or 0),
            ai_requests=int(row[7] or 0),
            ai_errors=int(row[8] or 0),
            ai_tokens=int(row[9] or 0),
            ai_cost_usd=Decimal(row[10] or 0),
        )

    async def days(self, start: date, end: date) -> dict[date, RollupTotals]:
        """Rollup rows for an inclusive day range, keyed by day."""
        rows = (
            await self._session.execute(
                select(DailyMetricModel).where(
                    DailyMetricModel.day >= start, DailyMetricModel.day <= end
                )
            )
        ).scalars()
        return {
            row.day: RollupTotals(
                sessions_completed=row.sessions_completed,
                xp_awarded=row.xp_awarded,
                answers_graded=row.answers_graded,
                correct_answers=row.correct_answers,
                reviews_total=row.reviews_total,
                reviews_passed=row.reviews_passed,
                quality_total=row.quality_total,
                ai_requests=row.ai_requests,
                ai_errors=row.ai_errors,
                ai_tokens=row.ai_tokens,
                ai_cost_usd=Decimal(row.ai_cost_usd),
            )
            for row in rows
        }

    # ---------------------------------------------------------------------- writer
    async def recompute(self, start: date, end: date) -> int:
        """Rebuild every day in ``[start, end]`` from the source tables.

        Returns the number of day rows written. Days with no events are not stored: the
        chart densifies gaps and the lifetime sums treat a missing day as zero, so a row
        of zeroes would only be a row to keep updating.
        """
        if end < start:
            return 0
        start_at, end_at = _bounds(start, end)
        totals: dict[date, dict[str, object]] = defaultdict(
            lambda: {
                "sessions_completed": 0,
                "xp_awarded": 0,
                "answers_graded": 0,
                "correct_answers": 0,
                "reviews_total": 0,
                "reviews_passed": 0,
                "quality_total": 0,
                "ai_requests": 0,
                "ai_errors": 0,
                "ai_tokens": 0,
                "ai_cost_usd": Decimal("0"),
            }
        )

        sessions = (
            await self._session.execute(
                select(
                    func.date(QuizSessionModel.completed_at),
                    func.count(),
                    func.sum(QuizSessionModel.xp_awarded),
                )
                .where(
                    QuizSessionModel.status == SessionStatus.COMPLETED.value,
                    QuizSessionModel.completed_at >= start_at,
                    QuizSessionModel.completed_at < end_at,
                )
                .group_by(func.date(QuizSessionModel.completed_at))
            )
        ).all()
        for day, count, xp in sessions:
            bucket = totals[_as_date(day)]
            bucket["sessions_completed"] = int(count or 0)
            bucket["xp_awarded"] = int(xp or 0)

        answers = (
            await self._session.execute(
                select(
                    func.date(QuizAnswerModel.answered_at),
                    func.count(),
                    func.sum(case((QuizAnswerModel.is_correct, 1), else_=0)),
                )
                .where(
                    QuizAnswerModel.answered_at >= start_at,
                    QuizAnswerModel.answered_at < end_at,
                )
                .group_by(func.date(QuizAnswerModel.answered_at))
            )
        ).all()
        for day, count, correct in answers:
            bucket = totals[_as_date(day)]
            bucket["answers_graded"] = int(count or 0)
            bucket["correct_answers"] = int(correct or 0)

        reviews = (
            await self._session.execute(
                select(
                    func.date(ReviewLogModel.reviewed_at),
                    func.count(),
                    func.sum(case((ReviewLogModel.quality >= PASSING_QUALITY, 1), else_=0)),
                    func.sum(ReviewLogModel.quality),
                )
                .where(
                    ReviewLogModel.reviewed_at >= start_at,
                    ReviewLogModel.reviewed_at < end_at,
                )
                .group_by(func.date(ReviewLogModel.reviewed_at))
            )
        ).all()
        for day, count, passed, quality in reviews:
            bucket = totals[_as_date(day)]
            bucket["reviews_total"] = int(count or 0)
            bucket["reviews_passed"] = int(passed or 0)
            bucket["quality_total"] = int(quality or 0)

        usage = (
            await self._session.execute(
                select(
                    func.date(AiUsageRecord.created_at),
                    func.count(),
                    func.sum(case((AiUsageRecord.status != _AI_OK, 1), else_=0)),
                    func.sum(AiUsageRecord.input_tokens + AiUsageRecord.output_tokens),
                    func.sum(AiUsageRecord.cost_usd),
                )
                .where(
                    AiUsageRecord.created_at >= start_at,
                    AiUsageRecord.created_at < end_at,
                )
                .group_by(func.date(AiUsageRecord.created_at))
            )
        ).all()
        for day, count, errors, tokens, cost in usage:
            bucket = totals[_as_date(day)]
            bucket["ai_requests"] = int(count or 0)
            bucket["ai_errors"] = int(errors or 0)
            bucket["ai_tokens"] = int(tokens or 0)
            bucket["ai_cost_usd"] = Decimal(cost or 0)

        await self._session.execute(
            delete(DailyMetricModel).where(
                DailyMetricModel.day >= start, DailyMetricModel.day <= end
            )
        )
        self._session.add_all([DailyMetricModel(day=day, **totals[day]) for day in sorted(totals)])
        await self._session.flush()
        return len(totals)
