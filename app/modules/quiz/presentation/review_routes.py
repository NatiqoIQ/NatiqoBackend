"""Authenticated routes for spaced repetition.

Four endpoints under ``/review``, all scoped to the caller's own cards: the due queue, the
stats, a cheap due-count badge, and the answer command. Like the attempt engine they carry
account data, so every one takes ``CurrentUser``.

The queue serves the same answer-free question projection a live session does and the answer
command returns the verdict and the rationale — the key travels only after the learner
answers, exactly as it does in ``/quiz``. Answering is a write, so it is rate-limited on the
review scope while the reads are cheap enough to share it.

Handlers stay thin: parse, delegate to :class:`ReviewService`, render. The SM-2 rule lives in
the service and its tests, not here, so a scheduling change cannot be made reachable only
through HTTP.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ReviewRateLimitDep, ReviewServiceDep
from app.core.responses import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.modules.quiz.application.dto import QuestionView
from app.modules.quiz.application.review_dto import (
    DueCardsView,
    DueCardView,
    ReviewedCardView,
    ReviewPreviewView,
    ReviewStatsView,
)
from app.modules.quiz.domain.policy import SubmittedAnswer
from app.modules.quiz.presentation.review_schemas import (
    DueCardResponse,
    DueCardsResponse,
    ReviewedCardResponse,
    ReviewPreviewResponse,
    ReviewStatsResponse,
)
from app.modules.quiz.presentation.schemas import AnswerPayload, SessionQuestionResponse
from app.modules.users.presentation.routes import CurrentUser

router = APIRouter()

#: The page size is the client's request, but still bounded: an unbounded ``limit`` is a
#: cheap denial-of-service lever on a route that does real work per row.
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]


def _question_response(view: QuestionView) -> SessionQuestionResponse:
    """Project an answer-free question view onto the wire — with no answer key."""
    return SessionQuestionResponse(
        id=view.id,
        position=view.position,
        kind=view.kind,
        skill=view.skill,
        prompt=view.prompt,
        choices=list(view.choices),
        points=view.points,
        image_url=view.image_url,
        audio_url=view.audio_url,
        video_url=view.video_url,
        prompts=list(view.prompts),
    )


def _due_card_response(view: DueCardView) -> DueCardResponse:
    return DueCardResponse(
        card_id=view.card_id,
        due_at=view.due_at,
        state=view.state,
        interval_days=view.interval_days,
        repetitions=view.repetitions,
        question=_question_response(view.question),
    )


@router.get(
    "/due",
    response_model=DueCardsResponse,
    summary="Read the learner's due review queue",
)
async def get_due_cards(
    user: CurrentUser,
    service: ReviewServiceDep,
    _limit: ReviewRateLimitDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    strict: bool = False,
) -> DueCardsResponse:
    """Cards that are due now, soonest first, each with its (answer-free) question.

    ``dueCount`` is the total owed, not the size of this page. Pass ``strict=true`` to get a
    ``NOT_FOUND`` (``REVIEW_NOTHING_DUE``) instead of an empty page when there is nothing to
    review — for a caller that would rather be told than shown a blank screen.
    """
    view: DueCardsView = await service.due_cards(user.id, limit=limit, strict=strict)
    return DueCardsResponse(
        cards=[_due_card_response(card) for card in view.cards],
        has_more=view.has_more,
        due_count=view.due_count,
    )


@router.get(
    "/stats",
    response_model=ReviewStatsResponse,
    summary="Read the learner's review statistics",
)
async def get_review_stats(
    user: CurrentUser,
    service: ReviewServiceDep,
    _limit: ReviewRateLimitDep,
) -> ReviewStatsResponse:
    """Total cards, how many are due now, how many are ahead, and lifetime retention."""
    view: ReviewStatsView = await service.stats(user.id)
    return ReviewStatsResponse.model_validate(view)


@router.get(
    "/preview",
    response_model=ReviewPreviewResponse,
    summary="Read how many cards are due",
)
async def get_review_preview(
    user: CurrentUser,
    service: ReviewServiceDep,
    _limit: ReviewRateLimitDep,
) -> ReviewPreviewResponse:
    """Just the due count, for badging a "review" entry point without loading the queue."""
    view: ReviewPreviewView = await service.preview(user.id)
    return ReviewPreviewResponse.model_validate(view)


@router.post(
    "/cards/{card_id}/answer",
    response_model=ReviewedCardResponse,
    summary="Answer a review card and advance its schedule",
)
async def answer_review_card(
    card_id: uuid.UUID,
    payload: AnswerPayload,
    user: CurrentUser,
    service: ReviewServiceDep,
    _limit: ReviewRateLimitDep,
) -> ReviewedCardResponse:
    """Grade one review and return the verdict, the correct answer and the new schedule.

    The card is looked up under the caller, so another account's card reads as absent. The
    verdict and the rationale are only ever reachable through the answer command, which is
    what keeps the answer key off every other surface.
    """
    view: ReviewedCardView = await service.answer_card(
        user.id,
        card_id,
        answer=SubmittedAnswer(
            choice=payload.choice,
            text=payload.text,
            choices=tuple(payload.choices or ()),
            order=tuple(payload.order or ()),
            matching=tuple(payload.matching or ()),
        ),
    )
    return ReviewedCardResponse.model_validate(view)


__all__ = ["router"]
