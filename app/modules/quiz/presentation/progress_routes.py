"""Authenticated routes for the progress surface.

Four reads under ``/progress``, all scoped to the caller's own record: a summary, one
lesson's record, the curriculum-ordered unlock map, and a paginated XP history. Unlike the
catalogue these carry account data, so every endpoint takes ``CurrentUser`` — and being
authenticated and read-only, they are also cached by the client and rate-limited on their
own scope rather than spending the write budget.

Handlers stay thin: parse, delegate to :class:`ProgressService`, render. The rule that
decides a streak or a state lives in the service and its tests, not here, so a progress
change cannot be made reachable only through HTTP.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import ProgressRateLimitDep, ProgressServiceDep
from app.core.responses import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, build_page
from app.modules.quiz.application.progress_dto import (
    LessonProgressView,
    ProgressSummaryView,
    UnlockMapItemView,
)
from app.modules.quiz.presentation.progress_schemas import (
    LessonProgressResponse,
    ProgressSummaryResponse,
    UnlockMapItemResponse,
    UnlockMapResponse,
    XpEventResponse,
)
from app.modules.users.presentation.routes import CurrentUser

router = APIRouter()

#: The page size is the client's request, but still bounded: an unbounded ``limit`` is a
#: cheap denial-of-service lever on a route that does real work per row.
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
Cursor = Annotated[str | None, Query(description="Opaque. Pass back meta.nextCursor verbatim.")]


@router.get(
    "/summary",
    response_model=ProgressSummaryResponse,
    summary="Read the learner's progress summary",
)
async def get_summary(
    user: CurrentUser,
    service: ProgressServiceDep,
    _limit: ProgressRateLimitDep,
) -> ProgressSummaryResponse:
    """XP, lesson counts, accuracy and streak — only what the record can prove.

    Omitting hearts and reviews until they have a store behind them is a deliberate part
    of this contract: the client gets zeroes nowhere and invents nothing.
    """
    view: ProgressSummaryView = await service.summary(user.id)
    return ProgressSummaryResponse.model_validate(view)


@router.get(
    "/lessons/{lesson_id}",
    response_model=LessonProgressResponse,
    summary="Read progress on one lesson",
)
async def get_lesson_progress(
    lesson_id: Annotated[uuid.UUID, Path(description="The lesson's id.")],
    user: CurrentUser,
    service: ProgressServiceDep,
    _limit: ProgressRateLimitDep,
) -> LessonProgressResponse:
    """One lesson's placement and the learner's record against it.

    An unpublished lesson (or one whose unit or level is in draft) is a 404 — the whole
    chain has to be published before it is reachable.
    """
    view: LessonProgressView = await service.lesson_progress(user.id, lesson_id)
    return LessonProgressResponse.model_validate(view)


@router.get(
    "/unlocks",
    response_model=UnlockMapResponse,
    summary="Read the curriculum-ordered progress map",
)
async def get_unlocks(
    user: CurrentUser,
    service: ProgressServiceDep,
    _limit: ProgressRateLimitDep,
) -> UnlockMapResponse:
    """Every published lesson in curriculum order, each coloured by the learner's record.

    Whole-map rather than paginated: the published catalogue is the bound, and a map a
    client draws incrementally would need the whole set anyway. Nothing is locked until a
    gating rule exists, but ``locked`` is present from the first version so turning one on
    is a data change, not a wire change.
    """
    items: list[UnlockMapItemView] = await service.unlock_map(user.id)
    return UnlockMapResponse(
        items=[UnlockMapItemResponse.model_validate(item) for item in items],
        total=len(items),
    )


@router.get(
    "/xp",
    response_model=Page[XpEventResponse],
    summary="Read the XP history",
)
async def get_xp_history(
    user: CurrentUser,
    service: ProgressServiceDep,
    _limit: ProgressRateLimitDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> Page[XpEventResponse]:
    """Completed sessions, newest first, as a paginated XP feed.

    Only completed sessions earn a row, so an abandoned sitting never shows up as history.
    The cursor is opaque and ordered by finish time.
    """
    result = await service.xp_history(user.id, limit=limit, cursor=cursor)
    return build_page(
        items=[XpEventResponse.model_validate(event) for event in result.items],
        limit=limit,
        has_more=result.has_more,
        next_cursor=result.next_cursor,
    )


__all__ = ["router"]
