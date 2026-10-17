"""SQLAlchemy implementation of the media port.

Insert-only, matching the aggregate: there is no ``save``. An asset is created once and
never edited, so the only two reads are by primary key (the read endpoint) and by storage
key (idempotent confirmation). The mapper is hand-written like the rest of the project, so
the ``kind`` column's string form stays a storage detail the domain never sees.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import UserId
from app.modules.media.domain.entities import MediaAsset
from app.modules.media.domain.policy import MediaKind
from app.modules.media.infrastructure.models import MediaAssetModel


def _to_asset(model: MediaAssetModel) -> MediaAsset:
    return MediaAsset(
        id=model.id,
        owner_id=UserId(model.owner_id) if model.owner_id is not None else None,
        kind=MediaKind(model.kind),
        storage_key=model.storage_key,
        content_type=model.content_type,
        size_bytes=model.size_bytes,
        checksum_sha256=model.checksum_sha256,
        created_at=model.created_at,
    )


def _to_model(asset: MediaAsset) -> MediaAssetModel:
    return MediaAssetModel(
        id=asset.id,
        owner_id=asset.owner_id,
        kind=asset.kind.value,
        storage_key=asset.storage_key,
        content_type=asset.content_type,
        size_bytes=asset.size_bytes,
        checksum_sha256=asset.checksum_sha256,
        created_at=asset.created_at,
    )


class SqlAlchemyMediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, asset: MediaAsset) -> None:
        self._session.add(_to_model(asset))

    async def get(self, media_id: uuid.UUID) -> MediaAsset | None:
        model = await self._session.get(MediaAssetModel, media_id)
        return _to_asset(model) if model is not None else None

    async def get_by_storage_key(self, storage_key: str) -> MediaAsset | None:
        stmt = select(MediaAssetModel).where(MediaAssetModel.storage_key == storage_key)
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_asset(model) if model is not None else None


__all__ = ["SqlAlchemyMediaRepository"]
