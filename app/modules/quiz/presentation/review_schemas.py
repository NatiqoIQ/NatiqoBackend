"""Wire schemas for the spaced-repetition surface.

Fields are listed by hand, the same way the catalogue and progress do, so adding something to
a read model cannot silently start leaking. A due card carries a
:class:`~app.modules.quiz.presentation.schemas.SessionQuestionResponse` — the same
answer-free projection a live session uses — so the answer key still never crosses the wire
before the learner answers.

``from_attributes`` (via ``_ReviewWire``) is what lets a route hand a read model straight to
a response model; the question is nested by hand in the route because
``SessionQuestionResponse`` is built by omission rather than attribute-copied.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import ConfigDict, Field

from app.core.responses import WireModel
from app.modules.quiz.presentation.schemas import SessionQuestionResponse

__all__ = [
    "DueCardResponse",
    "DueCardsResponse",
    "ReviewPreviewResponse",
    "ReviewStatsResponse",
    "ReviewedCardResponse",
]


class _ReviewWire(WireModel):
    """A wire model that reads attributes off a read model.

    Subclass rather than repeating ``model_config`` on each schema, so the camelCase aliasing
    and ``from_attributes`` cannot drift apart by one of them being edited.
    """

    model_config = ConfigDict(from_attributes=True)


class DueCardResponse(_ReviewWire):
    """One card waiting to be reviewed, with the question it points at."""

    card_id: uuid.UUID
    due_at: datetime
    state: str
    interval_days: int = Field(ge=0)
    repetitions: int = Field(ge=0)
    question: SessionQuestionResponse


class DueCardsResponse(_ReviewWire):
    """A page of the review queue, plus how much is owed beyond it."""

    cards: list[DueCardResponse] = Field(default_factory=list)
    has_more: bool
    due_count: int = Field(ge=0, description="Cards due in total, not just this page.")


class ReviewedCardResponse(_ReviewWire):
    """The verdict on one review, plus the card's new schedule.

    ``correctAnswer`` and ``explanation`` arrive only here, after the learner has answered —
    the same contract the attempt engine keeps. The SM-2 fields let the client say "next in
    6 days".
    """

    is_correct: bool
    correct_answer: dict[str, str] = Field(default_factory=dict)
    explanation: str
    points_awarded: int = Field(ge=0)
    quality: int = Field(ge=0, le=5)
    due_at: datetime
    interval_days: int = Field(ge=0)
    repetitions: int = Field(ge=0)
    lapses: int = Field(ge=0)
    state: str


class ReviewStatsResponse(_ReviewWire):
    """The review screen's headline figures."""

    total_cards: int = Field(ge=0)
    due_today: int = Field(ge=0)
    future: int = Field(ge=0)
    reviewed_total: int = Field(ge=0)
    retention_percent: int = Field(ge=0, le=100)


class ReviewPreviewResponse(_ReviewWire):
    """Just enough to badge a "review" entry point without loading the queue."""

    due_count: int = Field(ge=0)
