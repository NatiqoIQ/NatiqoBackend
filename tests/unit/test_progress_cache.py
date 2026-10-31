"""Progress-summary cache encoding (F10).

The cache stores JSON, not a pickled dataclass: the value has to survive a Redis round trip
and a new process. The failure that matters is a corrupt or older payload — it must be a
miss, never a 500 on the progress screen.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.modules.quiz.application.progress_cache import (
    decode_summary,
    encode_summary,
    invalidate_progress_summary,
)
from app.modules.quiz.application.progress_dto import ProgressSummaryView
from app.platform.cache.memory import InMemoryCache


def _view() -> ProgressSummaryView:
    return ProgressSummaryView(
        total_xp=42,
        lessons_completed=2,
        lessons_in_progress=1,
        lessons_started=3,
        correct_count=9,
        answered_count=12,
        accuracy_percent=75,
        current_streak_days=4,
        longest_streak_days=7,
        words_learned=18,
        last_activity_at=datetime(2026, 10, 30, 12, 0, tzinfo=UTC),
    )


def test_a_summary_round_trips_through_json() -> None:
    assert decode_summary(encode_summary(_view())) == _view()


def test_a_missing_activity_timestamp_round_trips_as_none() -> None:
    view = ProgressSummaryView(
        total_xp=0,
        lessons_completed=0,
        lessons_in_progress=0,
        lessons_started=0,
        correct_count=0,
        answered_count=0,
        accuracy_percent=0,
        current_streak_days=0,
        longest_streak_days=0,
        words_learned=0,
        last_activity_at=None,
    )
    assert decode_summary(encode_summary(view)).last_activity_at is None


def test_a_corrupt_payload_raises_so_the_caller_can_miss() -> None:
    with pytest.raises(ValueError):
        decode_summary("{not json")
    with pytest.raises((ValueError, TypeError, KeyError)):
        decode_summary('{"total_xp": 1}')


@pytest.mark.asyncio
async def test_invalidation_removes_the_key() -> None:
    cache = InMemoryCache()
    user_id = uuid.uuid4()
    key = cache.build_key("progress", "summary", str(user_id))
    await cache.set(key, "value")

    await invalidate_progress_summary(cache, user_id)  # type: ignore[arg-type]

    assert await cache.get(key) is None


@pytest.mark.asyncio
async def test_an_invalidation_failure_never_reaches_the_command() -> None:
    class Broken:
        def build_key(self, *parts: str) -> str:
            return ":".join(parts)

        async def delete(self, *keys: str) -> int:
            raise RuntimeError("cache down")

    await invalidate_progress_summary(Broken(), uuid.uuid4())  # type: ignore[arg-type]
