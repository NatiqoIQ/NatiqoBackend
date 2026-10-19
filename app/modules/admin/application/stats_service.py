"""The overview use case: one aggregate read, the ratios it implies, one continuous chart.

Deliberately thin. There is no rule to enforce and nothing to write — the interesting
decisions (what "active" means, how accuracy is weighted) live in the adapter and in the
three small divisions here, where a test can state them directly instead of inferring them
from a fixture's arithmetic.

The one piece of work the use case does beyond forwarding is densifying the activity
series: the adapter reports days that saw activity, and a chart needs every day the window
covers so a quiet Tuesday renders as zero rather than as a gap. Only this layer knows where
the window starts, so only this layer can fill it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.time import utc_now
from app.core.uow import UnitOfWorkProtocol
from app.modules.admin.domain.stats import AdminOverview, DailyActivity

__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "AdminOverviewView",
    "AdminStatsService",
]

#: The activity window the overview screens default to. A week is long enough that a
#: weekend's quiet does not read as an outage and short enough that the number moves.
DEFAULT_WINDOW_DAYS = 7

#: The largest window the route accepts. Beyond a quarter the "active" figure stops
#: distinguishing anything from the total, and the query starts scanning history nobody
#: acts on.
MAX_WINDOW_DAYS = 90


@dataclass(frozen=True, slots=True)
class AdminOverviewView:
    """The overview as the route renders it: totals, the derived ratios, the chart.

    ``series`` is dense — one point per calendar day the window touches, first day to
    today — so it holds ``window_days + 1`` points: the window's endpoints are partial
    days (it opens at this clock time ``window_days`` ago, and today is still running),
    and pretending otherwise would move numbers between columns as the clock advances.
    """

    generated_at: datetime
    window_days: int
    accuracy_percent: int
    average_score: int
    retention_percent: int
    average_quality: float
    series: tuple[DailyActivity, ...]
    totals: AdminOverview


def _percent(part: int, whole: int) -> int:
    """Weighted share as a rounded percentage, clamped to 0..100.

    Used for both accuracy and retention: both are weighted by their unit of work (an
    answer, a review) rather than averaged over sessions or learners, so a two-item
    sitting cannot outweigh a twenty-item one.

    A zero denominator is zero percent rather than an error or a null: the screen shows a
    number in every state, and "nothing graded yet" is already visible in the count beside
    it.
    """
    if whole <= 0:
        return 0
    return max(0, min(100, round(100 * part / whole)))


def _average(total: int, count: int) -> int:
    """Rounded mean of a 0..100 score, or 0 when nothing has been graded yet."""
    if count <= 0:
        return 0
    return round(total / count)


def _quality_average(total: int, count: int) -> float:
    """Mean SM-2 grade to two decimals, or 0.0 when no review has been recorded."""
    if count <= 0:
        return 0.0
    return round(total / count, 2)


def _dense_series(
    activity: tuple[DailyActivity, ...], *, since: datetime, until: datetime
) -> tuple[DailyActivity, ...]:
    """Every day the window covers, with absent days reported as an all-zero point.

    The adapter's output is sparse by design; this is where "no rows" becomes the honest
    chart value of zero, and where the partial first and last days of the window get their
    (unavoidably partial) points instead of being dropped.
    """
    by_day = {point.day: point for point in activity}
    first, last = since.date(), until.date()
    return tuple(
        by_day.get(
            day,
            DailyActivity(day=day, new_users=0, sessions_completed=0, xp_awarded=0, ai_requests=0),
        )
        for day in (first + timedelta(days=offset) for offset in range((last - first).days + 1))
    )


class AdminStatsService:
    def __init__(self, uow: UnitOfWorkProtocol) -> None:
        self._uow = uow

    async def overview(self, *, window_days: int = DEFAULT_WINDOW_DAYS) -> AdminOverviewView:
        now = utc_now()
        since = now - timedelta(days=window_days)
        totals = await self._uow.stats.overview(since=since)
        performance = totals.performance
        return AdminOverviewView(
            generated_at=now,
            window_days=window_days,
            accuracy_percent=_percent(totals.correct_answers, totals.answers_graded),
            average_score=_average(performance.score_total, performance.rated_lessons),
            retention_percent=_percent(performance.reviews_passed, performance.reviews_total),
            average_quality=_quality_average(performance.quality_total, performance.reviews_total),
            series=_dense_series(totals.activity, since=since, until=now),
            totals=totals,
        )
