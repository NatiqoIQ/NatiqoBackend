"""Object storage port.

Audio and media bytes never go into PostgreSQL (plan §19). Only metadata lives in
the database; the binary payload is addressed by ``storage_key`` and delivered with
time-limited signed URLs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class StorageError(RuntimeError):
    """Raised by adapters when the backing store is unreachable or rejects a write."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    content_type: str
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class PresignedDownload:
    url: str
    expires_in_seconds: int


@runtime_checkable
class ObjectStoragePort(Protocol):
    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
    ) -> StoredObject: ...

    async def get(self, key: str) -> bytes: ...

    def stream(self, key: str, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        """Stream an object in chunks. Declared without ``async`` because an async
        generator is called, not awaited."""
        ...

    async def delete(self, key: str) -> bool: ...

    async def exists(self, key: str) -> bool: ...

    async def presigned_url(
        self, key: str, *, expires_in_seconds: int | None = None
    ) -> PresignedDownload: ...
