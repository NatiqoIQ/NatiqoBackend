"""Sentinel construction for the cache and queue clients.

The adapters never import a concrete network client until a classmethod runs, so these
tests swap ``redis.asyncio.sentinel.Sentinel`` for a fake recorder and verify that the
composition root's configuration reaches the library as *resolve the current master on
each connect* — which is the whole point of Sentinel during a failover.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from app.platform.cache.redis import RedisCache
from app.platform.queue.redis import RedisQueue


class _FakeSentinel:
    instances: ClassVar[list[dict[str, Any]]] = []
    masters: ClassVar[list[dict[str, Any]]] = []

    def __init__(
        self,
        sentinels: list[tuple[str, int]] | list[str],
        *,
        socket_timeout: float | None = None,
        **kwargs: Any,
    ) -> None:
        _FakeSentinel.instances.append(
            {"nodes": list(sentinels), "socket_timeout": socket_timeout, **kwargs}
        )

    def master_for(self, service_name: str, *, socket_timeout: float | None = None) -> object:
        client = object()
        _FakeSentinel.masters.append(
            {"name": service_name, "socket_timeout": socket_timeout, "client": client}
        )
        return client


@pytest.fixture(autouse=True)
def _reset_recorder() -> Any:
    _FakeSentinel.instances.clear()
    _FakeSentinel.masters.clear()
    yield
    _FakeSentinel.instances.clear()
    _FakeSentinel.masters.clear()


@pytest.fixture()
def fake_sentinel(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSentinel]:
    monkeypatch.setattr("redis.asyncio.sentinel.Sentinel", _FakeSentinel)
    return _FakeSentinel


def test_cache_sentinel_resolves_master(fake_sentinel: type[_FakeSentinel]) -> None:
    cache = RedisCache.from_sentinel(
        ["10.0.0.1:26379", "10.0.0.2:26379"],
        master="mymaster",
        socket_timeout=0.5,
    )

    assert cache._redis is fake_sentinel.masters[0]["client"]
    assert cache.build_key("review", "stats") == "natiqo:review:stats"
    assert fake_sentinel.instances[0]["socket_timeout"] == 0.5
    assert fake_sentinel.masters == [
        {"name": "mymaster", "socket_timeout": 0.5, "client": cache._redis}
    ]


def test_queue_sentinel_uses_queue_timeouts(fake_sentinel: type[_FakeSentinel]) -> None:
    queue = RedisQueue.from_sentinel(
        ["10.0.0.1:26379"],
        master="queue",
        socket_timeout=5.0,
        visibility_seconds=123,
    )

    assert queue._redis is fake_sentinel.masters[0]["client"]
    assert queue._visibility_seconds == 123
    assert fake_sentinel.instances[0]["socket_connect_timeout"] == 1.0
    assert fake_sentinel.masters == [
        {"name": "queue", "socket_timeout": 5.0, "client": queue._redis}
    ]
