"""Feature-flag routes (F24).

Two surfaces, deliberately different:

* ``GET /api/v1/flags`` — authenticated and public-flag-only, evaluated for the caller.
  A client is told the answer, not the percentage, so the bucketing rule lives in one
  place and cannot drift between mobile and web.
* ``/api/v1/admin/flags`` — the operator surface. Writes are audited in the same
  transaction as the policy change, so "who turned this on" is never a guess.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.deps import AdminRateLimitDep, CatalogueRateLimitDep, FeatureFlagServiceDep
from app.modules.admin.presentation.flags_schemas import (
    FlagResponse,
    FlagUpdateRequest,
    PublicFlagsResponse,
)
from app.modules.users.domain.entities import User
from app.modules.users.presentation.routes import CurrentUser, require_admin

public_router = APIRouter()
admin_router = APIRouter()

_Key = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{1,63}$", description="Flag key.")]


@public_router.get("", response_model=PublicFlagsResponse, summary="Read the caller's flags")
async def read_public_flags(
    user: CurrentUser,
    service: FeatureFlagServiceDep,
    _limit_guard: CatalogueRateLimitDep,
) -> PublicFlagsResponse:
    """Only flags marked ``public``; an internal kill switch never reaches a client."""
    return PublicFlagsResponse(flags=await service.public_flags(user_id=user.id))


@admin_router.get("", response_model=list[FlagResponse], summary="List every feature flag")
async def list_flags(
    _admin: Annotated[User, Depends(require_admin)],
    service: FeatureFlagServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> list[FlagResponse]:
    flags = await service.snapshot()
    return [
        FlagResponse(
            key=flag.key,
            enabled=flag.enabled,
            rollout_percentage=flag.rollout_percentage,
            public=flag.public,
            description=flag.description,
            updated_at=flag.updated_at,
        )
        for flag in sorted(flags.values(), key=lambda item: item.key)
    ]


@admin_router.put("/{key}", response_model=FlagResponse, summary="Create or update a flag")
async def upsert_flag(
    admin: Annotated[User, Depends(require_admin)],
    service: FeatureFlagServiceDep,
    _limit_guard: AdminRateLimitDep,
    key: _Key,
    payload: FlagUpdateRequest,
) -> FlagResponse:
    flag = await service.upsert(
        key,
        enabled=payload.enabled,
        rollout_percentage=payload.rollout_percentage,
        public=payload.public,
        description=payload.description,
        actor_id=admin.id,
    )
    return FlagResponse(
        key=flag.key,
        enabled=flag.enabled,
        rollout_percentage=flag.rollout_percentage,
        public=flag.public,
        description=flag.description,
        updated_at=flag.updated_at,
    )


__all__ = ["admin_router", "public_router"]
