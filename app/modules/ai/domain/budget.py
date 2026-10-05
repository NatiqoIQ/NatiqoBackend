"""AI budget policy (domain).

Cost control is a domain rule, not infrastructure: "a free user may not spend more
than X per day" must hold regardless of which provider is plugged in. The port lives
here so :mod:`app.platform.ai.service` can enforce it without importing a repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.core.types import UserId
from app.platform.ai.port import AIFeature


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    allowed: bool
    spent_usd: Decimal
    limit_usd: Decimal
    reason: str | None = None

    @property
    def remaining_usd(self) -> Decimal:
        return max(self.limit_usd - self.spent_usd, Decimal("0"))


@runtime_checkable
class BudgetPort(Protocol):
    """Reads current spend. The write side is the usage recorder."""

    async def status(self, user_id: UserId, *, feature: AIFeature) -> BudgetStatus: ...


class UnmeteredBudget:
    """No limit. Used when AI is a paid-only feature or in development."""

    async def status(self, user_id: UserId, *, feature: AIFeature) -> BudgetStatus:
        return BudgetStatus(allowed=True, spent_usd=Decimal("0"), limit_usd=Decimal("0"))
