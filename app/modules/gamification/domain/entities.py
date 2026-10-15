"""The gamification aggregates: a learner's stats, the achievement catalogue and one
learner's record against one achievement.

Three shapes, kept apart because they change for different reasons:

* :class:`UserStats` — the durable per-learner projection (XP, gems, streak, league). It
  is the one row the hub screen reads, and the only place a streak is *stored* rather than
  re-derived. It is written by the lesson-completion event handler, never by a request, so
  a learner answering questions never pays to update it.
* :class:`Achievement` — a catalogue entry: what unlocks it and what claiming it pays. It
  is content, defined in code (``domain/catalog.py``) and mirrored into a table so admin
  tooling and translations can reach it without a deploy.
* :class:`UserAchievement` — the join: when a learner unlocked an achievement and whether
  they have claimed its reward yet.

The rules that turn XP into a league, a completion into a streak day, and stats into an
unlocked achievement live in ``domain/policy.py``; this module holds only state and the
arithmetic that cannot be wrong.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from app.core.time import utc_now
from app.core.types import UserId

__all__ = ["Achievement", "LeagueTier", "UserAchievement", "UserStats"]


class LeagueTier(StrEnum):
    """How far up the league a learner's lifetime XP has carried them.

    A tier, not a rank: there is no leaderboard yet, so a tier is a private badge rather
    than a position that would leak other learners' figures. The thresholds are in
    ``domain/policy.py``.
    """

    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    PLATINUM = "platinum"
    DIAMOND = "diamond"


@dataclass(slots=True)
class UserStats:
    """One learner's durable gamification projection.

    ── Why this exists when XP and streak are *also* derivable ──
    ``ProgressService`` derives ``total_xp`` from ``lesson_progress`` and the streak from
    session completion times. That is a correct read of history, and it stays the source
    of truth for the progress screen. But an achievement check that ran on every answer
    would re-aggregate every lesson row on every request, and a "streak freeze" has to
    record a decision that is not implied by any completion. So this row is a *projection*
    kept in step by the lesson-completion handler, and a read that finds it missing builds
    it from history first (see ``application/service.py``) so a learner who predates this
    phase is never shown a zero they did not earn.
    """

    user_id: UserId
    xp_total: int = 0
    gems: int = 0
    current_streak: int = 0
    longest_streak: int = 0
    last_activity_date: date | None = None
    freezes_available: int = 0
    league_tier: LeagueTier = LeagueTier.BRONZE
    league_updated_at: datetime | None = None
    updated_at: datetime = field(default_factory=utc_now)

    # ------------------------------------------------------------- mutation
    def award_xp(self, points: int, *, at: datetime | None = None) -> None:
        """Add XP. A non-positive award is ignored so a zero-point retry cannot lower it."""
        if points <= 0:
            return
        self.xp_total += points
        self.updated_at = at or utc_now()

    def add_gems(self, amount: int, *, at: datetime | None = None) -> None:
        if amount <= 0:
            return
        self.gems += amount
        self.updated_at = at or utc_now()

    def set_league(self, tier: LeagueTier, *, at: datetime | None = None) -> bool:
        """Promote (or demote) the stored tier. Returns whether it changed.

        The tier is *stored* rather than derived on read so ``league_updated_at`` can mark
        when a promotion happened — a derived tier has no instant to celebrate.
        """
        if tier is self.league_tier:
            return False
        self.league_tier = tier
        self.league_updated_at = at or utc_now()
        self.updated_at = self.league_updated_at
        return True

    def record_activity(self, day: date) -> bool:
        """Fold a completed session on ``day`` into the streak. Returns whether it changed.

        Idempotent for a repeated day (the second completion in a day is not a second
        streak day) and tolerant of an out-of-order event (a late-arriving event for a
        day already behind us is ignored rather than resetting the run).
        """
        last = self.last_activity_date
        if last is not None and day <= last:
            return False
        if last is not None and (day - last).days == 1:
            self.current_streak += 1
        else:
            self.current_streak = 1
        self.last_activity_date = day
        self.longest_streak = max(self.longest_streak, self.current_streak)
        self.updated_at = utc_now()
        return True

    def consume_freeze(self) -> bool:
        """Spend a streak freeze. Returns whether one was available."""
        if self.freezes_available <= 0:
            return False
        self.freezes_available -= 1
        self.updated_at = utc_now()
        return True


@dataclass(frozen=True, slots=True)
class Achievement:
    """A catalogue entry.

    ``condition`` is a small, declarative predicate — e.g. ``{"type": "streak", "days": 7}``
    — rather than code, so the catalogue can grow (and later move to an admin table) without
    a new branch in the evaluation loop. ``title_key`` / ``description_key`` / ``icon_key``
    are translation keys, not text: the client renders the learner's own language.
    """

    code: str
    title_key: str
    description_key: str
    icon_key: str
    condition: dict[str, Any]
    xp_reward: int = 0
    gems_reward: int = 0
    sort_order: int = 0


@dataclass(slots=True)
class UserAchievement:
    """One learner's record against one catalogue entry.

    ``progress`` snapshots the metric at the moment of unlocking (or the best value seen),
    which is what a "7/7 days" badge shows. ``claimed_at`` is separate from ``unlocked_at``
    because unlocking is automatic and claiming pays the reward on demand; a learner may
    unlock an achievement and never claim it.
    """

    id: uuid.UUID
    user_id: UserId
    achievement_code: str
    unlocked_at: datetime
    progress: int = 0
    claimed_at: datetime | None = None

    @property
    def is_claimed(self) -> bool:
        return self.claimed_at is not None
