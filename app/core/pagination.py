"""Opaque cursors for keyset ("seek") pagination.

Catalogue collections are ordered by ``(position, id)`` and paged with a cursor rather
than ``LIMIT/OFFSET``. The reason is that these rows are edited while a learner is
browsing: an editor publishes a unit between two requests and every later row shifts
down one slot, so ``OFFSET 20`` would silently skip a unit the learner never saw. A
cursor names the last row that was actually delivered, so a page boundary stays correct
even as rows appear and disappear around it.

The cursor is deliberately opaque. Clients must echo the string back untouched and must
not construct one, because the moment they depend on the layout the ordering guarantee
stops being ours to change.

``position`` is not unique on its own, so the ``id`` half of the key is what stops two
siblings from paging onto each other.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: Bumped only when the payload layout changes. A cursor stamped with any other
#: version is refused rather than misread.
CURSOR_VERSION: Final = "v1"


class InvalidCursor(ValueError):
    """A cursor that cannot be trusted to describe a position in the ordering.

    The application layer turns this into a 422. It is deliberately *not* silently
    coerced: guessing a position would skip content, and a learner seeing a gap has no
    way to tell it from a bug.
    """


@dataclass(frozen=True, slots=True)
class PositionCursor:
    """The last row delivered on the previous page, as a sort key."""

    position: int
    id: uuid.UUID

    def __post_init__(self) -> None:
        if self.position < 1:
            raise InvalidCursor("Cursor position must be one or greater.")


def encode_position_cursor(cursor: PositionCursor) -> str:
    """Render a cursor as an unpadded URL-safe token."""
    raw = f"{CURSOR_VERSION}:{cursor.position}:{cursor.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_position_cursor(raw: str) -> PositionCursor:
    """Parse a token produced by :func:`encode_position_cursor`.

    Every failure mode raises :class:`InvalidCursor`; nothing is repaired or defaulted.
    """
    if not raw:
        raise InvalidCursor("Cursor must not be empty.")

    padded = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursor("Cursor is not readable.") from exc

    parts = decoded.split(":")
    if len(parts) != 3:
        raise InvalidCursor("Cursor is not readable.")

    version, position_text, id_text = parts

    if version != CURSOR_VERSION:
        raise InvalidCursor(f"Unsupported cursor version: {version!r}.")

    try:
        position = int(position_text)
    except ValueError as exc:
        raise InvalidCursor("Cursor position is not a number.") from exc

    try:
        identifier = uuid.UUID(id_text)
    except ValueError as exc:
        raise InvalidCursor("Cursor id is not a UUID.") from exc

    return PositionCursor(position=position, id=identifier)


@dataclass(frozen=True, slots=True)
class PageResult[T]:
    """What a use case hands back: the page, plus what a client needs to ask for the next.

    Deliberately not :class:`app.core.responses.Page`. That one is the Pydantic wire
    model, and a use case returning it would drag the framework into the application
    layer for no benefit — the presentation layer converts this into ``Page`` in one
    call to ``build_page``.

    ``has_more`` is authoritative and ``next_cursor`` is ``None`` whenever it is false,
    so a client that only checks one of the two still behaves correctly.
    """

    items: list[T]
    has_more: bool
    next_cursor: str | None = None


def paginate[T](
    rows: list[T],
    *,
    limit: int,
    key_of: Callable[[T], tuple[int, uuid.UUID]],
) -> PageResult[T]:
    """Turn ``limit + 1`` fetched rows into a page.

    The repository fetches one row more than asked for; that extra row is never
    returned, it only proves a further page exists. The cursor is built from the last row
    *actually delivered* — using the first row of the next page instead is the classic
    off-by-one, and it either skips a row or serves it twice.
    """
    items = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = None
    if has_more and items:
        position, row_id = key_of(items[-1])
        next_cursor = encode_position_cursor(PositionCursor(position=position, id=row_id))
    return PageResult(items=items, has_more=has_more, next_cursor=next_cursor)
