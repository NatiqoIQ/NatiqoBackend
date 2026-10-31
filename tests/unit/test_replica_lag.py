"""Replica lag probe and fallback (P2-1).

The replica serves reads that tolerate a second of delay. It must stop serving them when
it is minutes behind — a missing lesson is worse than a slower read — and an unreachable
replica must be treated as maximally lagging, never as caught up.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core import database
from app.core.metrics import render_prometheus


class _Conn:
    def __init__(self, engine: _Engine) -> None:
        self._engine = engine

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def scalar(self, statement: Any) -> Any:
        if self._engine.fail:
            raise RuntimeError("replica unreachable")
        return self._engine.value


class _Engine:
    def __init__(self, value: float | None = 0.0, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail
        self.dialect = SimpleNamespace(name="postgresql")

    def connect(self) -> _Conn:
        return _Conn(self)


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "database_read_url": "postgresql+asyncpg://replica/natiqo",
        "read_sqlalchemy_dsn": "postgresql+asyncpg://replica/natiqo",
        "sqlalchemy_dsn": "postgresql+asyncpg://primary/natiqo",
        "replica_lag_cache_seconds": 5.0,
        "replica_max_lag_seconds": 5.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _reset() -> None:
    database.reset_replica_lag_cache()


def _use(monkeypatch: pytest.MonkeyPatch, engine: _Engine, **settings: Any) -> _Engine:
    monkeypatch.setattr(database, "get_settings", lambda: _settings(**settings))
    monkeypatch.setattr(database, "get_read_engine", lambda: engine)
    return engine


@pytest.mark.asyncio
async def test_no_replica_means_no_lag_and_no_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _Engine(), database_read_url=None, read_sqlalchemy_dsn="primary")
    monkeypatch.setattr(
        database, "get_read_engine", lambda: pytest.fail("must not probe without a replica")
    )

    assert await database.replica_lag_seconds() == 0.0
    assert await database.replica_is_lagging() is False


@pytest.mark.asyncio
async def test_a_lagging_replica_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _Engine(12.5))

    assert await database.replica_lag_seconds() == 12.5
    assert await database.replica_is_lagging() is True


@pytest.mark.asyncio
async def test_a_null_replay_timestamp_is_zero_lag(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _Engine(None))

    assert await database.replica_lag_seconds() == 0.0


@pytest.mark.asyncio
async def test_the_probe_is_cached_until_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _use(monkeypatch, _Engine(12.5), replica_lag_cache_seconds=60.0)

    assert await database.replica_lag_seconds() == 12.5
    engine.value = 0.0
    assert await database.replica_lag_seconds() == 12.5, "the cached answer must be reused"
    assert await database.replica_lag_seconds(force=True) == 0.0


@pytest.mark.asyncio
async def test_an_unreachable_replica_counts_as_lagging(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _Engine(fail=True))

    assert await database.replica_lag_seconds() == float("inf")
    assert await database.replica_is_lagging() is True


def test_the_gauge_exports_the_cached_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database, "_lag_seconds", 3.25)

    assert "natiqo_replica_lag_seconds 3.250" in render_prometheus()
