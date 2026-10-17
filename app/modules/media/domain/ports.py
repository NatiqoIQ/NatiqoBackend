"""Persistence port for the media module.

One port, three methods, and no writes past ``add``: an asset row is created once when an
upload is confirmed and never updated. The read by ``storage_key`` exists so confirmation
is idempotent — a client that retries confirm after a dropped response gets the row the
first attempt created rather than a duplicate or a unique-constraint 500.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.modules.media.domain.entities import MediaAsset


@runtime_checkable
class MediaRepositoryPort(Protocol):
    async def add(self, asset: MediaAsset) -> None: ...

    async def get(self, media_id: uuid.UUID) -> MediaAsset | None: ...

    async def get_by_storage_key(self, storage_key: str) -> MediaAsset | None: ...


__all__ = ["MediaRepositoryPort"]
