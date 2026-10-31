"""Cache hit ratio must be a measured number, not an assumption.

The Phase-1 exit criterion is a catalogue cache hit ratio above 90%. These tests pin the
two halves of that signal: the adapters count every read (including a failed Redis read,
which is a miss in effect), and ``/metrics`` renders bounded labels rather than keys.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.metrics import render_prometheus, reset_metrics
from app.platform.cache import stats
from app.platform.cache.memory import InMemoryCache
from app.platform.cache.redis import RedisCache


@pytest.fixture(autouse=True)
def _clean_registries() -> Iterator[None]:
    stats.reset()
    reset_metrics()
    yield
    stats.reset()
    reset_metrics()


def test_the_label_is_the_area_not_the_key() -> None:
    assert stats.area_of("natiqo:catalogue:tree:v1") == "catalogue"
    assert stats.area_of("natiqo:progress:summary:42") == "progress"
    # An unnamespaced key still gets a bounded first segment.
    assert stats.area_of("custom:key") == "custom"
    assert stats.area_of("") == "other"


@pytest.mark.asyncio
async def test_the_memory_adapter_counts_hits_and_misses() -> None:
    cache = InMemoryCache()
    key = cache.build_key("catalogue", "tree", "v1")

    assert await cache.get(key) is None
    await cache.set(key, "{}")
    assert await cache.get(key) == "{}"

    assert stats.snapshot() == {("catalogue", "miss"): 1, ("catalogue", "hit"): 1}


def test_metrics_exposes_the_counters_with_bounded_labels() -> None:
    stats.record("natiqo:catalogue:tree:v1", hit=True)
    stats.record("natiqo:catalogue:tree:v1", hit=True)
    stats.record("natiqo:progress:summary:42", hit=False)

    body = render_prometheus()

    assert "# TYPE natiqo_cache_requests_total counter" in body
    assert 'natiqo_cache_requests_total{area="catalogue",result="hit"} 2' in body
    assert 'natiqo_cache_requests_total{area="progress",result="miss"} 1' in body
    # The learner id must never become a label value.
    assert "summary:42" not in body


class _BrokenRedis:
    async def get(self, key: str) -> str:
        raise ConnectionError("redis is down")


@pytest.mark.asyncio
async def test_a_failed_redis_read_is_counted_as_a_miss_before_it_propagates() -> None:
    """The caller falls through to PostgreSQL; the ratio must reflect that fall-through.

    Swallowing the error here would hide a real outage from the adapter's contract, so
    the exception still propagates. But a request served by the database was not a cache
    hit, and counting it as anything else would make an outage look like good caching.
    """
    cache = RedisCache(_BrokenRedis())

    with pytest.raises(ConnectionError):
        await cache.get("natiqo:catalogue:tree:v1")

    assert stats.snapshot() == {("catalogue", "miss"): 1}
