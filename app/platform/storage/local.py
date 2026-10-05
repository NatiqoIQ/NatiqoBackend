"""Local filesystem object storage (development).

Deliberately simple and unsuitable for multi-node deployments: it is the fallback
that lets the media pipeline be exercised end-to-end without Docker.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

from app.core.config import get_settings
from app.platform.storage.port import PresignedDownload, StorageError, StoredObject

_CHUNK = 65536


class LocalObjectStorage:
    def __init__(self, root: str | Path | None = None) -> None:
        settings = get_settings()
        self._root = Path(root or settings.local_storage_path).resolve()

    def _path(self, key: str) -> Path:
        # Reject traversal: keys are server-generated but never trust a stored value.
        # `is_relative_to` rather than `str.startswith`: a sibling directory whose name
        # merely begins with the root's ("store-evil" next to "store") satisfies a prefix
        # check while being somewhere else entirely, so ".." walks straight out.
        candidate = (self._root / key).resolve()
        if not candidate.is_relative_to(self._root):
            raise StorageError(f"Refusing to access key outside storage root: {key!r}")
        return candidate

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        path = self._path(key)

        def _write() -> StoredObject:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            sidecar = path.with_suffix(path.suffix + ".meta")
            sidecar.write_text(content_type, encoding="utf-8")
            return StoredObject(
                key=key,
                size_bytes=len(data),
                content_type=content_type,
                checksum_sha256=hashlib.sha256(data).hexdigest(),
            )

        return await asyncio.to_thread(_write)

    async def get(self, key: str) -> bytes:
        path = self._path(key)

        def _read() -> bytes:
            if not path.is_file():
                raise StorageError(f"Object not found: {key}")
            return path.read_bytes()

        return await asyncio.to_thread(_read)

    async def stream(self, key: str, chunk_size: int = _CHUNK) -> AsyncIterator[bytes]:
        data = await self.get(key)
        for offset in range(0, len(data), chunk_size):
            yield data[offset : offset + chunk_size]

    async def delete(self, key: str) -> bool:
        path = self._path(key)

        def _delete() -> bool:
            removed = False
            if path.is_file():
                path.unlink()
                removed = True
            sidecar = path.with_suffix(path.suffix + ".meta")
            if sidecar.is_file():
                sidecar.unlink()
            return removed

        return await asyncio.to_thread(_delete)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    async def presigned_url(
        self, key: str, *, expires_in_seconds: int | None = None
    ) -> PresignedDownload:
        """No signing in local mode — the key is served by a development-only route."""
        ttl = expires_in_seconds or get_settings().signed_url_ttl_seconds
        return PresignedDownload(url=f"local://{key}", expires_in_seconds=ttl)
