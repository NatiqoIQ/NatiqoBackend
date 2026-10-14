"""Spaced-repetition domain: the SM-2 schedule and the card it lives on.

The scheduler is deliberately pure arithmetic and knows nothing about storage, HTTP
or time zones: given a card's current numbers and how well the learner recalled it, it
returns the next numbers. That keeps the one piece of this module that decides *when a
learner should see a question again* testable with plain integers and no database.

Two shapes live here:

- :class:`ReviewCard` — the scheduling state for one ``(user, question)`` pair. It is
  mutable on purpose, matching the project's other long-lived aggregates
  (:class:`~app.modules.quiz.domain.entities.QuizSession`,
  :class:`~app.modules.quiz.domain.entities.LessonProgress`): the row is updated in place
  as it is reviewed rather than replaced. ``schedule`` is the only mutation entry point.
- :class:`ReviewLog` — an immutable, append-only record of one review, kept so the
  learner's retention percentage can be answered without re-deriving it from the card.

Grades use the classic SM-2 0–5 quality scale. The engine only ever produces two of
those values — a correct first attempt is a 5, anything else is a 2 (see
:func:`quality_for`) — but the arithmetic admits the whole range so a future grading
change does not have to touch this module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from app.core.time import utc_now
from app.core.types import UserId, new_id

__all__ = [
    "DEFAULT_EASE_FACTOR",
    "MIN_EASE_FACTOR",
    "PASSING_QUALITY",
    "REVIEW_INITIAL_INTERVAL_DAYS",
    "ReviewCard",
    "ReviewLog",
    "ReviewLogStats",
    "ReviewState",
    "new_card",
    "next_ease",
    "next_interval",
    "quality_for",
]

#: Ease factor a card starts life with. 2.5 is the SM-2 default and the value a
#: never-reviewed card is expected to grow its interval by.
DEFAULT_EASE_FACTOR = 2.5

#: The floor an ease factor may never drop below. Below this, intervals stop growing
#: and hard cards would come back faster than the learner can keep up with.
MIN_EASE_FACTOR = 1.3

#: Grades at or above this count as a successful recall. A failure resets the card's
#: repetition run and routes it back to relearning.
PASSING_QUALITY = 3

#: Interval, in days, a card is given the first time it is scheduled. Kept as a named
#: constant so ``new_card`` and ``next_interval`` cannot drift apart.
REVIEW_INITIAL_INTERVAL_DAYS = 1


class ReviewState(StrEnum):
    """Where a card sits in its repetition lifecycle.

    ``NEW`` — created, never reviewed. ``LEARNING`` — answered correctly once, on a
    short interval. ``REVIEW`` — graduated past the learning steps. ``RELEARNING`` —
    a previously-seen card that was just failed and is being rebuilt.
    """

    NEW = "new"
    LEARNING = "learning"
    REVIEW = "review"
    RELEARNING = "relearning"


@dataclass(slots=True)
class ReviewCard:
    """The scheduling state for one question, for one learner.

    Mutable: :meth:`schedule` advances it in place and the repository writes the changed
    fields back, the same way a live quiz session is updated. Every other field is set
    once at construction and only read afterwards.
    """

    id: uuid.UUID
    user_id: UserId
    question_id: uuid.UUID
    ease_factor: float
    interval_days: int
    repetitions: int
    lapses: int
    due_at: datetime
    last_reviewed_at: datetime | None
    state: ReviewState

    def schedule(self, quality: int, at: datetime | None = None) -> None:
        """Advance this card by one review graded ``quality`` (0–5).

        The interval is computed from the ease factor *as it stood before this review*
        — the canonical SM-2 ordering — and the ease factor is then nudged by how well
        the recall went. A failing grade throws the repetition run away and brings the
        card back immediately, counting a lapse.
        """
        moment = at or utc_now()
        if quality < PASSING_QUALITY:
            self.repetitions = 0
            self.interval_days = REVIEW_INITIAL_INTERVAL_DAYS
            self.lapses += 1
            self.state = ReviewState.RELEARNING
        else:
            self.interval_days = next_interval(
                repetitions=self.repetitions,
                interval_days=self.interval_days,
                ease_factor=self.ease_factor,
            )
            self.repetitions += 1
            self.state = ReviewState.REVIEW if self.repetitions >= 2 else ReviewState.LEARNING
        self.ease_factor = next_ease(self.ease_factor, quality)
        self.last_reviewed_at = moment
        self.due_at = moment + timedelta(days=self.interval_days)


@dataclass(frozen=True, slots=True)
class ReviewLog:
    """One review, recorded immutably for retention accounting.

    Written once and never updated, so it is frozen. ``previous_interval`` and
    ``new_interval`` are captured here rather than read back off the card, because the
    card only remembers the latest interval.
    """

    user_id: UserId
    card_id: uuid.UUID
    quality: int
    previous_interval_days: int
    new_interval_days: int
    id: uuid.UUID = field(default_factory=new_id)
    reviewed_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ReviewLogStats:
    """Aggregate review counts, from which retention is derived."""

    total: int
    correct: int

    @property
    def retention_percent(self) -> int:
        """Share of reviews that succeeded, rounded to a whole percent.

        ``correct`` counts grades at or above :data:`PASSING_QUALITY`. With no reviews
        yet the retention is reported as 0 rather than undefined.
        """
        if self.total <= 0:
            return 0
        return round(self.correct / self.total * 100)


def quality_for(is_correct: bool) -> int:
    """Map the attempt engine's correctness verdict onto the SM-2 quality scale.

    A correct first attempt is the strongest recall and scores a 5; anything else
    (a wrong answer, or a retry that only scored half) is a 2 — a failed recall that
    resets the run.
    """
    return 5 if is_correct else 2


def next_ease(ease_factor: float, quality: int) -> float:
    """The SM-2 ease update: nudge ``ease_factor`` by how well recall went.

    Never returns below :data:`MIN_EASE_FACTOR`.
    """
    delta = 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)
    return max(MIN_EASE_FACTOR, ease_factor + delta)


def next_interval(*, repetitions: int, interval_days: int, ease_factor: float) -> int:
    """The next gap, in days, before a successfully recalled card is shown again.

    The first two successful reviews are fixed steps (one day, then six); from the third
    on the previous interval is multiplied by the ease factor. ``repetitions`` is the
    count *before* this review is added.
    """
    if repetitions <= 0:
        return REVIEW_INITIAL_INTERVAL_DAYS
    if repetitions == 1:
        return 6
    return max(REVIEW_INITIAL_INTERVAL_DAYS, round(interval_days * ease_factor))


def new_card(user_id: UserId, question_id: uuid.UUID, *, now: datetime | None = None) -> ReviewCard:
    """A freshly-created card, due one day after ``now``.

    The learner has just answered the question correctly in a lesson, so this is the
    first time it enters review; it starts in :attr:`ReviewState.NEW` with the default
    ease and an interval of a single day.
    """
    moment = now or utc_now()
    return ReviewCard(
        id=new_id(),
        user_id=user_id,
        question_id=question_id,
        ease_factor=DEFAULT_EASE_FACTOR,
        interval_days=0,
        repetitions=0,
        lapses=0,
        due_at=moment + timedelta(days=REVIEW_INITIAL_INTERVAL_DAYS),
        last_reviewed_at=None,
        state=ReviewState.NEW,
    )
