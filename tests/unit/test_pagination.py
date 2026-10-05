"""Cursor codec tests.

Catalogue pages are ordered by ``(position, id)`` and walked with an opaque cursor
rather than an offset. That is deliberate: a published unit can be inserted or
retired between two requests, and ``OFFSET`` would then silently skip or repeat a row
on the learner's screen. The cursor makes the ordering total and stable.

The codec is a public contract. A cursor arrives from an untrusted client, so a
malformed one has to fail loudly rather than being coerced into a plausible position
that would skip content.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from app.core.pagination import (
    InvalidCursor,
    PositionCursor,
    decode_position_cursor,
    encode_position_cursor,
)


def _cursor() -> PositionCursor:
    return PositionCursor(position=7, id=uuid.uuid4())


class TestRoundTrip:
    def test_a_cursor_survives_the_round_trip(self) -> None:
        cursor = _cursor()

        assert decode_position_cursor(encode_position_cursor(cursor)) == cursor

    def test_the_encoding_is_url_safe_and_unpadded(self) -> None:
        """The cursor travels in a query string. Base64's standard alphabet contains
        ``+`` and ``/``, which have to be percent-encoded by every client, and the
        ``=`` padding is optional-but-annoying; ``urlsafe_b64encode`` without padding
        avoids both."""
        encoded = encode_position_cursor(_cursor())

        assert not set(encoded) & set("+/=")
        assert encoded == encoded.strip()

    def test_two_cursors_at_the_same_position_differ_by_id(self) -> None:
        """``position`` alone is not unique, so the id half of the key is what keeps
        two siblings from paging onto each other."""
        first = PositionCursor(position=3, id=uuid.uuid4())
        second = PositionCursor(position=3, id=uuid.uuid4())

        assert encode_position_cursor(first) != encode_position_cursor(second)
        assert decode_position_cursor(encode_position_cursor(first)) == first
        assert decode_position_cursor(encode_position_cursor(second)) == second


class TestDecodeFailures:
    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("not-base64-$$$", id="not base64"),
            pytest.param("", id="empty"),
            pytest.param("dg", id="valid base64 but meaningless"),
        ],
    )
    def test_unusable_input_is_refused(self, raw: str) -> None:
        with pytest.raises(InvalidCursor):
            decode_position_cursor(raw)

    def test_a_foreign_cursor_version_is_refused(self) -> None:
        """Versioning the payload means a future change to the key can be detected
        instead of misread. A v0 cursor must not be silently accepted as v1."""

        raw = base64.urlsafe_b64encode(b"v0:4:6f0a0f0e-0000-0000-0000-000000000000").decode()

        with pytest.raises(InvalidCursor, match="version"):
            decode_position_cursor(raw.rstrip("="))

    def test_a_non_integer_position_is_refused(self) -> None:

        raw = base64.urlsafe_b64encode(b"v1:second:abc").decode().rstrip("=")

        with pytest.raises(InvalidCursor, match="position"):
            decode_position_cursor(raw)

    def test_a_non_uuid_is_refused(self) -> None:

        raw = base64.urlsafe_b64encode(b"v1:4:not-a-uuid").decode().rstrip("=")

        with pytest.raises(InvalidCursor, match="id"):
            decode_position_cursor(raw)

    def test_a_truncated_cursor_is_refused(self) -> None:
        """A client that clips the query string must get an error, not a page that
        starts from a made-up position."""
        raw = encode_position_cursor(_cursor())[:-6]

        with pytest.raises(InvalidCursor):
            decode_position_cursor(raw)

    def test_extra_fields_are_refused(self) -> None:
        """Trailing junk means the payload is not the one this version wrote."""

        raw = base64.urlsafe_b64encode(b"v1:4:6f0a0f0e-0000-0000-0000-000000000000:extra").decode()

        with pytest.raises(InvalidCursor):
            decode_position_cursor(raw.rstrip("="))

    def test_a_non_positive_position_is_refused(self) -> None:

        raw = base64.urlsafe_b64encode(b"v1:0:6f0a0f0e-0000-0000-0000-000000000000").decode()

        with pytest.raises(InvalidCursor, match="position"):
            decode_position_cursor(raw.rstrip("="))
