"""Read models and commands for the media surface.

Framework-free like every other module's DTOs, so the service can be exercised without a
web layer. The wire schemas are a thin projection of these.

:class:`PresignView` and :class:`MediaView` are kept apart on purpose. A presign response
describes a grant — a key, a URL, the headers the signature was bound to. A media view
describes an asset that now exists — its verified size, type and checksum, plus a read
URL. Collapsing them would let a client treat an upload grant as proof of ownership, which
is exactly the confusion the confirm step exists to prevent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.modules.media.domain.policy import MediaKind

__all__ = ["MediaView", "PresignCommand", "PresignView"]


@dataclass(frozen=True, slots=True)
class PresignCommand:
    """A request to store a file: what it is for, and what the client says it is."""

    kind: MediaKind
    content_type: str


@dataclass(frozen=True, slots=True)
class PresignView:
    """The grant handed back to the client before it uploads.

    ``required_headers`` are not decoration: an S3 signature computed over a
    ``Content-Type`` is rejected if the PUT sends a different one, so the client is told
    exactly what to send rather than discovering the requirement via a 403 after the bytes
    have already gone up.
    """

    media_key: str
    upload_url: str
    required_headers: dict[str, str]
    content_type: str
    kind: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class MediaView:
    """A stored asset, as the API describes it once it exists."""

    id: uuid.UUID
    kind: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    url: str
    expires_in_seconds: int
    created_at: datetime
