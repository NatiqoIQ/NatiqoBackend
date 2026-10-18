"""The overview use case: one aggregate read, one derived percentage.

Deliberately thin. There is no rule to enforce and nothing to write — the interesting
decisions (what "active" means, how accuracy is weighted) live in the adapter and in
:func:`_accuracy_percent` here, where a test can state them directly instead of inferring
them from a fixture's arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.time import utc_now
from app.core.uow import UnitOfWorkProtocol
from app.modules.admin.domain.stats import AdminOverview

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
    """The overview as the route renders it: totals plus the two derived values."""

    generated_at: datetime
    window_days: int
    accuracy_percent: int
    totals: AdminOverview


def _accuracy_percent(correct: int, graded: int) -> int:
    """Weighted accuracy, rounded, clamped to 0..100.

    Zero graded answers is zero percent rather than an error or a null: the screen shows a
    number in every state, and "no answers yet" is already visible in ``answers_graded``.
    """
    if graded <= 0:
        return 0
    return max(0, min(100, round(100 * correct / graded)))


class AdminStatsService:
    def __init__(self, uow: UnitOfWorkProtocol) -> None:
        self._uow = uow

    async def overview(self, *, window_days: int = DEFAULT_WINDOW_DAYS) -> AdminOverviewView:
        now = utc_now()
        totals = await self._uow.stats.overview(since=now - timedelta(days=window_days))
        return AdminOverviewView(
            generated_at=now,
            window_days=window_days,
            accuracy_percent=_accuracy_percent(totals.correct_answers, totals.answers_graded),
            totals=totals,
        )
