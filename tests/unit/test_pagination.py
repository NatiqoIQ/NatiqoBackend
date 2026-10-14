"""Cursor codec tests.

Catalogue pages are ordered by ``(position, id)`` and walked with an opaque cursor
rather than an offset. That is deliberate: a published unit can be inserted or
retired between two requests, and ``OFFSET`` would then silently skip or repeat a row
on the learner's screen. The cursor makes the ordering total and stable.

The same codec exists for a second shape — an appended stream like the XP history, whose
natural order is *when it happened* rather than an editorial position. That cursor is
encoded by ``encode_time_cursor`` and versioned separately (``t1`` vs ``v1``), so a token
from one family is refused instead of being misread as the other.

The codec is a public contract. A cursor arrives from an untrusted client, so a
malformed one has to fail loudly rather than being coerced into a plausible position
that would skip content.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.pagination import (
    InvalidCursor,
    PositionCursor,
    TimeCursor,
    decode_position_cursor,
    decode_time_cursor,
    encode_position_cursor,
    encode_time_cursor,
    paginate_time,
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


def _time_cursor(*, identifier: uuid.UUID | None = None) -> TimeCursor:
    return TimeCursor(
        timestamp=datetime(2026, 3, 1, 12, 30, 0, tzinfo=UTC),
        id=identifier or uuid.uuid4(),
    )


class TestTimeRoundTrip:
    def test_a_time_cursor_survives_the_round_trip(self) -> None:
        cursor = _time_cursor()

        assert decode_time_cursor(encode_time_cursor(cursor)) == cursor

    def test_sub_second_precision_is_kept(self) -> None:
        """The feed is ordered by instant, so losing microseconds would reorder or drop
        two sessions that finished in the same millisecond."""
        cursor = TimeCursor(
            timestamp=datetime(2026, 3, 1, 12, 30, 0, 123456, tzinfo=UTC), id=uuid.uuid4()
        )

        assert decode_time_cursor(encode_time_cursor(cursor)) == cursor

    def test_the_encoding_is_url_safe_and_unpadded(self) -> None:
        encoded = encode_time_cursor(_time_cursor())

        assert not set(encoded) & set("+/=")
        assert encoded == encoded.strip()

    def test_the_two_cursor_families_do_not_interfere(self) -> None:
        """``t1`` and ``v1`` are separate versions, so a time cursor handed to the
        position decoder is refused rather than parsed as a position."""
        raw = encode_time_cursor(_time_cursor())

        with pytest.raises(InvalidCursor):
            decode_position_cursor(raw)

    def test_a_naive_timestamp_is_refused_at_construction(self) -> None:
        """Storage is always UTC; a naive cursor would silently mean local time."""
        with pytest.raises(InvalidCursor, match="timezone-aware"):
            TimeCursor(timestamp=datetime(2026, 3, 1, 12, 30, 0), id=uuid.uuid4())


class TestTimeDecodeFailures:
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
            decode_time_cursor(raw)

    def test_a_foreign_version_is_refused(self) -> None:
        raw = base64.urlsafe_b64encode(
            b"t2:123:6f0a0f0e-0000-0000-0000-000000000000"
        ).decode().rstrip("=")

        with pytest.raises(InvalidCursor, match="version"):
            decode_time_cursor(raw)

    def test_a_non_numeric_timestamp_is_refused(self) -> None:
        raw = (
            base64.urlsafe_b64encode(
                b"t1:soon:6f0a0f0e-0000-0000-0000-000000000000"
            )
            .decode()
            .rstrip("=")
        )

        with pytest.raises(InvalidCursor, match="timestamp"):
            decode_time_cursor(raw)

    def test_a_non_uuid_is_refused(self) -> None:
        raw = base64.urlsafe_b64encode(b"t1:0:not-a-uuid").decode().rstrip("=")

        with pytest.raises(InvalidCursor, match="id"):
            decode_time_cursor(raw)

    def test_a_truncated_cursor_is_refused(self) -> None:
        with pytest.raises(InvalidCursor):
            decode_time_cursor(encode_time_cursor(_time_cursor())[:-6])

    def test_extra_fields_are_refused(self) -> None:
        raw = base64.urlsafe_b64encode(
            b"t1:0:6f0a0f0e-0000-0000-0000-000000000000:extra"
        ).decode().rstrip("=")

        with pytest.raises(InvalidCursor):
            decode_time_cursor(raw)


class TestPaginateTime:
    """The cursor must come from the last row actually delivered.

    Building it from the first row of the *next* page is the classic off-by-one that
    repeats a row between two requests, so the boundary is asserted directly.
    """

    @staticmethod
    def _rows(count: int) -> list[TimeCursor]:
        base = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
        return [
            TimeCursor(
                timestamp=base - timedelta(days=index),
                id=uuid.UUID(int=index),
            )
            for index in range(count)
        ]

    @staticmethod
    def _key(row: TimeCursor) -> tuple[datetime, uuid.UUID]:
        return row.timestamp, row.id

    def test_it_trims_to_the_limit_and_reports_another_page(self) -> None:
        rows = self._rows(3)

        page = paginate_time(rows, limit=2, key_of=self._key)

        assert [row.id for row in page.items] == [rows[0].id, rows[1].id]
        assert page.has_more is True

    def test_the_cursor_is_built_from_the_last_delivered_row(self) -> None:
        rows = self._rows(3)

        page = paginate_time(rows, limit=2, key_of=self._key)

        assert page.next_cursor is not None
        assert decode_time_cursor(page.next_cursor) == rows[1]

    def test_a_full_final_page_reports_no_cursor(self) -> None:
        rows = self._rows(2)

        page = paginate_time(rows, limit=2, key_of=self._key)

        assert page.has_more is False
        assert page.next_cursor is None

    def test_paging_with_the_cursor_does_not_repeat_a_row(self) -> None:
        rows = self._rows(4)

        first = paginate_time(rows, limit=2, key_of=self._key)
        after = decode_time_cursor(first.next_cursor or "")
        second = paginate_time(
            [row for row in rows if (row.timestamp, row.id) < (after.timestamp, after.id)],
            limit=2,
            key_of=self._key,
        )

        assert first.items + second.items == rows

    def test_an_empty_page_is_not_an_error(self) -> None:
        page = paginate_time([], limit=5, key_of=self._key)

        assert page.items == []
        assert page.has_more is False
        assert page.next_cursor is None
