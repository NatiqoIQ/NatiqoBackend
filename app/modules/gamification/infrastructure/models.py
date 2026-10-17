"""Gamification module — SQLAlchemy models.

Three tables:

* ``user_stats`` — the durable per-learner projection (XP, gems, streak, league), keyed by
  ``user_id``. One row per learner, so the hub screen is a single read. Its PK *is* the
  foreign key, which both enforces one-row-per-learner and cascades on account deletion.
* ``achievements`` — the catalogue, mirrored from ``domain/catalog.py``. The code is the
  primary key (a stable, human-readable handle a client or an admin references) and the
  ``condition`` is JSON so a new achievement kind is a data change, not an enum migration.
* ``user_achievements`` — the join, ``UNIQUE (user_id, achievement_code)`` so unlocking is
  idempotent at the database even if two events race.

The ``relationship()``s carry no navigation the code uses (they are read with
``lazy="raise_on_sql"``); they exist so the unit of work orders inserts correctly. A test
seeding a learner and their stats in one flush would otherwise write ``user_stats`` ahead of
``users`` (alphabetical order) and fail the foreign key.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.modules.gamification.domain.entities import LeagueTier
from app.modules.users.infrastructure.models import UserModel

#: Written into the ``CHECK`` on ``league_tier``. A module constant so the model, the
#: mapper and the migration cannot drift by being edited separately.
LEAGUE_TIERS: tuple[str, ...] = tuple(tier.value for tier in LeagueTier)
LEAGUE_TIERS_SQL: str = ", ".join(f"'{tier}'" for tier in LEAGUE_TIERS)

#: A catalogue entry is ``published`` (visible to learners) or ``draft`` (kept out of the
#: list until an editor releases it). The column exists now so a later admin surface does
#: not need a migration to hide an entry.
ACHIEVEMENT_STATES: tuple[str, ...] = ("draft", "published")
ACHIEVEMENT_STATES_SQL: str = ", ".join(f"'{state}'" for state in ACHIEVEMENT_STATES)


class UserStatsModel(Base, TimestampMixin):
    __tablename__ = "user_stats"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    xp_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gems: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    longest_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The learner-local day of the last counted completion. The streak is measured in
    #: their timezone, so the *date* is stored, not a UTC instant.
    last_activity_date: Mapped[date | None] = mapped_column(Date)
    freezes_available: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    league_tier: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LeagueTier.BRONZE.value
    )
    league_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Ordering metadata only — see the module docstring. Never navigated.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        CheckConstraint(f"league_tier IN ({LEAGUE_TIERS_SQL})", name="valid_league_tier"),
        CheckConstraint("xp_total >= 0", name="non_negative_xp"),
        CheckConstraint("gems >= 0", name="non_negative_gems"),
        CheckConstraint("current_streak >= 0", name="non_negative_current_streak"),
        CheckConstraint("longest_streak >= 0", name="non_negative_longest_streak"),
        CheckConstraint("freezes_available >= 0", name="non_negative_freezes"),
    )


class AchievementModel(Base, TimestampMixin):
    __tablename__ = "achievements"

    #: The stable handle. It is the primary key because the code, not a surrogate id, is
    #: what a client, an admin and ``user_achievements`` all reference.
    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    title_key: Mapped[str] = mapped_column(String(120), nullable=False)
    description_key: Mapped[str] = mapped_column(String(160), nullable=False)
    icon_key: Mapped[str] = mapped_column(String(40), nullable=False)
    xp_reward: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gems_reward: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    condition: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="published")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint(f"state IN ({ACHIEVEMENT_STATES_SQL})", name="valid_state"),
        CheckConstraint("xp_reward >= 0", name="non_negative_xp_reward"),
        CheckConstraint("gems_reward >= 0", name="non_negative_gems_reward"),
    )


class UserAchievementModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "user_achievements"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    achievement_code: Mapped[str] = mapped_column(
        ForeignKey("achievements.code", ondelete="CASCADE"), nullable=False
    )
    unlocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Ordering metadata only — see the module docstring. Never navigated.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")
    achievement: Mapped[AchievementModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        # The write half of "one row per learner per achievement"; the repository's read is
        # the other. Backs up the unlock check so a race cannot double-insert.
        UniqueConstraint("user_id", "achievement_code", name="uq_user_achievements_user_code"),
        Index("ix_user_achievements_user", "user_id"),
    )
