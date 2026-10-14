"""Persistence ports for spaced repetition.

Stated in domain terms and, like the attempt engine's ports, entirely non-committing:
the unit of work owns the transaction so scheduling a card and appending its log land
together or not at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.types import UserId
from app.modules.quiz.domain.review import ReviewCard, ReviewLog, ReviewLogStats

__all__ = [
    "ReviewCardRepositoryPort",
    "ReviewLogRepositoryPort",
]


@runtime_checkable
class ReviewCardRepositoryPort(Protocol):
    async def add(self, card: ReviewCard) -> None: ...

    async def get_for_user(self, card_id: uuid.UUID, user_id: UserId) -> ReviewCard | None:
        """One card, only when it belongs to ``user_id``.

        Ownership is part of the query, so a card id belonging to another account reads
        as absent rather than leaking whether it exists.
        """
        ...

    async def get_for_question(self, user_id: UserId, question_id: uuid.UUID) -> ReviewCard | None:
        """The learner's card for a question, or ``None``.

        The read half of the ``UNIQUE (user_id, question_id)`` rule; used to make card
        creation idempotent so a question answered correctly twice is scheduled once.
        """
        ...

    async def save(self, card: ReviewCard) -> None:
        """Persist a mutated card. Inserts when the row is not there yet."""
        ...

    async def list_due(self, user_id: UserId, *, now: datetime, limit: int) -> list[ReviewCard]:
        """Cards due at or before ``now``, soonest first.

        Ordered by ``(due_at, id)`` so the queue is stable, and fetches ``limit + 1``
        rows so the service can report whether another page exists without a second
        query.
        """
        ...

    async def count_due(self, user_id: UserId, *, now: datetime) -> int:
        """How many of the learner's cards are due at or before ``now``."""
        ...

    async def list_for_user(self, user_id: UserId) -> list[ReviewCard]:
        """Every card the learner owns.

        Unpaginated: it backs the stats roll-up, which needs the whole set at once, and a
        learner has at most one card per published question.
        """
        ...


@runtime_checkable
class ReviewLogRepositoryPort(Protocol):
    async def add(self, log: ReviewLog) -> None: ...

    async def stats_for_user(self, user_id: UserId) -> ReviewLogStats:
        """Total and successful review counts across all of the learner's reviews."""
        ...
