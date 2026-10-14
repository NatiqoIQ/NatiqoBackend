"""Read models for spaced repetition.

The queue hands the client a :class:`~app.modules.quiz.application.dto.QuestionView` — the
same answer-free projection a live session uses — so a review item is rendered by the same
code as a lesson item and the answer key still never crosses the wire before the learner
commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.modules.quiz.application.dto import QuestionView

__all__ = [
    "DueCardView",
    "DueCardsView",
    "ReviewPreviewView",
    "ReviewStatsView",
    "ReviewedCardView",
]


@dataclass(frozen=True, slots=True)
class DueCardView:
    """One card waiting to be reviewed, with the question it points at."""

    card_id: uuid.UUID
    due_at: datetime
    state: str
    interval_days: int
    repetitions: int
    question: QuestionView


@dataclass(frozen=True, slots=True)
class DueCardsView:
    """A page of the review queue, plus how much is owed beyond it."""

    cards: tuple[DueCardView, ...]
    has_more: bool
    #: How many cards are due in total, so the client can show "3 of 12" without paging.
    due_count: int


@dataclass(frozen=True, slots=True)
class ReviewedCardView:
    """The verdict on one review, plus the card's new schedule.

    Mirrors :class:`~app.modules.quiz.application.dto.GradedAnswerView`'s verdict fields —
    the correct answer and the explanation arrive only here, after the learner has
    answered — and adds the SM-2 numbers the client uses to say "next in 6 days".
    """

    is_correct: bool
    correct_answer: dict[str, str]
    explanation: str
    points_awarded: int
    quality: int
    due_at: datetime
    interval_days: int
    repetitions: int
    lapses: int
    state: str


@dataclass(frozen=True, slots=True)
class ReviewStatsView:
    """The review screen's headline figures."""

    total_cards: int
    due_today: int
    future: int
    reviewed_total: int
    retention_percent: int


@dataclass(frozen=True, slots=True)
class ReviewPreviewView:
    """Just enough to badge a "review" entry point without loading the queue."""

    due_count: int
