"""Content-type policy and real-type detection for uploaded media.

This module answers the two questions that justify a media pipeline existing at all,
and neither is one a client can be trusted to answer about its own upload:

* **May this kind of file be stored?** Each :class:`MediaKind` declares the content types
  it accepts, so an avatar cannot be a PDF and a pronunciation clip cannot be a shell
  script. The declared type is checked here at *presign* time, purely so an obviously
  wrong request fails before any bytes move.
* **What is this file, really?** The declared ``Content-Type`` is a client's claim, and a
  client that labels a bash script ``image/jpeg`` is the exact case this exists to stop.
  :func:`sniff_content_type` names the type from the leading bytes, so the confirm step
  can gate on the bytes rather than on the label. The declared type is used for nothing
  more than choosing the key's extension.

Keys are server-generated in a fixed ``<kind>/<uuid>.<ext>`` shape and re-parsed on
confirm. That shape is load-bearing twice: it stops a client from confirming into an
arbitrary key (a ````..```` walk, or someone else's object), and it re-derives the kind
from the key rather than trusting a second client-supplied field.
"""

from __future__ import annotations

import re
import uuid
from enum import StrEnum

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MEDIA_KINDS",
    "MediaKind",
    "build_storage_key",
    "max_bytes_allowed",
    "parse_storage_key",
    "sniff_content_type",
]


class MediaKind(StrEnum):
    """The buckets an upload can land in.

    Distinct from a content type: a kind is what the *product* is doing with the file
    (a profile picture, a question illustration, a listening clip, a pronunciation
    recording), and several content types are valid for one kind. Keeping the two
    separate is what lets the policy say "an avatar is a JPEG, a PNG or a WebP" without
    enumerating every kind the JPEG type is acceptable for.
    """

    AVATAR = "avatar"
    IMAGE = "image"
    AUDIO = "audio"
    PRONUNCIATION = "pronunciation"


#: What may be stored under each kind.
#:
#: ``image/gif`` is allowed for a general illustration but not for an avatar: an
#: animated profile picture is a UI decision nobody asked for, while an animated
#: illustration in a lesson is legitimate.
ALLOWED_CONTENT_TYPES: dict[MediaKind, frozenset[str]] = {
    MediaKind.AVATAR: frozenset({"image/jpeg", "image/png", "image/webp"}),
    MediaKind.IMAGE: frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"}),
    MediaKind.AUDIO: frozenset(
        {"audio/mpeg", "audio/mp4", "audio/wav", "audio/x-wav", "audio/ogg", "audio/aac"}
    ),
    MediaKind.PRONUNCIATION: frozenset(
        {"audio/mpeg", "audio/mp4", "audio/wav", "audio/x-wav", "audio/ogg", "audio/aac"}
    ),
}

#: The kind names, for the ``CHECK`` on the column and for error details.
MEDIA_KINDS: tuple[str, ...] = tuple(kind.value for kind in MediaKind)

#: Extension written into the key for each declared type. Cosmetic — the row records the
#: *sniffed* type, so a benign relabel never leaves the stored metadata lying. A type with
#: no entry here is still keyed, just without a meaningful extension.
_EXTENSIONS: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/aac": "aac",
}

#: Kinds whose ceiling is the image one; everything else uses the audio ceiling.
_IMAGE_KINDS: frozenset[MediaKind] = frozenset({MediaKind.AVATAR, MediaKind.IMAGE})

#: ``<kind>/<32 hex>.<2-5 alphanumeric>``. Anchored and extension-bounded so a key that
#: is not one *this server issued* is refused by grammar alone.
_KEY_PATTERN = re.compile(r"^(avatar|image|audio|pronunciation)/([0-9a-f]{32})\.([a-z0-9]{2,5})$")


def build_storage_key(kind: MediaKind, content_type: str) -> str:
    """Mint a fresh key for one upload.

    The random component is a UUID rather than anything derived from the request, so two
    uploads can never collide and a client cannot influence where its bytes land. The
    extension is derived from the declared type only because it makes a bucket readable
    to a human; correctness never depends on it.
    """
    extension = _EXTENSIONS.get(content_type, "bin")
    return f"{kind.value}/{uuid.uuid4().hex}.{extension}"


def parse_storage_key(key: str) -> tuple[MediaKind, str] | None:
    """Return the ``(kind, extension)`` a server-issued key encodes, or ``None``.

    ``None`` means "this is not a key we handed out", and the caller turns that into an
    upload failure rather than probing storage with it. Rejecting here is what keeps a
    traversal attempt from ever reaching the adapter.
    """
    match = _KEY_PATTERN.match(key)
    if match is None:
        return None
    return MediaKind(match.group(1)), match.group(3)


def max_bytes_allowed(kind: MediaKind, *, image_max: int, audio_max: int) -> int:
    """The size ceiling for a kind. Ceilings are configuration, so they are passed in
    rather than baked in here — the domain decides *which* ceiling applies, the settings
    decide how large it is."""
    return image_max if kind in _IMAGE_KINDS else audio_max


def _looks_like_mpeg_frame(header: bytes) -> bool:
    """An MPEG audio frame begins with eleven set bits (``0xFFE0`` masked).

    Covers the common ``0xFF FB``/``F3``/``F2``/``E3`` variants, which is what a file
    that starts with a bare frame instead of an ``ID3`` tag looks like.
    """
    return len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0


def sniff_content_type(header: bytes) -> str | None:
    """Name the file type from its leading bytes, or ``None`` if unrecognised.

    Only the magic numbers this pipeline accepts are recognised; anything else — a shell
    script, an executable, a zip — returns ``None`` and is refused. The caller passes in
    the first handful of bytes, so a multi-megabyte object is never fully read to answer
    this question.

    ``RIFF`` is checked for both its containers because the first four bytes are shared:
    the discriminator is at offset eight (``WEBP`` vs ``WAVE``).
    """
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    if header.startswith(b"ID3") or _looks_like_mpeg_frame(header):
        return "audio/mpeg"
    # ``ftyp`` at offset four is the ISO base media container: M4A and the MP4/HEIC
    # family. Treated as ``audio/mp4`` because that is the only member this pipeline
    # stores, and a kind check still has to pass for it to be accepted.
    if header[4:8] == b"ftyp":
        return "audio/mp4"
    return None
