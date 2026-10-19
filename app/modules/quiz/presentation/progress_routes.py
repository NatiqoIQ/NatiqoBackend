"""Authenticated routes for the progress surface.

Two reads under ``/progress``, both scoped to the caller's own record: an overview (the
summary plus the first page of the XP feed) and the curriculum-ordered unlock map. Unlike
the catalogue these carry account data, so every endpoint takes ``CurrentUser`` — and being
authenticated and read-only, they are also cached by the client and rate-limited on their
own scope rather than spending the write budget.

The overview merges the summary and the XP feed because a progress screen reads both at
once: one round trip instead of two, with the summary flattened at the top level and the
feed under ``xp``. A single lesson's record is not a route any more — it travels on each
unlock-map node, so the map is one request rather than a map plus a per-lesson probe.

Handlers stay thin: parse, delegate to :class:`ProgressService`, render. The rule that
decides a streak or a state lives in the service and its tests, not here, so a progress
change cannot be made reachable only through HTTP.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import ProgressRateLimitDep, ProgressServiceDep
from app.core.responses import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, build_page
from app.modules.quiz.application.progress_dto import (
    ProgressSummaryView,
    UnitProgressItemView,
    UnlockMapItemView,
)
from app.modules.quiz.presentation.progress_schemas import (
    ProgressOverviewResponse,
    ProgressSummaryResponse,
    UnitProgressItemResponse,
    UnitsResponse,
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
    "",
    response_model=ProgressOverviewResponse,
    summary="Read the learner's progress overview",
)
async def get_progress(
    user: CurrentUser,
    service: ProgressServiceDep,
    _limit: ProgressRateLimitDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> ProgressOverviewResponse:
    """XP, lesson counts, accuracy, streak — and the XP feed — in one read.

    The summary fields are flattened at the top level; the completed-session feed is the
    ``xp`` page beside them, ordered newest first and paged by the same opaque cursor the
    feed always used. Omitting hearts and reviews until they have a store behind them is a
    deliberate part of this contract: the client gets zeroes nowhere and invents nothing.
    """
    summary: ProgressSummaryView = await service.summary(user.id)
    feed = await service.xp_history(user.id, limit=limit, cursor=cursor)
    return ProgressOverviewResponse(
        **ProgressSummaryResponse.model_validate(summary).model_dump(),
        xp=build_page(
            items=[XpEventResponse.model_validate(event) for event in feed.items],
            limit=limit,
            has_more=feed.has_more,
            next_cursor=feed.next_cursor,
        ),
    )


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
    client draws incrementally would need the whole set anyway. Each node carries the
    learner's ``bestScore``, ``attempts`` and ``xpEarned``, and ``locked`` is the
    sequential-unlock rule made visible: a lesson is shut while its unit is, and a unit
    opens once its predecessor's checkpoint has been passed. The unit-level view of the
    same rule — with the checkpoint record itself — is ``GET /progress/units``.
    """
    items: list[UnlockMapItemView] = await service.unlock_map(user.id)
    return UnlockMapResponse(
        items=[UnlockMapItemResponse.model_validate(item) for item in items],
        total=len(items),
    )


@router.get(
    "/units",
    response_model=UnitsResponse,
    summary="Read the unit checkpoints' standing",
)
async def get_units(
    user: CurrentUser,
    service: ProgressServiceDep,
    _limit: ProgressRateLimitDep,
) -> UnitsResponse:
    """Every published unit in curriculum order, with its checkpoint standing.

    Whole-list like the map, and for the same reason: the published catalogue is the
    bound. Each item combines the sequential rule (``locked``) with the checkpoint
    record (``passed``, ``bestScore``, ``attempts``, ``passedAt``) and the lesson counts,
    so the client can offer the next action — finish a lesson, sit the checkpoint, or
    pass the previous unit first — from this one read.
    """
    items: list[UnitProgressItemView] = await service.units(user.id)
    return UnitsResponse(
        items=[UnitProgressItemResponse.model_validate(item) for item in items],
        total=len(items),
    )


__all__ = ["router"]
