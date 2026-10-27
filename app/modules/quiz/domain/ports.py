"""Persistence ports for the attempt engine.

Stated in domain terms, so the application layer never learns that these live in SQL.
Every method here is deliberately non-committing: the unit of work owns the transaction,
so a caller can grade an answer, bump the session counter and write progress as one
indivisible change rather than three that can half-succeed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.pagination import TimeCursor
from app.core.types import UserId
from app.modules.quiz.domain.entities import (
    AnswerHistory,
    AnswerTally,
    LessonProgress,
    QuizAnswer,
    QuizSession,
    UnitProgress,
)

__all__ = [
    "LessonProgressRepositoryPort",
    "QuizAnswerRepositoryPort",
    "QuizSessionRepositoryPort",
    "UnitProgressRepositoryPort",
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

    async def list_completed_for_user(
        self, user_id: UserId, *, limit: int, after: TimeCursor | None
    ) -> list[QuizSession]:
        """The learner's completed sessions, newest first, starting after ``after``.

        Ordered by ``(completed_at, id)`` descending — the order XP history reads in.
        Fetches ``limit + 1`` rows so the service can decide whether another page exists.
        """
        ...

    async def list_completion_times(self, user_id: UserId) -> list[datetime]:
        """Every completed session's ``completed_at``, newest first.

        The streak is a calendar question, so this returns instants and lets the service
        convert them to the learner's local dates. Answering it in SQL would mean
        hard-coding a timezone into the query, which is exactly the coupling
        ``app/core/time.py`` exists to avoid.
        """
        ...

    async def sum_words_learned_for_user(self, user_id: UserId) -> int:
        """Lifetime first-time words across every completed session.

        Sums the session column rather than the per-lesson ledger, so checkpoint and
        review sittings — which write no lesson row — still count towards the total,
        and so the summary equals what a client would get by adding up the XP feed
        page by page.
        """
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

    async def tally_for_user(self, user_id: UserId) -> AnswerTally:
        """Answered and correct counts across every answer in the learner's sessions.

        Spans all sessions, including abandoned ones, because accuracy is a property of
        the learner's practice rather than of any one sitting.
        """
        ...

    async def history_for_user(
        self, user_id: UserId, question_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, AnswerHistory]:
        """The learner's answer history for the given questions, keyed by question id.

        One grouped query over the whole batch — checkpoint selection asks about every
        question in a unit at once, and a per-question read would turn one fact-gathering
        step into dozens. Questions never answered simply do not appear in the mapping,
        so absence and ``AnswerHistory()`` mean the same thing and a caller cannot
        mistake a miss for a query failure.

        Spans every session the learner has, including checkpoints: "ever missed" is the
        whole point, and a miss recorded on a previous checkpoint must weight the next
        one just as a miss in a lesson does.
        """
        ...

    async def correct_question_ids_for_user(
        self,
        user_id: UserId,
        question_ids: Sequence[uuid.UUID],
        *,
        exclude_session_id: uuid.UUID,
    ) -> set[uuid.UUID]:
        """The subset of ``question_ids`` the learner already answered correctly in some
        *other* session.

        This is the read behind "a word counts once ever": completion subtracts this set
        from the sitting's correct vocabulary questions, so a replayed lesson, a retry of
        a missed question or a report retried from a second device cannot inflate a
        lifetime or daily total. The current session is excluded because its own correct
        answers are exactly what is being counted, not what they should be netted against.
        """
        ...


@runtime_checkable
class LessonProgressRepositoryPort(Protocol):
    async def get(self, user_id: UserId, lesson_id: uuid.UUID) -> LessonProgress | None: ...

    async def save(self, progress: LessonProgress) -> None:
        """Insert or update the learner's row for one lesson."""
        ...

    async def list_for_user(self, user_id: UserId) -> list[LessonProgress]:
        """Every progress row the learner owns.

        Unpaginated: it backs the summary roll-up and the unlock map, both of which need
        the whole set at once, and a learner can have at most one row per published lesson.
        """
        ...


@runtime_checkable
class UnitProgressRepositoryPort(Protocol):
    async def get(self, user_id: UserId, unit_id: uuid.UUID) -> UnitProgress | None: ...

    async def save(self, progress: UnitProgress) -> None:
        """Insert or update the learner's checkpoint record for one unit."""
        ...

    async def list_for_user(self, user_id: UserId) -> list[UnitProgress]:
        """Every checkpoint record the learner owns.

        Unpaginated for the same reason as lesson progress: the unlock map evaluates a
        unit against its predecessor in order, so it needs the whole set in one read, and
        a learner can have at most one row per unit.
        """
        ...
