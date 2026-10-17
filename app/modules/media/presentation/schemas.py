"""Wire schemas for the media surface.

Fields are listed by hand rather than inherited from the read models, the same way every
other surface does it: the JSON is a contract, and a field added to a DTO cannot silently
start leaking to clients.

The kind is accepted as the enum, so an unknown value is a 422 at the edge rather than a
500 from a dictionary miss deep in the service. The declared ``contentType`` travels as a
plain string because it is untrusted input the service validates against the kind's
allow-list — typing it as a literal set here would move a policy decision into the schema
and give the two places something to disagree about. Responses carry the *sniffed* type,
which is why the field is documented rather than implied.

``from_attributes`` (via ``_MediaWire``) lets the routes hand a read model straight to a
response model, exactly as the other surfaces do.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import ConfigDict, Field

from app.core.responses import WireModel
from app.modules.media.domain.policy import MediaKind

__all__ = [
    "MediaConfirmRequest",
    "MediaPresignRequest",
    "MediaPresignResponse",
    "MediaResponse",
]


class _MediaWire(WireModel):
    """A wire model that reads attributes off a read model."""

    model_config = ConfigDict(from_attributes=True)


class MediaPresignRequest(WireModel):
    """A request to store one file.

    ``kind`` is what the file is *for*; ``content_type`` is what the client claims it is.
    The claim is checked against the kind's allow-list before a grant is issued, so an
    obviously wrong request fails before any bytes move — but it is never trusted for a
    security decision. What the file actually is, is decided at confirm from the bytes.
    """

    kind: MediaKind
    content_type: str = Field(min_length=3, max_length=100)


class MediaPresignResponse(_MediaWire):
    """The grant the client uploads against.

    ``required_headers`` is the client's contract with the signature: an S3 signature that
    was computed over a ``Content-Type`` is rejected if the PUT sends a different one, so
    the requirement is stated up front instead of discovered as a 403 after the upload.
    """

    media_key: str
    upload_url: str
    required_headers: dict[str, str]
    content_type: str
    kind: str
    expires_in_seconds: int


class MediaConfirmRequest(WireModel):
    """Confirmation that an upload landed. The key is the one the presign response returned."""

    media_key: str = Field(min_length=1, max_length=255)


class MediaResponse(_MediaWire):
    """A stored asset.

    ``content_type`` is the *sniffed* type, not the declared one, so a client reading this
    back learns what the server actually stored. ``sizeBytes`` and ``checksumSha256`` are
    measured from the object, and ``url`` is a short-lived signed read URL rather than the
    object key — clients never need the key, and never see one that outlives the TTL.
    """

    id: uuid.UUID
    kind: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    url: str
    expires_in_seconds: int
    created_at: datetime
