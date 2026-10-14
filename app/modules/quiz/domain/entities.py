"""The attempt engine's aggregates: a session, its answers, a lesson's progress.

Three shapes, kept apart because they change for different reasons:

* :class:`QuizSession` — one sitting with a lesson. Its question list is frozen at
  start, so a page reload, an app kill or an editor publishing mid-lesson cannot change
  the questions a learner already committed to.
* :class:`QuizAnswer` — one row per question, holding the learner's latest attempt. It
  snapshots the verdict *and* the explanation at the moment it was graded, so revising a
  question row cannot retroactively change what a learner was told. A retry re-grades
  that same row (half marks, see ``domain/policy.py``) instead of appending a second one,
  which is what keeps a question counting exactly once towards a session's total.
* :class:`LessonProgress` — the durable per-learner record a lesson was attempted and how
  it went. It is the only one of the three that outlives a session on purpose.

The engine grades on the server and never hands the client an answer key; see
``domain/policy.py`` for the rule — including what a second attempt is worth — and
``presentation/schemas.py`` for the wire contract.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.core.time import ensure_aware, utc_now
from app.core.types import UserId

__all__ = [
    "AnswerTally",
    "LessonProgress",
    "ProgressState",
    "QuizAnswer",
    "QuizSession",
    "SessionMode",
    "SessionStatus",
]


class SessionMode(StrEnum):
    """What a session is for.

    Only ``LESSON`` can be started today; ``REVIEW`` and ``PLACEMENT`` are named now so
    the column, the constraint and the wire enum do not have to be reshaped when the SRS
    and onboarding slices land. A mode with no way to be reached is rejected at the
    service boundary rather than silently treated as a lesson.
    """

    LESSON = "lesson"
    REVIEW = "review"
    PLACEMENT = "placement"


class SessionStatus(StrEnum):
    """Lifecycle of one sitting. ``EXPIRED`` and ``ABANDONED`` are both terminal and
    both mean "this session can no longer be answered"; the difference is only *why*."""

    ACTIVE = "active"
    COMPLETED = "completed"
    EXPIRED = "expired"
    ABANDONED = "abandoned"


class ProgressState(StrEnum):
    """How far a learner has got with one lesson.

    ``LOCKED`` and ``AVAILABLE`` are not written by this slice — sequential unlocking is a
    later phase — but they are part of the stored vocabulary so the state column does not
    need a migration to grow a value.
    """

    LOCKED = "locked"
    AVAILABLE = "available"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass(slots=True)
class QuizSession:
    """One attempt at a lesson.

    ``question_ids`` is the ordering authority. It is fixed when the session starts and
    never re-derived from the lesson, because a lesson edited mid-session must not change
    the set of questions a learner is being scored on. ``total`` is derived from it rather
    than stored, so there is exactly one place that can be wrong.
    """

    id: uuid.UUID
    user_id: UserId
    mode: SessionMode
    lesson_id: uuid.UUID | None
    question_ids: tuple[uuid.UUID, ...]
    expires_at: datetime
    status: SessionStatus = SessionStatus.ACTIVE
    started_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    #: Denormalised running counters. Kept on the row so resuming a session is one read
    #: instead of an aggregate over every answer.
    correct_count: int = 0
    xp_awarded: int = 0

    @property
    def total(self) -> int:
        return len(self.question_ids)

    @property
    def is_active(self) -> bool:
        return self.status is SessionStatus.ACTIVE

    def contains(self, question_id: uuid.UUID) -> bool:
        return question_id in self.question_ids

    def is_expired(self, at: datetime | None = None) -> bool:
        """Whether the session's lease has run out.

        A lease rather than a hard deadline: the client is expected to finish well inside
        it, and the check is lazy on read (no sweeper job) so a session that outlives a
        server restart still expires correctly. See ``backend-roadmap-ar.md`` §3 for why
        lazy expiry is the chosen pattern.
        """
        return ensure_aware(self.expires_at) <= (at or utc_now())

    def register_correct(self) -> None:
        self.correct_count += 1

    def mark_expired(self, at: datetime | None = None) -> None:
        if self.status is SessionStatus.ACTIVE:
            self.status = SessionStatus.EXPIRED
            self.completed_at = at or utc_now()

    def complete(self, *, xp_awarded: int, at: datetime | None = None) -> None:
        self.status = SessionStatus.COMPLETED
        self.xp_awarded = xp_awarded
        self.completed_at = at or utc_now()

    def abandon(self, at: datetime | None = None) -> None:
        if self.status is SessionStatus.ACTIVE:
            self.status = SessionStatus.ABANDONED
            self.completed_at = at or utc_now()


@dataclass(frozen=True, slots=True)
class QuizAnswer:
    """The learner's current answer to one question in one session.

    Exactly one row per ``(session, question)``: answering a question that was missed
    again *replaces* this record rather than adding to it, which is what keeps the
    session's answered count equal to the number of questions. ``points_awarded`` then
    carries the halved value a retry earns (see ``domain.policy``).

    ``explanation`` is a copy of the question's rationale taken at grading time, and
    ``is_correct`` / ``points_awarded`` are the verdict from the most recent attempt.
    Copying the rationale means a learner's feedback stays truthful even after the
    underlying question is reworded or its key corrected.
    """

    id: uuid.UUID
    session_id: uuid.UUID
    question_id: uuid.UUID
    position: int
    given_answer: dict[str, str]
    is_correct: bool
    points_awarded: int
    explanation: str
    answered_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class LessonProgress:
    """One learner's durable record against one lesson.

    Created when a lesson is first attempted and updated on every completion, so it is
    the row a "my progress" screen will read once that slice exists.
    """

    id: uuid.UUID
    user_id: UserId
    lesson_id: uuid.UUID
    state: ProgressState = ProgressState.IN_PROGRESS
    best_score: int = 0
    attempts: int = 0
    xp_earned: int = 0
    completed_at: datetime | None = None

    def start(self) -> None:
        """Mark the lesson as attempted, without downgrading a finished one."""
        if self.state is not ProgressState.COMPLETED:
            self.state = ProgressState.IN_PROGRESS

    def record_completion(self, *, score_percent: int, xp: int, at: datetime | None = None) -> None:
        """Fold one finished attempt in.

        ``best_score`` keeps the maximum and ``xp_earned`` accumulates, so a retry can
        only ever improve the record — a learner who does worse on a second pass is not
        punished for having tried again.
        """
        self.attempts += 1
        self.xp_earned += xp
        self.best_score = max(self.best_score, score_percent)
        self.state = ProgressState.COMPLETED
        self.completed_at = at or utc_now()


@dataclass(frozen=True, slots=True)
class AnswerTally:
    """How many questions a learner has answered, and how many they got right.

    A read-only roll-up rather than a stored counter: the session row already keeps its
    own ``correct_count``, but an "accuracy" figure has to span every session including
    abandoned ones, so it is counted from the answers themselves. The percentage lives
    here, next to the two numbers it is derived from, so no caller can divide by zero.
    """

    answered: int = 0
    correct: int = 0

    @property
    def accuracy_percent(self) -> int:
        if self.answered <= 0:
            return 0
        return round(self.correct / self.answered * 100)

