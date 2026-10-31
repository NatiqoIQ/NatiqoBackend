"""Operational gauges added for the read replica and the AI breaker (P2-1/P2-4)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.core.metrics import render_prometheus
from app.platform.ai.service import _shared_breaker, reset_circuit_breakers


class _FakePool:
    def __init__(self, checked_out: int) -> None:
        self._checked_out = checked_out

    def checkedout(self) -> int:
        return self._checked_out

    def size(self) -> int:
        return 20

    def overflow(self) -> int:
        return 3


class _FakeEngine:
    pool = _FakePool(checked_out=7)


def test_pool_gauges_are_labelled_by_role(monkeypatch: pytest.MonkeyPatch) -> None:
    # SQLite uses StaticPool, which has no ``size()``; the production path is PostgreSQL.
    monkeypatch.setattr(database, "_engine", _FakeEngine())

    text = render_prometheus()

    assert 'natiqo_db_pool_checked_out{role="primary"} 7' in text
    assert 'natiqo_db_pool_size{role="primary"} 20' in text
    assert 'natiqo_db_pool_overflow{role="primary"} 3' in text


@pytest.mark.asyncio
async def test_ai_breaker_state_is_exported(session: AsyncSession) -> None:
    reset_circuit_breakers()
    assert "natiqo_ai_circuit_open 0" in render_prometheus()

    _shared_breaker().record_failure(threshold=1, now=0.0)

    assert "natiqo_ai_circuit_open 1" in render_prometheus()
    reset_circuit_breakers()
