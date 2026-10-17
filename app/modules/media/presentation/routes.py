"""Authenticated routes for the media surface.

Three endpoints, all requiring a bearer token, and one flow:

* ``POST /media/presign`` — issue a write grant for a new key.
* ``POST /media/confirm`` — verify the upload landed and record its verified metadata.
* ``GET /media/{media_id}`` — describe an asset and re-sign a read URL for it.

The binary payload never passes through these handlers. A client asks for a grant, PUTs
straight to the object store, and calls back — which is why uploads can be larger than the
request-body ceiling without that ceiling being raised: the two never meet.

Reads are open to any authenticated caller rather than restricted to the uploader. The id
is a non-enumerable UUID and the URL is short-lived and signed, and the assets this phase
stores — avatars and curriculum aids — are meant to be visible to other learners, so an
ownership check here would refuse the very reads the product wants while protecting
nothing. All three are rate-limited on their own ``media`` scope.

Handlers stay thin: parse, delegate to :class:`MediaService`, render. Every rule that
decides what a file may be — its kind's allow-list, its real type, its size ceiling, whether
the key is even one we issued — lives in the service and its tests, not here.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status

from app.api.deps import MediaRateLimitDep, MediaServiceDep
from app.modules.media.application.dto import MediaView, PresignCommand, PresignView
from app.modules.media.presentation.schemas import (
    MediaConfirmRequest,
    MediaPresignRequest,
    MediaPresignResponse,
    MediaResponse,
)
from app.modules.users.presentation.routes import CurrentUser

router = APIRouter()

#: Typed as a UUID so a non-UUID path segment fails as a 422 at the edge, before any lookup.
MediaId = Annotated[uuid.UUID, Path(description="The media asset's id.")]


@router.post(
    "/presign",
    response_model=MediaPresignResponse,
    summary="Grant a direct upload to storage",
)
async def presign_media(
    payload: MediaPresignRequest,
    user: CurrentUser,
    service: MediaServiceDep,
    _limit: MediaRateLimitDep,
) -> MediaPresignResponse:
    """Mint a key and a write grant for it.

    Nothing is persisted here — the response is a grant, not an asset — so a client that
    never uploads leaves no trace but the key it was handed. The declared content type is
    checked against the kind's allow-list so an obviously wrong request fails before any
    bytes move; the real decision is made at confirm.
    """
    view: PresignView = await service.presign(
        user.id,
        PresignCommand(kind=payload.kind, content_type=payload.content_type),
    )
    return MediaPresignResponse.model_validate(view)


@router.post(
    "/confirm",
    response_model=MediaResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Confirm an upload and record its metadata",
)
async def confirm_media(
    payload: MediaConfirmRequest,
    user: CurrentUser,
    service: MediaServiceDep,
    _limit: MediaRateLimitDep,
) -> MediaResponse:
    """Verify the object exists, then describe it from what the bytes proved.

    The server re-reads the object rather than trusting anything the client says about it:
    the key must be one this service issued, the size must be within the kind's ceiling, and
    the real type — named from the leading bytes, not the request header — must be on the
    kind's allow-list. A rejected upload is deleted as it is refused. Confirmation is
    idempotent, so a retry after a dropped response returns the row the first attempt
    created rather than a duplicate.
    """
    view: MediaView = await service.confirm(user.id, payload.media_key)
    return MediaResponse.model_validate(view)


@router.get(
    "/{media_id}",
    response_model=MediaResponse,
    summary="Read a stored asset",
)
async def get_media(
    media_id: MediaId,
    user: CurrentUser,
    service: MediaServiceDep,
    _limit: MediaRateLimitDep,
) -> MediaResponse:
    """Describe an existing asset and re-sign its read URL.

    Any authenticated caller may read any asset: the id is non-enumerable and the URL is
    short-lived and signed, and these assets are meant to be shared. An unknown id is a 404
    rather than a leak — the same answer a caller gets for an asset they may not see, so the
    endpoint never confirms whether an id exists to someone probing ids.
    """
    view: MediaView = await service.get(media_id)
    return MediaResponse.model_validate(view)


__all__ = ["router"]
