"""Read models for the gamification surface.

Plain frozen dataclasses, framework-free, so the service can be exercised without a web
layer and the wire schemas stay a thin projection of these. Nothing here reaches into an
ORM object.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

__all__ = [
    "AchievementView",
    "ClaimResultView",
    "GamificationSummaryView",
    "StreakView",
    "UserStatsView",
]


@dataclass(frozen=True, slots=True)
class UserStatsView:
    """A learner's headline numbers."""

    xp_total: int
    gems: int
    current_streak: int
    longest_streak: int
    league_tier: str
    freezes_available: int
    last_activity_date: date | None


@dataclass(frozen=True, slots=True)
class AchievementView:
    """One catalogue entry, coloured by the learner's record against it.

    ``progress``/``target`` let a client draw a "7/7" bar without knowing what axis the
    achievement measures. ``claimable`` is true exactly when it is unlocked but unclaimed,
    so the UI does not have to re-derive that from two nullable timestamps.
    """

    code: str
    title_key: str
    description_key: str
    icon_key: str
    xp_reward: int
    gems_reward: int
    target: int
    progress: int
    unlocked: bool
    unlocked_at: datetime | None
    claimed_at: datetime | None
    claimable: bool


@dataclass(frozen=True, slots=True)
class GamificationSummaryView:
    """Everything the hub screen needs in one response."""

    stats: UserStatsView
    lessons_completed: int
    achievements_unlocked: int
    achievements_total: int


@dataclass(frozen=True, slots=True)
class StreakView:
    """The streak detail screen's payload."""

    current_streak: int
    longest_streak: int
    last_activity_date: date | None
    freezes_available: int
    premium: bool


@dataclass(frozen=True, slots=True)
class ClaimResultView:
    """What claiming one achievement paid."""

    achievement: AchievementView
    xp_awarded: int
    gems_awarded: int
