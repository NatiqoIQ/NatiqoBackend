"""Persistence ports for the attempt engine.

Stated in domain terms, so the application layer never learns that these live in SQL.
Every method here is deliberately non-committing: the unit of work owns the transaction,
so a caller can grade an answer, bump the session counter and write progress as one
indivisible change rather than three that can half-succeed.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.core.types import UserId
from app.modules.quiz.domain.entities import (
    LessonProgress,
    QuizAnswer,
    QuizSession,
)

__all__ = [
    "LessonProgressRepositoryPort",
    "QuizAnswerRepositoryPort",
    "QuizSessionRepositoryPort",
]


@runtime_checkable
class QuizSessionRepositoryPort(Protocol):
    async def add(self, session: QuizSession) -> None: ...

    async def get(self, session_id: uuid.UUID) -> QuizSession | None: ...

    async def get_for_user(self, session_id: uuid.UUID, user_id: UserId) -> QuizSession | None:
        """One session, only when it belongs to ``user_id``.

        Ownership is part of the query rather than a check the caller remembers, so a
        session id guessed from another account reads as "not found" instead of leaking
        whether it exists.
        """
        ...

    async def save(self, session: QuizSession) -> None:
        """Persist a mutated session. Inserts when the row is not there yet."""
        ...


@runtime_checkable
class QuizAnswerRepositoryPort(Protocol):
    async def add(self, answer: QuizAnswer) -> None: ...

    async def save(self, answer: QuizAnswer) -> None:
        """Persist a re-graded answer. Inserts when the row is not there yet.

        A retry writes over the learner's earlier answer for the same question rather than
        appending a second row, so ``UNIQUE (session_id, question_id)`` still holds and a
        question still counts once towards the session total.
        """
        ...

    async def get(self, session_id: uuid.UUID, question_id: uuid.UUID) -> QuizAnswer | None:
        """The answer already recorded for a question, or ``None``.

        This is the read half of the "one row per question" rule; the unique constraint is
        the write half, so a race cannot get past both. The caller inspects ``is_correct``
        to tell a question that is finished from one that may be retried.
        """
        ...

    async def list_for_session(self, session_id: uuid.UUID) -> list[QuizAnswer]: ...

    async def count_for_session(self, session_id: uuid.UUID) -> int: ...


@runtime_checkable
class LessonProgressRepositoryPort(Protocol):
    async def get(self, user_id: UserId, lesson_id: uuid.UUID) -> LessonProgress | None: ...

    async def save(self, progress: LessonProgress) -> None:
        """Insert or update the learner's row for one lesson."""
        ...
