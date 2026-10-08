"""Public catalogue routes.

Five GET endpoints, mounted under ``/api/v1/content`` and **deliberately unauthenticated**.
Browsing a published curriculum is not account data: an app shell has to be able to show
"there is a beginner course" before anyone has signed in, and gating it would mean an
authenticated empty screen instead.

That choice is only safe because of what these routes cannot return. The lists serve
``LevelResponse`` / ``UnitResponse`` / ``LessonSummaryResponse``, and the detail endpoint
serves ``LessonDetailResponse``, whose ``QuestionResponse`` carries no answer key. So the
questions are readable *before* an attempt — the point of a practice catalogue — and
unreadable *after* one without going through the attempt engine.

The fifth endpoint is the exception that proves the rule: ``GET .../questions/{id}/explanation``
**does** return an explanation. That is intentional — every question ships with one, and
a learner needs it after answering — so it lives on its own route, fetched by the client
only once the attempt is over, instead of becoming a field on a payload that travels
before the attempt. There is no attempt engine yet, so this server cannot *enforce* that
ordering; it keeps the two surfaces apart and lets the client sequence them. Serving it
in the lesson payload instead would hand every caller the answer key to a public test.

Unpublished content does not exist here. The repositories filter on ``published``, so a
draft level, a draft unit, a question under a draft lesson and an id that was never
issued all produce the same 404.

All five are ``@cacheable`` and rate-limited, which are the two halves of the same
decision. They carry no user data and change only when an editor publishes, so a phone
may reuse them; they are also unauthenticated, so nothing but a budget stops one client
from walking the whole catalogue in a loop. ``stale_while_revalidate`` is set generously
because a learner looking at yesterday's lesson list is better served than a spinner.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import CatalogueRateLimitDep, CatalogueServiceDep
from app.core.http_cache import cacheable
from app.core.responses import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, build_page
from app.modules.content.presentation.schemas import (
    LessonDetailResponse,
    LessonSummaryResponse,
    LevelResponse,
    QuestionExplanationResponse,
    UnitResponse,
)

router = APIRouter()

#: The page size is the client's request, but it is still bounded here: an unbounded
#: ``limit`` is a cheap denial-of-service lever on a public, unauthenticated endpoint.
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
Cursor = Annotated[str | None, Query(description="Opaque. Pass back meta.nextCursor verbatim.")]


@router.get("/levels", response_model=Page[LevelResponse], summary="Browse CEFR levels")
@cacheable(max_age=60, stale_while_revalidate=600)
async def list_levels(
    service: CatalogueServiceDep,
    _limit_guard: CatalogueRateLimitDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> Page[LevelResponse]:
    """Published levels in curriculum order, each with its published unit count."""
    result = await service.list_levels(limit=limit, cursor=cursor)
    return build_page(
        items=[LevelResponse.model_validate(level) for level in result.items],
        limit=limit,
        has_more=result.has_more,
        next_cursor=result.next_cursor,
    )


@cacheable(max_age=60, stale_while_revalidate=600)
@router.get(
    "/levels/{level_code}/units",
    response_model=Page[UnitResponse],
    summary="Browse a level's units",
)
async def list_units(
    level_code: Annotated[
        str, Path(description="CEFR code, e.g. 'A1'. An unknown code is a 404, not a 422.")
    ],
    service: CatalogueServiceDep,
    _limit_guard: CatalogueRateLimitDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> Page[UnitResponse]:
    """Published units of one level. 404 for an unknown *or* an unpublished level."""
    result = await service.list_units(level_code, limit=limit, cursor=cursor)
    return build_page(
        items=[UnitResponse.model_validate(unit) for unit in result.items],
        limit=limit,
        has_more=result.has_more,
        next_cursor=result.next_cursor,
    )


@cacheable(max_age=60, stale_while_revalidate=600)
@router.get(
    "/units/{unit_id}/lessons",
    response_model=Page[LessonSummaryResponse],
    summary="Browse a unit's lessons",
)
async def list_lessons(
    unit_id: uuid.UUID,
    service: CatalogueServiceDep,
    _limit_guard: CatalogueRateLimitDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> Page[LessonSummaryResponse]:
    """Published lessons of one unit, each with its question count."""
    result = await service.list_lessons(unit_id, limit=limit, cursor=cursor)
    return build_page(
        items=[LessonSummaryResponse.model_validate(lesson) for lesson in result.items],
        limit=limit,
        has_more=result.has_more,
        next_cursor=result.next_cursor,
    )


@cacheable(max_age=60, stale_while_revalidate=600)
@router.get(
    "/lessons/{lesson_id}",
    response_model=LessonDetailResponse,
    summary="Open a lesson",
)
async def get_lesson(
    lesson_id: uuid.UUID,
    service: CatalogueServiceDep,
    _limit_guard: CatalogueRateLimitDep,
) -> LessonDetailResponse:
    """A lesson and its questions, ready to attempt — and nothing else about them."""
    detail = await service.get_lesson(lesson_id)
    return LessonDetailResponse.model_validate(detail)


@cacheable(max_age=60, stale_while_revalidate=600)
@router.get(
    "/questions/{question_id}/explanation",
    response_model=QuestionExplanationResponse,
    summary="Read a question's explanation after answering",
)
async def get_question_explanation(
    question_id: uuid.UUID,
    service: CatalogueServiceDep,
    _limit_guard: CatalogueRateLimitDep,
) -> QuestionExplanationResponse:
    """The rationale behind a question's answer.

    Every question carries one; this is the only route that reveals it, and it is kept
    off the lesson payload so the public catalogue never hands out the answer key. The
    client is expected to call this after the learner has answered — the attempt engine
    that would make that ordering enforceable does not exist yet.
    """
    view = await service.get_question_explanation(question_id)
    return QuestionExplanationResponse.model_validate(view)


__all__ = ["router"]
