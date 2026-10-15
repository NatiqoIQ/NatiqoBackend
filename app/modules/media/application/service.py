"""The media use cases: presign, confirm, read.

Three methods, one shape each, and one rule that runs through all of them — **the server
believes the bytes, never the request**. Presign checks the declared type only so an
obviously wrong request fails before any bytes move; the binding decision is made on
confirm, from the object's own leading bytes and its measured size.

The upload never passes through this process. A client asks for a grant, PUTs straight to
the object store, and calls back. That is why the request-body ceiling stays at 1 MB while
pronunciation clips are allowed to be far larger: the two never meet.

Confirmation is idempotent. A client that retries after a dropped response gets the row the
first attempt created, rather than a duplicate or a unique-constraint failure — a retry of
an operation the server already completed is not an error.

A rejected object is deleted from storage as it is refused. The alternative — leaving a
disallowed upload in the bucket forever because the row was never created — is a slow
storage leak with no owner and no cleanup path, so the refusal is also the cleanup.
"""

from __future__ import annotations

import hashlib
import uuid

from app.core.config import Settings
from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.time import utc_now
from app.core.types import UserId, new_id
from app.core.uow import UnitOfWorkProtocol
from app.modules.media.application.dto import MediaView, PresignCommand, PresignView
from app.modules.media.domain.entities import MediaAsset
from app.modules.media.domain.policy import (
    ALLOWED_CONTENT_TYPES,
    build_storage_key,
    max_bytes_allowed,
    parse_storage_key,
    sniff_content_type,
)
from app.platform.storage.port import ObjectStoragePort

#: Enough leading bytes for every magic number the sniffer recognises; the RIFF
#: discriminator sits at offset eight, so a shorter prefix would misread WebP as WAV.
_HEADER_BYTES = 16


class MediaService:
    def __init__(
        self,
        uow: UnitOfWorkProtocol,
        storage: ObjectStoragePort,
        settings: Settings,
    ) -> None:
        self._uow = uow
        self._storage = storage
        self._settings = settings

    async def presign(self, user_id: UserId, command: PresignCommand) -> PresignView:
        """Issue a write grant for a new key.

        The declared content type is validated against the kind's allow-list here, because
        a client that is going to send the wrong thing should learn so before uploading it
        — not after. This is a courtesy check, not the security boundary; that is on
        confirm.
        """
        allowed = ALLOWED_CONTENT_TYPES[command.kind]
        if command.content_type not in allowed:
            raise ValidationFailed(
                f"A {command.kind.value} cannot be a {command.content_type}.",
                code=ErrorCode.MEDIA_TYPE_NOT_ALLOWED,
                details={
                    "field": "contentType",
                    "kind": command.kind.value,
                    "allowed": sorted(allowed),
                },
            )

        key = build_storage_key(command.kind, command.content_type)
        grant = await self._storage.presigned_upload(
            key,
            content_type=command.content_type,
            expires_in_seconds=self._settings.signed_url_ttl_seconds,
        )
        return PresignView(
            media_key=key,
            upload_url=grant.url,
            required_headers=dict(grant.required_headers),
            content_type=command.content_type,
            kind=command.kind.value,
            expires_in_seconds=grant.expires_in_seconds,
        )

    async def confirm(self, user_id: UserId, media_key: str) -> MediaView:
        """Verify an upload landed and record its verified metadata.

        The key is re-parsed rather than trusted: it must be one this server issued, which
        both re-derives the kind and keeps a hand-crafted key (a traversal, or another
        account's object) from ever reaching the storage adapter.
        """
        parsed = parse_storage_key(media_key)
        if parsed is None:
            raise ValidationFailed(
                "That media key was not issued by this service.",
                code=ErrorCode.MEDIA_UPLOAD_FAILED,
                details={"field": "mediaKey"},
            )
        kind, _extension = parsed

        # Idempotent retry: if the first confirm already created the row, hand it back
        # rather than re-reading storage or tripping the unique key.
        existing = await self._uow.media.get_by_storage_key(media_key)
        if existing is not None:
            return await self._view(existing)

        if not await self._storage.exists(media_key):
            raise ValidationFailed(
                "The upload was not found. It may have expired before it completed.",
                code=ErrorCode.MEDIA_UPLOAD_FAILED,
                details={"field": "mediaKey"},
            )

        data = await self._storage.get(media_key)

        max_bytes = max_bytes_allowed(
            kind,
            image_max=self._settings.media_max_image_bytes,
            audio_max=self._settings.media_max_audio_bytes,
        )
        if len(data) > max_bytes:
            await self._storage.delete(media_key)
            raise ValidationFailed(
                "The upload is larger than this kind allows.",
                code=ErrorCode.MEDIA_UPLOAD_FAILED,
                details={"field": "mediaKey", "maxBytes": max_bytes},
            )

        sniffed = sniff_content_type(data[:_HEADER_BYTES])
        if sniffed is None or sniffed not in ALLOWED_CONTENT_TYPES[kind]:
            await self._storage.delete(media_key)
            raise ValidationFailed(
                "The upload's actual type is not allowed for this kind.",
                code=ErrorCode.MEDIA_TYPE_NOT_ALLOWED,
                details={"kind": kind.value, "detected": sniffed},
            )

        asset = MediaAsset(
            id=new_id(),
            owner_id=user_id,
            kind=kind,
            storage_key=media_key,
            content_type=sniffed,
            size_bytes=len(data),
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            created_at=utc_now(),
        )
        await self._uow.media.add(asset)
        await self._uow.commit()
        return await self._view(asset)

    async def get(self, media_id: uuid.UUID) -> MediaView:
        """Describe an existing asset and re-sign a read URL for it.

        Any authenticated caller may read any asset by id. The id is a non-enumerable
        UUID and the URL is short-lived and signed, and the assets this phase stores —
        avatars and curriculum aids — are meant to be visible to other learners.
        """
        asset = await self._uow.media.get(media_id)
        if asset is None:
            raise NotFoundError("No media with that id.", code=ErrorCode.MEDIA_NOT_FOUND)
        return await self._view(asset)

    async def _view(self, asset: MediaAsset) -> MediaView:
        download = await self._storage.presigned_url(
            asset.storage_key,
            expires_in_seconds=self._settings.signed_url_ttl_seconds,
        )
        return MediaView(
            id=asset.id,
            kind=asset.kind.value,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
            checksum_sha256=asset.checksum_sha256,
            url=download.url,
            expires_in_seconds=download.expires_in_seconds,
            created_at=asset.created_at,
        )


__all__ = ["MediaService"]
