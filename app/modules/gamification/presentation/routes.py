"""Authenticated routes for the gamification surface.

Five endpoints, all scoped to the caller's own record, all requiring a bearer token: XP,
streaks and achievements are account data, and claiming a reward mutates the caller.

* ``GET /gamification/summary`` — the hub screen.
* ``GET /achievements`` — the whole catalogue, coloured by the learner's record.
* ``POST /achievements/{code}/claim`` — collect an unlocked achievement's reward.
* ``GET /streak`` — the streak detail screen.
* ``POST /streak/freeze`` — spend a freeze to protect the run (premium-gated).

Unlike the AI module, this router declares its full paths and is mounted without a prefix.
The surface is not one segment: the summary sits under ``/gamification`` while achievements
and streaks are top-level nouns in the product's URL vocabulary, and a router that has to
declare one path in each prefix should own that shape here rather than split across two
mounted routers that happen to belong to the same module. This is the same pattern
``users/presentation/routes.py`` already uses.

Handlers stay thin: parse, delegate to :class:`GamificationService`, render. The rule that
decides a league tier, whether a badge unlocks and whether a freeze is available all live in
the service and its tests, not here.

Every endpoint is rate-limited on its own ``gamification`` scope. Two of the five are writes,
so they spend the read budget rather than the ordinary write budget — none of them are on the
session-completion path, which has its own.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path

from app.api.deps import GamificationRateLimitDep, GamificationServiceDep
from app.modules.gamification.application.dto import (
    AchievementView,
    ClaimResultView,
    GamificationSummaryView,
    StreakView,
)
from app.modules.gamification.presentation.schemas import (
    AchievementListResponse,
    AchievementResponse,
    ClaimResultResponse,
    GamificationSummaryResponse,
    StreakResponse,
)
from app.modules.users.presentation.routes import CurrentUser

router = APIRouter()

#: Achievement codes are the snake_case slugs in the code catalogue; validating the shape
#: here means a malformed ``code`` fails as a 422 instead of a 404, which is the honest
#: answer for a value the URL grammar rejects before any lookup happens.
AchievementCode = Annotated[str, Path(pattern=r"^[a-z0-9_]{1,64}$")]


@router.get(
    "/gamification/summary",
    response_model=GamificationSummaryResponse,
    summary="Read the gamification hub summary",
)
async def get_gamification_summary(
    user: CurrentUser,
    service: GamificationServiceDep,
    _limit: GamificationRateLimitDep,
) -> GamificationSummaryResponse:
    """The hub screen: XP, gems, streaks, league tier and the achievement tally, in one read.

    For a learner with no stored stats row the service projects one from durable history, so
    a returning account does not read as a blank sheet.
    """
    view: GamificationSummaryView = await service.summary(user.id)
    return GamificationSummaryResponse.model_validate(view)


@router.get(
    "/achievements",
    response_model=AchievementListResponse,
    summary="List achievements",
)
async def list_achievements(
    user: CurrentUser,
    service: GamificationServiceDep,
    _limit: GamificationRateLimitDep,
) -> AchievementListResponse:
    """The whole catalogue in curriculum-free order, locked entries included.

    Locked badges are the point of a catalogue — they are what the learner is aiming at — so
    they arrive with live ``progress`` rather than being filtered out until earned.
    """
    views: list[AchievementView] = await service.list_achievements(user.id)
    items = [AchievementResponse.model_validate(view) for view in views]
    return AchievementListResponse(items=items, total=len(items))


@router.post(
    "/achievements/{code}/claim",
    response_model=ClaimResultResponse,
    summary="Claim an achievement reward",
)
async def claim_achievement(
    code: AchievementCode,
    user: CurrentUser,
    service: GamificationServiceDep,
    _limit: GamificationRateLimitDep,
) -> ClaimResultResponse:
    """Collect an unlocked achievement's XP and gems.

    Claiming is a deliberate tap rather than a silent grant, and a second claim is refused as
    a conflict so a retrying client can tell a successful first claim from a duplicate. An
    unknown or still-locked code reads as a 404.
    """
    view: ClaimResultView = await service.claim(user.id, code)
    return ClaimResultResponse.model_validate(view)


@router.get(
    "/streak",
    response_model=StreakResponse,
    summary="Read the streak detail",
)
async def get_streak(
    user: CurrentUser,
    service: GamificationServiceDep,
    _limit: GamificationRateLimitDep,
) -> StreakResponse:
    """The streak screen's payload: current and longest run, freezes left, and whether
    freezing is on offer at all so the paywall can be shown before the tap."""
    view: StreakView = await service.streak(user.id)
    return StreakResponse.model_validate(view)


@router.post(
    "/streak/freeze",
    response_model=StreakResponse,
    summary="Spend a streak freeze",
)
async def freeze_streak(
    user: CurrentUser,
    service: GamificationServiceDep,
    _limit: GamificationRateLimitDep,
) -> StreakResponse:
    """Spend one freeze to protect the run.

    Premium-gated for this phase (real subscriptions arrive later): a non-premium deployment
    answers ``PREMIUM_REQUIRED`` with a purchase link. Running out is a conflict, not a
    crash, so a double-tapped button degrades to an honest error.
    """
    view: StreakView = await service.freeze(user.id)
    return StreakResponse.model_validate(view)


__all__ = ["router"]
