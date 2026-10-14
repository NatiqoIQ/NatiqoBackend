"""Public catalogue routes.

Two GET endpoints, mounted under ``/api/v1/content`` and **deliberately unauthenticated**.
Browsing a published curriculum is not account data: an app shell has to be able to show
"there is a beginner course" before anyone has signed in, and gating it would mean an
authenticated empty screen instead.

That choice is only safe because of what these routes cannot return. The tree serves
``CatalogueTreeResponse`` — levels → units → ``LessonSummaryResponse`` — and the detail
endpoint serves ``LessonDetailResponse``, whose ``QuestionResponse`` carries no answer key.
So the questions are readable *before* an attempt — the point of a practice catalogue —
and unreadable *after* one without going through the attempt engine.

The rationale is deliberately *not* here. Every question ships with an explanation, but
the surface that reveals one is the attempt engine: an answer command on
``POST /quiz/sessions/{id}`` returns it, and only to a caller who has actually answered. A
public route would have had to trust the client to ask only after the attempt, with no
engine to enforce it; now that one exists, serving an explanation from the unauthenticated
catalogue would simply hand the answer key to anyone who walks question ids. So the two
surfaces stay apart, and the one route that reveals an answer is an authenticated one.

The tree replaced three browsable list endpoints (levels, a level's units, a unit's
lessons). Those were a client-side walk with its own paging contract at each step; the
whole tree is small, changes only when an editor publishes, and fits in a single cached
response an app shell can hold. Two routes to read the catalogue is the point.

Unpublished content does not exist here. The repositories filter on ``published``, so a
draft level, a draft unit, a question under a draft lesson and an id that was never
issued all produce the same 404.

Both are ``@cacheable`` and rate-limited, which are the two halves of the same decision.
They carry no user data and change only when an editor publishes, so a phone may reuse
them; they are also unauthenticated, so nothing but a budget stops one client from walking
the whole catalogue in a loop. ``stale_while_revalidate`` is set generously because a
learner looking at yesterday's lesson list is better served than a spinner.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import CatalogueRateLimitDep, CatalogueServiceDep
from app.core.http_cache import cacheable
from app.modules.content.presentation.schemas import (
    CatalogueTreeResponse,
    LessonDetailResponse,
    LevelNodeResponse,
)

router = APIRouter()


@cacheable(max_age=60, stale_while_revalidate=600)
@router.get("/tree", response_model=CatalogueTreeResponse, summary="Browse the catalogue")
async def get_tree(
    service: CatalogueServiceDep,
    _limit_guard: CatalogueRateLimitDep,
) -> CatalogueTreeResponse:
    """The whole published catalogue: every level with its units and their lessons.

    Whole-tree rather than paginated by design — the curriculum is small and the response
    is cached, so a single request is friendlier than a walk, and the client does not have
    to stitch pages together to show a course map.
    """
    levels = await service.tree()
    return CatalogueTreeResponse(
        items=[LevelNodeResponse.model_validate(level) for level in levels]
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


__all__ = ["router"]
