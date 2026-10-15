"""Wire schemas for the gamification surface.

Fields are listed by hand rather than inherited wholesale from the read models, the same way
the catalogue, quiz and review surfaces do: the JSON is a contract, and a field added to a
DTO cannot silently start leaking to clients.

The tier travels as its string value (``"GOLD"``), not the enum, because that is what the
Flutter client switches on — and because a JSON enum that serialises differently across a
library upgrade is a wire break nobody wants.

``from_attributes`` (via ``_GamificationWire``) lets the routes hand a read model straight to
a response model, exactly as the AI and review schemas do.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import ConfigDict

from app.core.responses import WireModel

__all__ = [
    "AchievementListResponse",
    "AchievementResponse",
    "ClaimResultResponse",
    "GamificationSummaryResponse",
    "StreakResponse",
    "UserStatsResponse",
]


class _GamificationWire(WireModel):
    """A wire model that reads attributes off a read model."""

    model_config = ConfigDict(from_attributes=True)


class UserStatsResponse(_GamificationWire):
    """The headline numbers on the hub: XP, gems, streaks and the current league tier."""

    xp_total: int
    gems: int
    current_streak: int
    longest_streak: int
    league_tier: str
    freezes_available: int
    last_activity_date: date | None = None


class AchievementResponse(_GamificationWire):
    """One achievement, coloured by the learner's record against it.

    ``progress``/``target`` let a client draw a progress bar without knowing what axis the
    badge measures; ``claimable`` is the single bit that decides whether a "claim" button is
    live, so the client never has to re-derive it from two nullable timestamps.
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
    unlocked_at: datetime | None = None
    claimed_at: datetime | None = None
    claimable: bool


class AchievementListResponse(_GamificationWire):
    """The whole catalogue in one response.

    Whole-list rather than paginated: the catalogue is a fixed, small, code-owned set, and a
    screen that draws badges incrementally would need the full set anyway. ``total`` is
    carried explicitly so the client need not count.
    """

    items: list[AchievementResponse]
    total: int


class GamificationSummaryResponse(_GamificationWire):
    """The hub screen's payload: stats plus the achievement tally, in one round trip."""

    stats: UserStatsResponse
    lessons_completed: int
    achievements_unlocked: int
    achievements_total: int


class StreakResponse(_GamificationWire):
    """The streak detail screen. ``premium`` says whether freezing is on offer at all, so the
    client can show the paywall *before* the learner taps rather than after a 402."""

    current_streak: int
    longest_streak: int
    last_activity_date: date | None = None
    freezes_available: int
    premium: bool


class ClaimResultResponse(_GamificationWire):
    """What claiming one achievement paid, plus the updated badge."""

    achievement: AchievementResponse
    xp_awarded: int
    gems_awarded: int
