"""SQLAlchemy implementations of the attempt-engine ports.

``get_for_user`` carries the ownership predicate in the query rather than checking it
after a fetch, so a session id belonging to another account reads as absent. The unique
constraints on ``(session_id, question_id)``, ``(user_id, lesson_id)`` and
``(user_id, unit_id)`` are the other half of the invariants the service enforces in
logic; a race between two requests hits the database rather than double-counting.

None of these methods commit. The unit of work owns the transaction, so a completion that
touches the session, its answers and the learner's progress either lands whole or not at
all.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.pagination import TimeCursor
from app.core.types import UserId
from app.modules.quiz.domain.entities import (
    AnswerHistory,
    AnswerTally,
    LessonProgress,
    QuizAnswer,
    QuizSession,
    SessionStatus,
    UnitProgress,
)
from app.modules.quiz.domain.ports import (
    LessonProgressRepositoryPort,
    QuizAnswerRepositoryPort,
    QuizSessionRepositoryPort,
    UnitProgressRepositoryPort,
)
from app.modules.quiz.infrastructure.mappers import (
    apply_answer,
    apply_progress,
    apply_session,
    apply_unit_progress,
    to_answer,
    to_answer_model,
    to_progress,
    to_progress_model,
    to_session,
    to_session_model,
    to_unit_progress,
    to_unit_progress_model,
)
from app.modules.quiz.infrastructure.models import (
    LessonProgressModel,
    QuizAnswerModel,
    QuizSessionModel,
    UnitProgressModel,
)

_COMPLETED = SessionStatus.COMPLETED.value


def _completed_keyset_predicate(after: TimeCursor) -> ColumnElement[bool]:
    """``(completed_at, id) < (after.timestamp, after.id)`` for the newest-first order.

    The mirror of the catalogue's ascending keyset: the history reads newest first, so a
    further page is everything strictly older, and a tie on the instant is broken by id in
    the same descending direction.
    """
    return or_(
        QuizSessionModel.completed_at < after.timestamp,
        and_(
            QuizSessionModel.completed_at == after.timestamp,
            QuizSessionModel.id < after.id,
        ),
    )


class SqlAlchemyQuizSessionRepository(QuizSessionRepositoryPort):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, session: QuizSession) -> None:
        self._session.add(to_session_model(session))

    async def get(self, session_id: uuid.UUID) -> QuizSession | None:
        model = await self._session.get(QuizSessionModel, session_id)
        return to_session(model) if model is not None else None

    async def get_for_user(self, session_id: uuid.UUID, user_id: UserId) -> QuizSession | None:
        stmt = select(QuizSessionModel).where(
            QuizSessionModel.id == session_id,
            QuizSessionModel.user_id == user_id,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_session(model) if model is not None else None

    async def save(self, session: QuizSession) -> None:
        model = await self._session.get(QuizSessionModel, session.id)
        if model is None:
            self._session.add(to_session_model(session))
            return
        apply_session(session, model)

    async def list_completed_for_user(
        self, user_id: UserId, *, limit: int, after: TimeCursor | None
    ) -> list[QuizSession]:
        stmt = select(QuizSessionModel).where(
            QuizSessionModel.user_id == user_id,
            QuizSessionModel.status == _COMPLETED,
            QuizSessionModel.completed_at.is_not(None),
        )
        if after is not None:
            stmt = stmt.where(_completed_keyset_predicate(after))
        stmt = stmt.order_by(
            QuizSessionModel.completed_at.desc(),
            QuizSessionModel.id.desc(),
        ).limit(limit)
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_session(model) for model in models]

    async def list_completion_times(self, user_id: UserId) -> list[datetime]:
        stmt = (
            select(QuizSessionModel.completed_at)
            .where(
                QuizSessionModel.user_id == user_id,
                QuizSessionModel.status == _COMPLETED,
                QuizSessionModel.completed_at.is_not(None),
            )
            .order_by(QuizSessionModel.completed_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [moment for moment in rows if moment is not None]

    async def sum_words_learned_for_user(self, user_id: UserId) -> int:
        """See :meth:`QuizSessionRepositoryPort.sum_words_learned_for_user`.

        One aggregate over the completed sessions rather than the lesson ledger, because
        a checkpoint sitting earns no lesson row but does earn words, and a feed page
        summing to a different figure than the summary is the kind of drift a client
        cannot work around.
        """
        stmt = (
            select(func.coalesce(func.sum(QuizSessionModel.words_learned), 0))
            .select_from(QuizSessionModel)
            .where(
                QuizSessionModel.user_id == user_id,
                QuizSessionModel.status == _COMPLETED,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())


class SqlAlchemyQuizAnswerRepository(QuizAnswerRepositoryPort):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, answer: QuizAnswer) -> None:
        self._session.add(to_answer_model(answer))

    async def save(self, answer: QuizAnswer) -> None:
        model = await self._session.get(QuizAnswerModel, answer.id)
        if model is None:
            self._session.add(to_answer_model(answer))
            return
        apply_answer(answer, model)

    async def get(self, session_id: uuid.UUID, question_id: uuid.UUID) -> QuizAnswer | None:
        stmt = select(QuizAnswerModel).where(
            QuizAnswerModel.session_id == session_id,
            QuizAnswerModel.question_id == question_id,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_answer(model) if model is not None else None

    async def list_for_session(self, session_id: uuid.UUID) -> list[QuizAnswer]:
        stmt = (
            select(QuizAnswerModel)
            .where(QuizAnswerModel.session_id == session_id)
            .order_by(QuizAnswerModel.position, QuizAnswerModel.id)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_answer(model) for model in models]

    async def count_for_session(self, session_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(QuizAnswerModel)
            .where(QuizAnswerModel.session_id == session_id)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def tally_for_user(self, user_id: UserId) -> AnswerTally:
        stmt = (
            select(
                func.count(),
                func.coalesce(
                    func.sum(case((QuizAnswerModel.is_correct.is_(True), 1), else_=0)),
                    0,
                ),
            )
            .select_from(QuizAnswerModel)
            .join(QuizSessionModel, QuizSessionModel.id == QuizAnswerModel.session_id)
            .where(QuizSessionModel.user_id == user_id)
        )
        answered, correct = (await self._session.execute(stmt)).one()
        return AnswerTally(answered=int(answered), correct=int(correct))

    async def history_for_user(
        self, user_id: UserId, question_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, AnswerHistory]:
        """See :meth:`QuizAnswerRepositoryPort.history_for_user`.

        A miss is counted two ways in one expression: the row's verdict is wrong, or the
        attempt number is past one — the second catching a question the engine overwrote
        with a successful retry, where the verdict alone would claim it was always known.
        The join reaches the owner through the session, because an answer row carries no
        user id of its own.
        """
        if not question_ids:
            return {}
        misses = case(
            (
                or_(
                    QuizAnswerModel.is_correct.is_(False),
                    QuizAnswerModel.attempt > 1,
                ),
                1,
            ),
            else_=0,
        )
        stmt = (
            select(
                QuizAnswerModel.question_id,
                func.count(),
                func.coalesce(func.sum(misses), 0),
            )
            .join(QuizSessionModel, QuizSessionModel.id == QuizAnswerModel.session_id)
            .where(
                QuizSessionModel.user_id == user_id,
                QuizAnswerModel.question_id.in_(list(question_ids)),
            )
            .group_by(QuizAnswerModel.question_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {
            question_id: AnswerHistory(
                answered=int(attempts) > 0,
                ever_wrong=int(miss_count) > 0,
            )
            for question_id, attempts, miss_count in rows
        }

    async def correct_question_ids_for_user(
        self,
        user_id: UserId,
        question_ids: Sequence[uuid.UUID],
        *,
        exclude_session_id: uuid.UUID,
    ) -> set[uuid.UUID]:
        """See :meth:`QuizAnswerRepositoryPort.correct_question_ids_for_user`.

        The join reaches the owner through the session — an answer row carries no user id
        — and the current session is excluded in SQL, not in Python, so the caller's
        set-subtraction cannot be defeated by a row that slipped past a post-filter.
        """
        if not question_ids:
            return set()
        stmt = (
            select(QuizAnswerModel.question_id)
            .join(QuizSessionModel, QuizSessionModel.id == QuizAnswerModel.session_id)
            .where(
                QuizSessionModel.user_id == user_id,
                QuizAnswerModel.question_id.in_(list(question_ids)),
                QuizAnswerModel.is_correct.is_(True),
                QuizAnswerModel.session_id != exclude_session_id,
            )
            .distinct()
        )
        return set((await self._session.execute(stmt)).scalars().all())


class SqlAlchemyLessonProgressRepository(LessonProgressRepositoryPort):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UserId, lesson_id: uuid.UUID) -> LessonProgress | None:
        stmt = select(LessonProgressModel).where(
            LessonProgressModel.user_id == user_id,
            LessonProgressModel.lesson_id == lesson_id,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_progress(model) if model is not None else None

    async def save(self, progress: LessonProgress) -> None:
        model = await self._session.get(LessonProgressModel, progress.id)
        if model is None:
            self._session.add(to_progress_model(progress))
            return
        apply_progress(progress, model)

    async def list_for_user(self, user_id: UserId) -> list[LessonProgress]:
        stmt = (
            select(LessonProgressModel)
            .where(LessonProgressModel.user_id == user_id)
            .order_by(LessonProgressModel.id)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_progress(model) for model in models]


class SqlAlchemyUnitProgressRepository(UnitProgressRepositoryPort):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UserId, unit_id: uuid.UUID) -> UnitProgress | None:
        stmt = select(UnitProgressModel).where(
            UnitProgressModel.user_id == user_id,
            UnitProgressModel.unit_id == unit_id,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_unit_progress(model) if model is not None else None

    async def save(self, progress: UnitProgress) -> None:
        model = await self._session.get(UnitProgressModel, progress.id)
        if model is None:
            self._session.add(to_unit_progress_model(progress))
            return
        apply_unit_progress(progress, model)

    async def list_for_user(self, user_id: UserId) -> list[UnitProgress]:
        stmt = (
            select(UnitProgressModel)
            .where(UnitProgressModel.user_id == user_id)
            .order_by(UnitProgressModel.unit_id)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_unit_progress(model) for model in models]


__all__ = [
    "SqlAlchemyLessonProgressRepository",
    "SqlAlchemyQuizAnswerRepository",
    "SqlAlchemyQuizSessionRepository",
    "SqlAlchemyUnitProgressRepository",
]
