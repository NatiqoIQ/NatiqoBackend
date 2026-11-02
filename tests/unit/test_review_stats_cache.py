"""The review stats cache: one card scan plus one lifetime aggregate per open.

A learner keeps the review screen open and polls it; without a cache every poll pays
two database reads for figures that only an *answer* can change. The contract here
mirrors the progress summary: fail-open on a cache outage, invalidated right after the
answer's commit, TTL bounding read-only repeats only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.modules.quiz.application.review_cache import (
    decode_stats,
    encode_stats,
    invalidate_review_stats,
    review_stats_key,
)
from app.modules.quiz.application.review_dto import ReviewStatsView
from app.modules.quiz.domain.review import ReviewLogStats
from app.modules.quiz.application.review_service import ReviewService
from app.platform.cache.memory import InMemoryCache


class _StubCardsRow:
    due_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=2)


class _StubCards:
    def __init__(self, cards: list[Any]) -> None:
        self._cards = cards
        self.calls = 0

    async def list_for_user(self, user_id: Any) -> list[Any]:
        self.calls += 1
        return self._cards


class _StubLogs:
    def __init__(self, stats: ReviewLogStats) -> None:
        self._stats = stats
        self.calls = 0

    async def stats_for_user(self, user_id: Any) -> ReviewLogStats:
        self.calls += 1
        return self._stats


class _StubUow:
    def __init__(self) -> None:
        self.review_cards = _StubCards([_StubCardsRow()])
        self.review_logs = _StubLogs(ReviewLogStats(total=10, correct=9))


class _BrokenCache:
    """A cache that accepts keys and then fails every operation."""

    async def get(self, key: str) -> str | None:
        raise ConnectionError("boom")

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        raise ConnectionError("boom")

    def build_key(self, *parts: str) -> str:
        return ":".join(parts)


@pytest.mark.asyncio
async def test_stats_roundtrips_through_the_cache_and_skips_the_database() -> None:
    first = _StubUow()
    cache = InMemoryCache()
    service = ReviewService(first, cache=cache, stats_cache_ttl_seconds=30)

    view = await service.stats(1)
    assert view is not None
    assert view.reviewed_total == 10
    assert first.review_cards.calls == 1
    assert first.review_logs.calls == 1

    # A second open hits the cache: the two aggregates are not re-read.
    second = _StubUow()
    cached_service = ReviewService(second, cache=cache, stats_cache_ttl_seconds=30)
    assert await cached_service.stats(1) == ReviewStatsView(
        total_cards=1,
        due_today=0,
        future=1,
        reviewed_total=10,
        retention_percent=90,
    )
    assert second.review_cards.calls == 0
    assert second.review_logs.calls == 0


@pytest.mark.asyncio
async def test_invalidation_rebuilds_from_the_uow() -> None:
    uow = _StubUow()
    cache = InMemoryCache()
    service = ReviewService(uow, cache=cache, stats_cache_ttl_seconds=30)

    await service.stats(7)
    await invalidate_review_stats(cache, 7)

    assert await service.stats(7) is not None
    assert uow.review_cards.calls == 2
    assert uow.review_logs.calls == 2


@pytest.mark.asyncio
async def test_cache_outage_falls_through_to_the_uow() -> None:
    uow = _StubUow()
    service = ReviewService(uow, cache=_BrokenCache())  # type: ignore[arg-type]

    assert (await service.stats(5)).reviewed_total == 10
    assert uow.review_cards.calls == 1


def test_encode_decode_roundtrip() -> None:
    view = ReviewStatsView(
        total_cards=3,
        due_today=1,
        future=2,
        reviewed_total=42,
        retention_percent=76,
    )
    assert decode_stats(encode_stats(view)) == view


def test_key_is_namespaced_per_user() -> None:
    cache = InMemoryCache()
    assert review_stats_key(cache, 9) == cache.build_key("review", "stats", "9")
