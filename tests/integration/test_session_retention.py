"""Abandoned quiz-session retention (P2-3).

A session someone opened and never answered is storage with no history: deleting it
reclaims space. A session with even one answer is history: deleting it would rewrite the
activity and accuracy figures after the fact, so it must survive regardless of age.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)
from app.modules.quiz.domain.entities import SessionStatus
from app.modules.quiz.infrastructure.models import QuizAnswerModel, QuizSessionModel
from app.modules.quiz.infrastructure.repositories import SqlAlchemyQuizSessionRepository
from app.modules.users.infrastructure.models import UserModel


def _session(user_id: uuid.UUID, *, status: str, created_at) -> QuizSessionModel:
    return QuizSessionModel(
        id=uuid.uuid4(),
        user_id=user_id,
        mode="lesson",
        status=status,
        question_ids=[],
        started_at=created_at,
        expires_at=created_at + timedelta(minutes=30),
    )


async def _question(db: AsyncSession) -> QuestionModel:
    level = LevelModel(id=uuid.uuid4(), code="A1", title="Beginner", position=1, state="published")
    db.add(level)
    await db.flush()
    unit = UnitModel(
        id=uuid.uuid4(), level_id=level.id, title="Basics", position=1, state="published"
    )
    db.add(unit)
    await db.flush()
    lesson = LessonModel(
        id=uuid.uuid4(),
        unit_id=unit.id,
        title="Greetings",
        summary="Hello",
        position=1,
        estimated_minutes=5,
        state="published",
    )
    db.add(lesson)
    await db.flush()
    question = QuestionModel(
        id=uuid.uuid4(),
        lesson_id=lesson.id,
        position=1,
        kind="multiple_choice",
        skill="vocabulary",
        prompt="Which one means hello?",
        choices=["Hello", "Goodbye"],
        accepted_answers=[],
        correct_index=0,
        points=1,
    )
    db.add(question)
    await db.flush()
    return question


@pytest.mark.asyncio
async def test_only_old_unanswered_active_sessions_are_deleted(session: AsyncSession) -> None:
    now = utc_now()
    old = now - timedelta(days=30)
    user = UserModel(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", status="active")
    session.add(user)
    await session.flush()
    question = await _question(session)

    abandoned = _session(user.id, status=SessionStatus.ACTIVE.value, created_at=old)
    answered = _session(user.id, status=SessionStatus.ACTIVE.value, created_at=old)
    fresh = _session(user.id, status=SessionStatus.ACTIVE.value, created_at=now)
    finished = _session(user.id, status=SessionStatus.COMPLETED.value, created_at=old)
    session.add_all([abandoned, answered, fresh, finished])
    await session.flush()
    session.add(
        QuizAnswerModel(
            id=uuid.uuid4(),
            session_id=answered.id,
            question_id=question.id,
            position=1,
            given_answer={"choice": "Hello"},
            is_correct=True,
            points_awarded=1,
            attempt=1,
            answered_at=now,
        )
    )
    await session.commit()

    removed = await SqlAlchemyQuizSessionRepository(session).purge_abandoned(
        before=now - timedelta(days=7), limit=100
    )
    await session.commit()

    assert removed == 1
    remaining = set((await session.execute(select(QuizSessionModel.id))).scalars().all())
    assert remaining == {answered.id, fresh.id, finished.id}


@pytest.mark.asyncio
async def test_the_batch_limit_is_respected(session: AsyncSession) -> None:
    now = utc_now()
    old = now - timedelta(days=30)
    user = UserModel(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", status="active")
    session.add(user)
    await session.flush()
    session.add_all(
        [_session(user.id, status=SessionStatus.ACTIVE.value, created_at=old) for _ in range(5)]
    )
    await session.commit()

    repo = SqlAlchemyQuizSessionRepository(session)
    first = await repo.purge_abandoned(before=now - timedelta(days=7), limit=2)
    await session.commit()
    second = await repo.purge_abandoned(before=now - timedelta(days=7), limit=2)
    await session.commit()

    assert first == 2
    assert second == 2
