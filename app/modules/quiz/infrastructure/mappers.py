"""Row → aggregate mappers for the attempt engine.

Hand-written, like the rest of the project, so the SQL shape can change without the
domain noticing. Conversion is deliberately one-directional for the immutable shapes
(:class:`~app.modules.quiz.domain.entities.QuizAnswer` is constructed, never written back
from a mutated entity) and two-directional for the mutable ones, which the repositories
update field by field.
"""

from __future__ import annotations

import uuid

from app.core.types import UserId
from app.modules.quiz.domain.entities import (
    LessonProgress,
    ProgressState,
    QuizAnswer,
    QuizSession,
    SessionMode,
    SessionStatus,
)
from app.modules.quiz.infrastructure.models import (
    LessonProgressModel,
    QuizAnswerModel,
    QuizSessionModel,
)


def to_session(model: QuizSessionModel) -> QuizSession:
    return QuizSession(
        id=model.id,
        user_id=UserId(model.user_id),
        mode=SessionMode(model.mode),
        lesson_id=model.lesson_id,
        question_ids=tuple(uuid.UUID(str(value)) for value in (model.question_ids or ())),
        status=SessionStatus(model.status),
        started_at=model.started_at,
        expires_at=model.expires_at,
        completed_at=model.completed_at,
        correct_count=model.correct_count,
        xp_awarded=model.xp_awarded,
    )


def to_answer(model: QuizAnswerModel) -> QuizAnswer:
    return QuizAnswer(
        id=model.id,
        session_id=model.session_id,
        question_id=model.question_id,
        position=model.position,
        given_answer=dict(model.given_answer or {}),
        is_correct=model.is_correct,
        points_awarded=model.points_awarded,
        attempt=model.attempt,
        explanation=model.explanation or "",
        answered_at=model.answered_at,
    )


def to_progress(model: LessonProgressModel) -> LessonProgress:
    return LessonProgress(
        id=model.id,
        user_id=UserId(model.user_id),
        lesson_id=model.lesson_id,
        state=ProgressState(model.state),
        best_score=model.best_score,
        attempts=model.attempts,
        xp_earned=model.xp_earned,
        completed_at=model.completed_at,
    )


def _session_question_ids(session: QuizSession) -> list[str]:
    return [str(question_id) for question_id in session.question_ids]


def to_session_model(session: QuizSession) -> QuizSessionModel:
    return QuizSessionModel(
        id=session.id,
        user_id=session.user_id,
        mode=session.mode.value,
        lesson_id=session.lesson_id,
        status=session.status.value,
        question_ids=_session_question_ids(session),
        started_at=session.started_at,
        expires_at=session.expires_at,
        completed_at=session.completed_at,
        correct_count=session.correct_count,
        xp_awarded=session.xp_awarded,
    )


def apply_session(session: QuizSession, model: QuizSessionModel) -> None:
    """Copy a mutated session onto its row.

    Only the fields the engine changes are written; ``question_ids`` is intentionally not
    among them, because the frozen order is the one thing a live session must never
    rewrite.
    """
    model.status = session.status.value
    model.completed_at = session.completed_at
    model.correct_count = session.correct_count
    model.xp_awarded = session.xp_awarded


def to_answer_model(answer: QuizAnswer) -> QuizAnswerModel:
    return QuizAnswerModel(
        id=answer.id,
        session_id=answer.session_id,
        question_id=answer.question_id,
        position=answer.position,
        given_answer=dict(answer.given_answer),
        is_correct=answer.is_correct,
        points_awarded=answer.points_awarded,
        attempt=answer.attempt,
        explanation=answer.explanation,
        answered_at=answer.answered_at,
    )


def apply_answer(answer: QuizAnswer, model: QuizAnswerModel) -> None:
    """Copy a re-graded answer onto its row.

    Everything except the identity columns is written. The question a row points at never
    changes — a retry is the same question, answered again — so ``session_id`` and
    ``question_id`` are deliberately left alone, which is what keeps the unique constraint
    meaningful.
    """
    model.given_answer = dict(answer.given_answer)
    model.is_correct = answer.is_correct
    model.points_awarded = answer.points_awarded
    model.attempt = answer.attempt
    model.explanation = answer.explanation
    model.answered_at = answer.answered_at


def to_progress_model(progress: LessonProgress) -> LessonProgressModel:
    return LessonProgressModel(
        id=progress.id,
        user_id=progress.user_id,
        lesson_id=progress.lesson_id,
        state=progress.state.value,
        best_score=progress.best_score,
        attempts=progress.attempts,
        xp_earned=progress.xp_earned,
        completed_at=progress.completed_at,
    )


def apply_progress(progress: LessonProgress, model: LessonProgressModel) -> None:
    model.state = progress.state.value
    model.best_score = progress.best_score
    model.attempts = progress.attempts
    model.xp_earned = progress.xp_earned
    model.completed_at = progress.completed_at


__all__ = [
    "apply_answer",
    "apply_progress",
    "apply_session",
    "to_answer",
    "to_answer_model",
    "to_progress",
    "to_progress_model",
    "to_session",
    "to_session_model",
]
