"""Spaced-repetition — SQLAlchemy models.

Two tables:

* ``review_cards`` — the scheduling state for one ``(user, question)`` pair, with a
  ``UNIQUE (user_id, question_id)`` so a question answered correctly a second time
  updates no new row. The card is mutated in place as it is reviewed; only the SM-2
  columns change, so the identity never moves.
* ``review_logs`` — an append-only record of each review, kept so the learner's
  retention percentage can be answered without re-deriving it from the card. Its
  ``card_id`` cascades, because a log without its card is meaningless.

These live in the ``quiz`` module rather than a module of their own: card creation is
triggered by a correct quiz answer, and keeping them here avoids an import cycle where
the review side would need :func:`~app.modules.quiz.domain.policy.grade_answer` from
quiz while quiz needed review to schedule.

The ``relationship()``s carry no navigation the code uses (``lazy="raise_on_sql"``);
they exist so the unit of work knows the insert order against ``users`` and
``questions``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.modules.content.infrastructure.models import QuestionModel
from app.modules.quiz.domain.review import (
    DEFAULT_EASE_FACTOR,
    MIN_EASE_FACTOR,
    ReviewState,
)
from app.modules.users.infrastructure.models import UserModel

#: Written into the ``CHECK`` on ``state``. A module constant so the model, the mapper
#: and the migration cannot drift by being edited separately.
REVIEW_STATES: tuple[str, ...] = tuple(state.value for state in ReviewState)
REVIEW_STATES_SQL: str = ", ".join(f"'{state}'" for state in REVIEW_STATES)


class ReviewCardModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "review_cards"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )

    #: SM-2 scheduling state. Floats and small ints are ample; these are counters, not
    #: money, so ``Float`` is the honest type for the ease factor.
    ease_factor: Mapped[float] = mapped_column(Float, nullable=False, default=DEFAULT_EASE_FACTOR)
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lapses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: When the card next comes up. The queue reads it with a ``<= now`` predicate.
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=ReviewState.NEW.value)

    #: Ordering metadata only — see the module docstring. Never navigated.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")
    question: Mapped[QuestionModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        # The write half of "one card per question"; the repository read is the other.
        UniqueConstraint("user_id", "question_id", name="uq_review_cards_user_question"),
        CheckConstraint(f"state IN ({REVIEW_STATES_SQL})", name="valid_state"),
        CheckConstraint(f"ease_factor >= {MIN_EASE_FACTOR}", name="ease_at_least_floor"),
        CheckConstraint("interval_days >= 0", name="non_negative_interval"),
        CheckConstraint("repetitions >= 0", name="non_negative_repetitions"),
        CheckConstraint("lapses >= 0", name="non_negative_lapses"),
        Index("ix_review_cards_user_due", "user_id", "due_at"),
    )


class ReviewLogModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One review. Written once, never updated."""

    __tablename__ = "review_logs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    card_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_cards.id", ondelete="CASCADE"), nullable=False
    )
    #: The SM-2 grade (0–5). ``SmallInteger`` is ample.
    quality: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Both intervals are copied here because the card only remembers the latest one.
    previous_interval_days: Mapped[int] = mapped_column(Integer, nullable=False)
    new_interval_days: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Ordering metadata only — see the module docstring.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")
    card: Mapped[ReviewCardModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        CheckConstraint("quality BETWEEN 0 AND 5", name="quality_in_range"),
        CheckConstraint("previous_interval_days >= 0", name="non_negative_previous_interval"),
        CheckConstraint("new_interval_days >= 0", name="non_negative_new_interval"),
        Index("ix_review_logs_card", "card_id", "reviewed_at"),
        Index("ix_review_logs_user", "user_id", "reviewed_at"),
        # The daily rollup's day filter neither user nor card leads with, so it needs the
        # bare column the two composites above put second.
        Index("ix_review_logs_reviewed_at", "reviewed_at"),
    )
