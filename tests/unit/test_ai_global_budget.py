"""The platform-wide AI budget (P2-4).

A per-user limit cannot stop a runaway prompt, a retry storm or ten thousand users each
spending their own allowance. This cap is the backstop; the tests pin that it denies
before the provider is called, that a cached figure does not become a query per request,
and that a broken cache does not disable the guard.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.core.config import Settings
from app.core.types import UserId
from app.modules.ai.domain.budget import BudgetPort, BudgetStatus
from app.modules.ai.infrastructure.budget import GlobalBudget, budget_for
from app.platform.ai.port import AIFeature
from app.platform.cache.memory import InMemoryCache

_USER = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
_FEATURE = AIFeature.TUTOR_CHAT


def _settings(**overrides: Any) -> Settings:
    values = {"ai_daily_user_budget_usd": 0.5, "ai_daily_global_budget_usd": 5.0}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class _Session:
    """Carries only ``scalar``: the adapter never touches anything else."""

    def __init__(self, spent: Decimal) -> None:
        self.spent = spent
        self.calls = 0

    async def scalar(self, statement: Any) -> Decimal | None:
        # The timezone lookup and the spend sum share this seam; only the sum counts.
        if "sum" not in str(statement).lower():
            return None
        self.calls += 1
        return self.spent


class _DenyingBudget(BudgetPort):
    async def status(self, user_id: UserId, *, feature: AIFeature) -> BudgetStatus:
        return BudgetStatus(
            allowed=False,
            spent_usd=Decimal("1"),
            limit_usd=Decimal("1"),
            reason="user budget",
        )


class _BrokenCache(InMemoryCache):
    async def get(self, key: str) -> str | None:
        raise RuntimeError("cache down")

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        raise RuntimeError("cache down")


@pytest.mark.asyncio
async def test_spend_below_the_ceiling_is_allowed() -> None:
    session = _Session(Decimal("3.5"))
    budget = budget_for(session, _settings(ai_daily_user_budget_usd=0.0), cache=InMemoryCache())  # type: ignore[arg-type]
    status = await budget.status(_USER, feature=_FEATURE)
    assert status.allowed is True


@pytest.mark.asyncio
async def test_spend_at_the_ceiling_is_denied_with_a_clear_reason() -> None:
    session = _Session(Decimal("5"))
    budget = budget_for(session, _settings(ai_daily_user_budget_usd=0.0), cache=InMemoryCache())  # type: ignore[arg-type]
    status = await budget.status(_USER, feature=_FEATURE)
    assert status.allowed is False
    assert "platform" in (status.reason or "")


@pytest.mark.asyncio
async def test_the_spend_figure_is_cached_between_requests() -> None:
    session = _Session(Decimal("1"))
    budget = budget_for(
        session,
        _settings(ai_daily_user_budget_usd=0.0),  # isolate the global path
        cache=InMemoryCache(),
    )  # type: ignore[arg-type]

    await budget.status(_USER, feature=_FEATURE)
    await budget.status(_USER, feature=_FEATURE)

    assert session.calls == 1


@pytest.mark.asyncio
async def test_a_broken_cache_does_not_disable_the_cap() -> None:
    session = _Session(Decimal("9"))
    budget = budget_for(
        session,
        _settings(ai_daily_user_budget_usd=0.0),
        cache=_BrokenCache(),
    )  # type: ignore[arg-type]
    status = await budget.status(_USER, feature=_FEATURE)
    assert status.allowed is False
    assert session.calls == 1


@pytest.mark.asyncio
async def test_a_user_who_is_already_over_budget_skips_the_global_query() -> None:
    session = _Session(Decimal("0"))
    budget = GlobalBudget(
        _DenyingBudget(),
        session,  # type: ignore[arg-type]
        daily_limit_usd=Decimal("5"),
        cache=InMemoryCache(),
    )

    status = await budget.status(_USER, feature=_FEATURE)

    assert status.allowed is False
    assert status.reason == "user budget"
    assert session.calls == 0


@pytest.mark.asyncio
async def test_zero_global_limit_does_not_wrap_the_budget() -> None:
    session = _Session(Decimal("100"))
    budget = budget_for(
        session,
        _settings(ai_daily_user_budget_usd=0.0, ai_daily_global_budget_usd=0.0),
        cache=InMemoryCache(),
    )  # type: ignore[arg-type]
    assert not isinstance(budget, GlobalBudget)
    assert (await budget.status(_USER, feature=_FEATURE)).allowed is True
