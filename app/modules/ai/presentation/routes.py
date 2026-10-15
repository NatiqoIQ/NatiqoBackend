"""Authenticated routes for the AI tutor surface.

Four endpoints under ``/ai``, all scoped to the caller and all requiring a bearer token: a
tutor thread is account data, and grading an answer is a per-learner act.

* ``POST /ai/explain`` — grade one answer and return the verdict plus a rationale. The key
  travels only after the learner has committed an answer, exactly as ``/quiz`` and ``/review``
  keep it.
* ``POST /ai/tutor`` — one chat turn; opens or continues a thread.
* ``GET /ai/tutor/{id}`` — read a thread back.
* ``GET /ai/quota`` — how much of today's AI allowance is left.

Handlers stay thin: parse, delegate to :class:`AiTutorService`, render. The rule that decides
an answer's correctness, the conversation ownership check and the budget read all live in the
service and its tests, not here, so none of them can be made reachable only through HTTP.

Every endpoint is rate-limited on its own ``ai`` scope. The tutor is the one surface where a
single request can trigger an upstream, billable call, so it gets a budget separate from the
ordinary read/write scopes rather than sharing one.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import AiRateLimitDep, AiTutorServiceDep
from app.modules.ai.application.dto import (
    AiQuotaView,
    ConversationView,
    ExplanationView,
    TutorReplyView,
)
from app.modules.ai.presentation.schemas import (
    AiQuotaResponse,
    ConversationResponse,
    ExplainRequest,
    ExplanationResponse,
    TutorReplyResponse,
    TutorRequest,
)
from app.modules.quiz.domain.policy import SubmittedAnswer
from app.modules.users.presentation.routes import CurrentUser

router = APIRouter()


@router.post(
    "/explain",
    response_model=ExplanationResponse,
    summary="Explain an answer",
)
async def explain_answer(
    payload: ExplainRequest,
    user: CurrentUser,
    service: AiTutorServiceDep,
    _limit: AiRateLimitDep,
) -> ExplanationResponse:
    """Grade one answer and return the verdict with a rationale.

    The verdict is computed on the server from the answer the learner posted; the key is only
    ever present in the response to this call. When AI is disabled or over budget the
    explanation falls back to the question's authored rationale, and ``source`` says so.
    """
    view: ExplanationView = await service.explain(
        user.id,
        question_id=payload.question_id,
        answer=SubmittedAnswer(choice=payload.answer.choice, text=payload.answer.text),
    )
    return ExplanationResponse.model_validate(view)


@router.post(
    "/tutor",
    response_model=TutorReplyResponse,
    summary="Ask the AI tutor",
)
async def ask_tutor(
    payload: TutorRequest,
    user: CurrentUser,
    service: AiTutorServiceDep,
    _limit: AiRateLimitDep,
) -> TutorReplyResponse:
    """Send a message to the tutor, opening or continuing a thread.

    Omitting ``conversationId`` opens a new thread; passing one continues it, and it must
    belong to the caller or it reads as absent. The learner's turn and the reply are stored
    together, so a thread never contains a question without its answer.
    """
    view: TutorReplyView = await service.ask(
        user.id,
        message=payload.message,
        conversation_id=payload.conversation_id,
        lesson_id=payload.lesson_id,
    )
    return TutorReplyResponse.model_validate(view)


@router.get(
    "/tutor/{conversation_id}",
    response_model=ConversationResponse,
    summary="Read a tutor conversation",
)
async def read_conversation(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    service: AiTutorServiceDep,
    _limit: AiRateLimitDep,
) -> ConversationResponse:
    """A thread's turns, oldest first. Scoped to the caller's own threads."""
    view: ConversationView = await service.history(user.id, conversation_id)
    return ConversationResponse.model_validate(view)


@router.get(
    "/quota",
    response_model=AiQuotaResponse,
    summary="Read the learner's AI quota",
)
async def read_quota(
    user: CurrentUser,
    service: AiTutorServiceDep,
    _limit: AiRateLimitDep,
) -> AiQuotaResponse:
    """How much of today's AI allowance is left, so a UI can warn before it runs out."""
    view: AiQuotaView = await service.quota(user.id)
    return AiQuotaResponse.model_validate(view)


__all__ = ["router"]
