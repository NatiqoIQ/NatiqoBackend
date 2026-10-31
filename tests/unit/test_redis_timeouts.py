"""Redis clients must fail fast enough for the fallback to exist.

F32 made Redis non-fatal for readiness, but a client without socket timeouts can still
hold a request open until the kernel gives up. The cache is optional, so a slow cache
must turn into a miss in well under a second; the queue's read is blocking by design,
so its timeout must exceed the block interval instead.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from app.platform.cache.redis import RedisCache
from app.platform.queue.redis import RedisQueue


class _FakeRedis:
    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> _FakeRedis:
        cls.last_url = url  # type: ignore[attr-defined]
        cls.last_kwargs = kwargs  # type: ignore[attr-defined]
        return cls()

    async def aclose(self) -> None:
        return None


def test_the_cache_client_has_a_sub_second_socket_timeout() -> None:
    with patch("redis.asyncio.Redis", _FakeRedis):
        RedisCache.from_url("redis://cache:6379/0", socket_timeout=0.25)

    assert _FakeRedis.last_kwargs["socket_timeout"] == 0.25  # type: ignore[attr-defined]
    assert _FakeRedis.last_kwargs["socket_connect_timeout"] == 0.25  # type: ignore[attr-defined]
    assert _FakeRedis.last_kwargs["health_check_interval"] == 30  # type: ignore[attr-defined]


def test_the_queue_timeout_outlives_its_blocking_read() -> None:
    with patch("redis.asyncio.Redis", _FakeRedis):
        RedisQueue.from_url("redis://queue:6379/0", socket_timeout=5.0)

    kwargs = _FakeRedis.last_kwargs  # type: ignore[attr-defined]
    assert kwargs["socket_timeout"] == 5.0
    # The connect timeout is bounded independently; a stalled handshake should not
    # inherit the five-second read budget.
    assert kwargs["socket_connect_timeout"] == 1.0
