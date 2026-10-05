"""SQLAlchemy implementation of :class:`BudgetPort`.

Spend is summed from the append-only ``ai_usage_records`` table for the user's local
calendar day. Sums over an indexed timestamp column are cheap at this table size, and
keeping the read here avoids a denormalised counter that could drift out of sync with
the authoritative usage rows.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.time import local_day_bounds
from app.core.types import UserId
from app.modules.ai.domain.budget import BudgetPort, BudgetStatus, UnmeteredBudget
from app.modules.ai.infrastructure.models import AiUsageRecord
from app.platform.ai.port import AIFeature

#: Resolves the IANA timezone a user's day starts in. Injected rather than hard-coded
#: so the budget adapter does not need to know the users module exists.
TimezoneResolver = Callable[[UserId], Awaitable[str | None]]


async def _profile_timezone(session: AsyncSession, user_id: UserId) -> str | None:
    from app.modules.users.infrastructure.models import UserProfileModel

    return await session.scalar(
        select(UserProfileModel.timezone).where(UserProfileModel.user_id == user_id)
    )


class SqlAlchemyBudget(BudgetPort):
    def __init__(
        self,
        session: AsyncSession,
        *,
        daily_limit_usd: Decimal,
        timezone_resolver: TimezoneResolver | None = None,
    ) -> None:
        self._session = session
        self._daily_limit_usd = daily_limit_usd
        self._timezone_resolver = timezone_resolver

    async def _day_bounds(self, user_id: UserId) -> tuple[Any, Any]:
        """Bounds of the caller's own calendar day.

        Falling back to ``None`` (i.e. UTC) rather than a hard-coded city keeps a user
        with a bad timezone row on a real budget instead of an accidentally unmetered
        or permanently-exhausted one.
        """
        timezone = None
        if self._timezone_resolver is not None:
            timezone = await self._timezone_resolver(user_id)
        return local_day_bounds(timezone)

    async def status(self, user_id: UserId, *, feature: AIFeature) -> BudgetStatus:
        start, end = await self._day_bounds(user_id)
        spent = await self._session.scalar(
            select(func.coalesce(func.sum(AiUsageRecord.cost_usd), 0)).where(
                AiUsageRecord.user_id == user_id,
                AiUsageRecord.created_at >= start,
                AiUsageRecord.created_at < end,
            )
        )
        total = Decimal(str(spent or 0))
        if self._daily_limit_usd <= 0:
            return BudgetStatus(allowed=True, spent_usd=total, limit_usd=Decimal("0"))
        if total >= self._daily_limit_usd:
            return BudgetStatus(
                allowed=False,
                spent_usd=total,
                limit_usd=self._daily_limit_usd,
                reason=f"Daily AI budget reached ({feature}). It resets tomorrow.",
            )
        return BudgetStatus(allowed=True, spent_usd=total, limit_usd=self._daily_limit_usd)


def budget_for(session: AsyncSession, settings: Settings) -> BudgetPort:
    limit = Decimal(str(settings.ai_daily_user_budget_usd))
    if limit <= 0:
        return UnmeteredBudget()

    async def resolver(user_id: UserId) -> str | None:
        return await _profile_timezone(session, user_id)

    return SqlAlchemyBudget(session, daily_limit_usd=limit, timezone_resolver=resolver)
