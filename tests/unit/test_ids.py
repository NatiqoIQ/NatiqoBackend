"""UUIDv7 generation (F21).

The reason to leave UUIDv4 is index locality, so these tests care about ordering at
least as much as uniqueness. A v7 that is not monotonic would still be unique and would
still fail the purpose.
"""

from __future__ import annotations

import time
import uuid

from app.core import ids
from app.core.ids import uuid7


class TestShape:
    def test_it_is_a_version_7_uuid(self) -> None:
        value = uuid7()
        assert value.version == 7
        assert value.variant == uuid.RFC_4122

    def test_the_timestamp_is_the_current_time(self) -> None:
        before = time.time_ns() // 1_000_000
        value = uuid7()
        after = time.time_ns() // 1_000_000
        embedded = value.int >> 80
        assert before - 5 <= embedded <= after + 5

    def test_ids_are_unique(self) -> None:
        assert len({uuid7() for _ in range(10_000)}) == 10_000


class TestOrdering:
    def test_generated_ids_sort_in_creation_order(self) -> None:
        values = [uuid7() for _ in range(5_000)]
        assert values == sorted(values)

    def test_a_frozen_clock_still_produces_increasing_ids(self, monkeypatch) -> None:
        monkeypatch.setattr(ids, "_last_ms", 0)
        monkeypatch.setattr(ids, "_last_seq", 0)
        monkeypatch.setattr(ids.time, "time_ns", lambda: 1_700_000_000_000_000_000)
        values = [uuid7() for _ in range(100)]
        assert values == sorted(values)
        assert len(set(values)) == 100

    def test_a_backwards_clock_never_regresses(self, monkeypatch) -> None:
        monkeypatch.setattr(ids, "_last_ms", 0)
        monkeypatch.setattr(ids, "_last_seq", 0)
        times = iter([1_700_000_000_000_000_000, 1_699_999_999_000_000_000])
        monkeypatch.setattr(ids.time, "time_ns", lambda: next(times))
        first = uuid7()
        second = uuid7()
        assert second > first


class TestMixin:
    def test_models_default_to_v7(self) -> None:
        from app.modules.quiz.infrastructure.models import QuizAnswerModel

        column = QuizAnswerModel.__table__.c.id
        generated = column.default.arg(None)  # type: ignore[union-attr]
        assert generated.version == 7
