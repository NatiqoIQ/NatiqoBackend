"""S3-compatible object storage (AWS S3, MinIO, Cloudflare R2, ...).

Two write paths, and they serve different payloads:

* :meth:`put` buffers the body. It is the right tool for small objects the server
  itself produces or has already received — a generated exercise, a re-encoded clip.
* :meth:`presigned_upload` hands the client a grant and gets out of the way, so an
  avatar or a pronunciation recording travels from the device straight to the object
  store. The API never sees those bytes, which is what lets a large upload coexist
  with a tight request-body ceiling.

Both are bounded by the media service's size ceiling, which is checked on the *confirm*
path against the stored object rather than against a header a client controls.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.platform.storage.port import (
    PresignedDownload,
    PresignedUpload,
    StorageError,
    StoredObject,
)

logger = get_logger("storage.s3")

_CHUNK = 65536


class S3ObjectStorage:
    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    @classmethod
    def from_settings(cls) -> S3ObjectStorage:
        import boto3
        from botocore.config import Config

        settings = get_settings()
        if not (settings.s3_access_key_id and settings.s3_secret_access_key):
            raise StorageError("S3 credentials are not configured")
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key.get_secret_value(),
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if settings.s3_force_path_style else "auto"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cls(client, settings.s3_bucket)

    async def put(self, key: str, data: bytes, *, content_type: str) -> StoredObject:
        def _put() -> StoredObject:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )
            return StoredObject(
                key=key,
                size_bytes=len(data),
                content_type=content_type,
                checksum_sha256=hashlib.sha256(data).hexdigest(),
            )

        try:
            return await asyncio.to_thread(_put)
        except Exception as exc:
            raise StorageError(str(exc)) from exc

    async def get(self, key: str) -> bytes:
        def _get() -> bytes:
            try:
                response = self._client.get_object(Bucket=self._bucket, Key=key)
            except Exception as exc:
                raise StorageError(str(exc)) from exc
            return bytes(response["Body"].read())

        return await asyncio.to_thread(_get)

    async def stream(self, key: str, chunk_size: int = _CHUNK) -> AsyncIterator[bytes]:
        data = await self.get(key)
        for offset in range(0, len(data), chunk_size):
            yield data[offset : offset + chunk_size]

    async def delete(self, key: str) -> bool:
        def _delete() -> bool:
            try:
                self._client.delete_object(Bucket=self._bucket, Key=key)
            except Exception as exc:  # noqa: BLE001
                logger.warning("storage.delete_failed", key=key, error=str(exc))
                return False
            return True

        return await asyncio.to_thread(_delete)

    async def exists(self, key: str) -> bool:
        def _exists() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
            except Exception:  # noqa: BLE001
                return False
            return True

        return await asyncio.to_thread(_exists)

    async def presigned_url(
        self, key: str, *, expires_in_seconds: int | None = None
    ) -> PresignedDownload:
        settings = get_settings()
        ttl = expires_in_seconds or settings.signed_url_ttl_seconds

        def _sign() -> PresignedDownload:
            try:
                url = self._client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._bucket, "Key": key},
                    ExpiresIn=ttl,
                )
            except Exception as exc:
                raise StorageError(str(exc)) from exc
            return PresignedDownload(url=url, expires_in_seconds=ttl)

        return await asyncio.to_thread(_sign)

    async def presigned_upload(
        self,
        key: str,
        *,
        content_type: str,
        expires_in_seconds: int | None = None,
    ) -> PresignedUpload:
        settings = get_settings()
        ttl = expires_in_seconds or settings.signed_url_ttl_seconds

        def _sign() -> PresignedUpload:
            try:
                # ``ContentType`` is passed to the signer, not merely returned to the
                # caller: it becomes part of the signature, so a client that PUTs a
                # different type is rejected by the object store instead of quietly
                # storing bytes under a label that does not describe them.
                url = self._client.generate_presigned_url(
                    "put_object",
                    Params={"Bucket": self._bucket, "Key": key, "ContentType": content_type},
                    ExpiresIn=ttl,
                )
            except Exception as exc:
                raise StorageError(str(exc)) from exc
            return PresignedUpload(
                url=url,
                expires_in_seconds=ttl,
                required_headers={"Content-Type": content_type},
            )

        return await asyncio.to_thread(_sign)
