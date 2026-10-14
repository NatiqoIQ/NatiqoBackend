"""SQLAlchemy implementations of the attempt-engine ports.

``get_for_user`` carries the ownership predicate in the query rather than checking it
after a fetch, so a session id belonging to another account reads as absent. The unique
constraints on ``(session_id, question_id)`` and ``(user_id, lesson_id)`` are the other
half of the invariants the service enforces in logic; a race between two requests hits the
database rather than double-counting.

None of these methods commit. The unit of work owns the transaction, so a completion that
touches the session, its answers and the learner's progress either lands whole or not at
all.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import UserId
from app.modules.quiz.domain.entities import LessonProgress, QuizAnswer, QuizSession
from app.modules.quiz.domain.ports import (
    LessonProgressRepositoryPort,
    QuizAnswerRepositoryPort,
    QuizSessionRepositoryPort,
)
from app.modules.quiz.infrastructure.mappers import (
    apply_answer,
    apply_progress,
    apply_session,
    to_answer,
    to_answer_model,
    to_progress,
    to_progress_model,
    to_session,
    to_session_model,
)
from app.modules.quiz.infrastructure.models import (
    LessonProgressModel,
    QuizAnswerModel,
    QuizSessionModel,
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


__all__ = [
    "SqlAlchemyLessonProgressRepository",
    "SqlAlchemyQuizAnswerRepository",
    "SqlAlchemyQuizSessionRepository",
]
