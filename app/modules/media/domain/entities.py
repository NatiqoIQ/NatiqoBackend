"""The media aggregate: one stored object and the facts the server verified about it.

Deliberately minimal. Everything here is either server-generated (the id, the key) or
server-observed (the real content type, the size, the checksum); nothing is a client's
assertion. That is the whole security posture of the module in one sentence: an upload is
described by what the bytes proved, not by what the request claimed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.core.types import UserId
from app.modules.media.domain.policy import MediaKind

__all__ = ["MediaAsset"]


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """A stored object's metadata.

    Frozen because the object it describes is immutable: replacing an avatar is a new
    upload with a new key and a new row, never an edit of this one. That keeps the
    checksum honest — it describes exactly the bytes at ``storage_key`` and can never
    drift away from them.

    ``owner_id`` is nullable because not every asset has an account behind it. A user's
    avatar does; curriculum media imported by a later phase does not. The row is
    ``ON DELETE SET NULL`` rather than cascading, because deleting the uploader must not
    silently break every lesson that references their clip.
    """

    id: uuid.UUID
    owner_id: UserId | None
    kind: MediaKind
    storage_key: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    created_at: datetime
