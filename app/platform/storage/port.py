"""Object storage port.

Audio and media bytes never go into PostgreSQL (plan §19). Only metadata lives in
the database; the binary payload is addressed by ``storage_key`` and delivered with
time-limited signed URLs.

Two directions are signed, and they are deliberately distinct:

* :meth:`ObjectStoragePort.presigned_url` signs a **read**. It is issued when a client
  asks to play or display something the server already knows exists.
* :meth:`ObjectStoragePort.presigned_upload` signs a **write**. It is issued before a
  client uploads, so the bytes go straight from the device to storage and never pass
  through the API process — which is the only way a 40 MB pronunciation clip can coexist
  with a 1 MB request-body ceiling without the ceiling being quietly raised for everyone.

The two return different dataclasses on purpose: a caller that has an upload grant has
no use for a download grant's fields and vice versa, and one shared "signed URL" type
would let the wrong grant be sent to a client.
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


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    """A grant to *write* one key.

    ``required_headers`` carries the headers the signature was computed over — for S3,
    ``Content-Type`` — because a client that PUTs a different one produces a signature
    mismatch the object store reports as a 403. Passing the requirement along is the
    difference between a usable grant and one that only fails after the bytes have
    already crossed the network.
    """

    url: str
    expires_in_seconds: int
    required_headers: dict[str, str]


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

    async def presigned_upload(
        self,
        key: str,
        *,
        content_type: str,
        expires_in_seconds: int | None = None,
    ) -> PresignedUpload: ...
