"""Quiz module — SQLAlchemy models.

Three tables:

* ``quiz_sessions`` — one sitting. ``question_ids`` is a JSON array frozen at start; the
  total is derived from it rather than stored so there is a single source of truth for
  "how many questions was this learner scored on".
* ``quiz_answers`` — one row per ``(session, question)``, with a ``UNIQUE
  (session_id, question_id)`` that makes a double submission a database error rather than
  a doubled score. A retry after a wrong answer re-grades that row in place, so the row
  count stays equal to the questions the learner has committed to.
* ``lesson_progress`` — the durable per-learner record, ``UNIQUE (user_id, lesson_id)`` so
  a retry updates the row instead of stacking a new one.

The ``relationship()``s here carry no navigation the code uses (they are read with
``lazy="raise_on_sql"``); they exist so the unit of work knows the insert order. A user or
lesson row can be created in the same flush as a session during seeding, and without the
dependency edge the ORM would order inserts alphabetically and write ``quiz_sessions``
ahead of ``users``, failing the foreign key.

Every table carries the composite index its exact read uses: resume and progress hit
``(user_id, status)`` and ``(user_id, state)``; replaying a session's answers hits
``(session_id, position)``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.modules.content.infrastructure.models import LessonModel, QuestionModel
from app.modules.quiz.domain.entities import ProgressState, SessionMode, SessionStatus
from app.modules.users.infrastructure.models import UserModel

#: Written into every ``CHECK`` on ``mode`` / ``status`` / ``state``. Module constants so
#: the models and the migration cannot drift by being edited separately.
SESSION_MODES: tuple[str, ...] = tuple(mode.value for mode in SessionMode)
SESSION_MODES_SQL: str = ", ".join(f"'{mode}'" for mode in SESSION_MODES)

SESSION_STATUSES: tuple[str, ...] = tuple(status.value for status in SessionStatus)
SESSION_STATUSES_SQL: str = ", ".join(f"'{status}'" for status in SESSION_STATUSES)

PROGRESS_STATES: tuple[str, ...] = tuple(state.value for state in ProgressState)
PROGRESS_STATES_SQL: str = ", ".join(f"'{state}'" for state in PROGRESS_STATES)


class QuizSessionModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "quiz_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(20), nullable=False, default=SessionMode.LESSON.value)
    #: ``SET NULL`` rather than ``CASCADE``: retiring a lesson should not erase the record
    #: that learners attempted it. A session with no lesson is still a session.
    lesson_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("lessons.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SessionStatus.ACTIVE.value
    )
    #: The question order, frozen at start. A list of stringified UUIDs.
    question_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The session's lease. Expiry is evaluated lazily on read — see ``QuizSession``.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    correct_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    xp_awarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Ordering metadata only — see the module docstring. Never navigated.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")
    lesson: Mapped[LessonModel | None] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        CheckConstraint(f"mode IN ({SESSION_MODES_SQL})", name="valid_mode"),
        CheckConstraint(f"status IN ({SESSION_STATUSES_SQL})", name="valid_status"),
        CheckConstraint("correct_count >= 0", name="non_negative_correct_count"),
        CheckConstraint("xp_awarded >= 0", name="non_negative_xp"),
        Index("ix_quiz_sessions_user_status", "user_id", "status"),
        Index("ix_quiz_sessions_lesson", "lesson_id"),
    )


class QuizAnswerModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One graded answer. Written once, never updated."""

    __tablename__ = "quiz_answers"

    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("quiz_sessions.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    #: The question's catalogue position, copied so an answer list can be ordered without
    #: joining back to a question that may since have been retired.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    #: What the learner sent, in wire shape: ``{"choice": "..."}`` or ``{"text": "..."}``.
    given_answer: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    points_awarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The rationale as it stood when the answer was graded. A copy, not a reference: a
    #: revised question must not rewrite feedback a learner already received.
    explanation: Mapped[str | None] = mapped_column(Text)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Ordering metadata only — see the module docstring.
    session: Mapped[QuizSessionModel] = relationship(lazy="raise_on_sql")
    question: Mapped[QuestionModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        # The write half of "one answer per question"; the repository read is the other.
        UniqueConstraint("session_id", "question_id", name="uq_quiz_answers_session_question"),
        CheckConstraint("position >= 1", name="positive_position"),
        CheckConstraint("points_awarded >= 0", name="non_negative_points"),
        Index("ix_quiz_answers_session", "session_id", "position"),
    )


class LessonProgressModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "lesson_progress"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ProgressState.IN_PROGRESS.value
    )
    #: Best percentage across attempts. ``SmallInteger`` is ample for 0..100.
    best_score: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    xp_earned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Ordering metadata only — see the module docstring.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")
    lesson: Mapped[LessonModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        UniqueConstraint("user_id", "lesson_id", name="uq_lesson_progress_user_lesson"),
        CheckConstraint(f"state IN ({PROGRESS_STATES_SQL})", name="valid_state"),
        CheckConstraint("best_score BETWEEN 0 AND 100", name="score_in_range"),
        CheckConstraint("attempts >= 0", name="non_negative_attempts"),
        Index("ix_lesson_progress_user_state", "user_id", "state"),
    )
