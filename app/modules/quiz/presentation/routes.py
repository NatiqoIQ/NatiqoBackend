"""Authenticated routes for the attempt engine.

Five endpoints under ``/quiz``: open a sitting, resume one, answer a question, finish, and
abandon. All require a bearer token — unlike the catalogue, a session is account data, and
every one of them is scoped to the caller's own sessions.

Handlers are thin: parse, delegate to :class:`QuizService`, render. The rule that decides
an answer's correctness never appears here, and the answer key never leaves the response
models in ``schemas.py``.

``CurrentUser`` is imported from the users module because authentication is cross-cutting
and there is no shared auth module; it resolves the caller from the access token and its
roles from the live account. This mirrors the one existing cross-module read in the
codebase (``ai`` reaching into ``users`` for a profile timezone).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.deps import QuizRateLimitDep, QuizServiceDep
from app.modules.quiz.application.dto import (
    CompletedSessionView,
    GradedAnswerView,
    SessionDetailView,
    SessionView,
)
from app.modules.quiz.domain.policy import SubmittedAnswer
from app.modules.quiz.presentation.schemas import (
    CompletedSessionResponse,
    GradedAnswerResponse,
    SessionDetailResponse,
    SessionQuestionResponse,
    SessionResponse,
    StartSessionRequest,
    SubmitAnswerRequest,
)
from app.modules.users.presentation.routes import CurrentUser

router = APIRouter()


def _session_response(view: SessionView) -> SessionResponse:
    return SessionResponse(
        id=view.id,
        mode=view.mode,
        lesson_id=view.lesson_id,
        status=view.status,
        started_at=view.started_at,
        expires_at=view.expires_at,
        completed_at=view.completed_at,
        total_count=view.total_count,
        answered_count=view.answered_count,
        correct_count=view.correct_count,
        xp_awarded=view.xp_awarded,
        answered_question_ids=list(view.answered_question_ids),
    )


def _detail_response(view: SessionDetailView) -> SessionDetailResponse:
    return SessionDetailResponse(
        session=_session_response(view.session),
        questions=[
            SessionQuestionResponse(
                id=question.id,
                position=question.position,
                kind=question.kind,
                skill=question.skill,
                prompt=question.prompt,
                choices=list(question.choices),
                points=question.points,
                image_url=question.image_url,
                audio_url=question.audio_url,
            )
            for question in view.questions
        ],
    )


@router.post(
    "/sessions",
    response_model=SessionDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a quiz session over a lesson",
)
async def start_session(
    payload: StartSessionRequest,
    user: CurrentUser,
    service: QuizServiceDep,
    _limit: QuizRateLimitDep,
) -> SessionDetailResponse:
    """Open a sitting and return its questions — without any answer key.

    Safe to retry: send an ``Idempotency-Key`` and a repeat of the same request replays the
    original ``sessionId`` instead of opening a second sitting.
    """
    detail = await service.start_session(user.id, mode=payload.mode, lesson_id=payload.lesson_id)
    return _detail_response(detail)


@router.get(
    "/sessions/{session_id}",
    response_model=SessionDetailResponse,
    summary="Resume a quiz session",
)
async def resume_session(
    session_id: uuid.UUID,
    user: CurrentUser,
    service: QuizServiceDep,
) -> SessionDetailResponse:
    """Re-open a sitting after the app was killed, or read a finished one."""
    return _detail_response(await service.resume_session(user.id, session_id))


@router.post(
    "/sessions/{session_id}/answers",
    response_model=GradedAnswerResponse,
    summary="Answer one question",
)
async def submit_answer(
    session_id: uuid.UUID,
    payload: SubmitAnswerRequest,
    user: CurrentUser,
    service: QuizServiceDep,
    _limit: QuizRateLimitDep,
) -> GradedAnswerResponse:
    """Grade one answer and return the verdict, the correct answer and the rationale."""
    graded: GradedAnswerView = await service.submit_answer(
        user.id,
        session_id,
        question_id=payload.question_id,
        answer=SubmittedAnswer(choice=payload.answer.choice, text=payload.answer.text),
    )
    return GradedAnswerResponse(
        is_correct=graded.is_correct,
        correct_answer=graded.correct_answer,
        explanation=graded.explanation,
        points_awarded=graded.points_awarded,
        answered_count=graded.answered_count,
        total_count=graded.total_count,
        correct_count=graded.correct_count,
        is_complete=graded.is_complete,
    )


@router.post(
    "/sessions/{session_id}/complete",
    response_model=CompletedSessionResponse,
    summary="Finish a session and score it",
)
async def complete_session(
    session_id: uuid.UUID,
    user: CurrentUser,
    service: QuizServiceDep,
) -> CompletedSessionResponse:
    """Score a fully answered sitting and record it against the lesson."""
    completed: CompletedSessionView = await service.complete_session(user.id, session_id)
    return CompletedSessionResponse(
        session=_session_response(completed.session),
        score_percent=completed.score_percent,
    )


@router.post(
    "/sessions/{session_id}/abandon",
    response_model=SessionResponse,
    summary="Abandon a session",
)
async def abandon_session(
    session_id: uuid.UUID,
    user: CurrentUser,
    service: QuizServiceDep,
) -> SessionResponse:
    """End a sitting without scoring it. Idempotent once abandoned."""
    return _session_response(await service.abandon_session(user.id, session_id))


__all__ = ["router"]
