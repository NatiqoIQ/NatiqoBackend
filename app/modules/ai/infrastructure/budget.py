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
from app.core.logging import get_logger
from app.core.time import local_day_bounds, utc_now
from app.core.types import UserId
from app.modules.ai.domain.budget import BudgetPort, BudgetStatus, UnmeteredBudget
from app.modules.ai.infrastructure.models import AiUsageRecord
from app.platform.ai.port import AIFeature
from app.platform.cache.port import CachePort

logger = get_logger("ai.budget")

#: How long a platform-wide spend figure is reused. The cap is a safety valve, not an
#: accounting ledger: a minute of overshoot is acceptable, a SUM over today's usage rows
#: on every AI request is not.
_GLOBAL_SPEND_TTL_SECONDS = 60

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


class GlobalBudget(BudgetPort):
    """Apply a platform-wide daily ceiling on top of the per-user budget.

    The spend total is cached for a minute. Two AI replicas may each see a slightly stale
    figure, which at most lets the platform overspend by one polling window — a far better
    failure than making every AI request pay for a ``SUM`` over the day's usage rows.
    """

    def __init__(
        self,
        inner: BudgetPort,
        session: AsyncSession,
        *,
        daily_limit_usd: Decimal,
        cache: CachePort,
        ttl_seconds: int = _GLOBAL_SPEND_TTL_SECONDS,
    ) -> None:
        self._inner = inner
        self._session = session
        self._daily_limit_usd = daily_limit_usd
        self._cache = cache
        self._ttl_seconds = ttl_seconds

    async def _spent_today(self) -> Decimal:
        today = utc_now().date().isoformat()
        key = self._cache.build_key("ai", "spend", "global", today)
        try:
            cached = await self._cache.get(key)
        except Exception as exc:  # noqa: BLE001 - a cache outage must not disable the cap
            logger.warning("ai.global_budget_cache_read_failed", error=str(exc)[:200])
            cached = None
        if cached is not None:
            try:
                return Decimal(cached)
            except ArithmeticError:
                logger.warning("ai.global_budget_cache_corrupt", value=cached[:40])

        start, _end = local_day_bounds("UTC")
        spent = await self._session.scalar(
            select(func.coalesce(func.sum(AiUsageRecord.cost_usd), 0)).where(
                AiUsageRecord.created_at >= start,
            )
        )
        total = Decimal(str(spent or 0))
        try:
            await self._cache.set(key, str(total), ttl_seconds=self._ttl_seconds)
        except Exception as exc:  # noqa: BLE001 - a failed write is a cache miss next time
            logger.warning("ai.global_budget_cache_write_failed", error=str(exc)[:200])
        return total

    async def status(self, user_id: UserId, *, feature: AIFeature) -> BudgetStatus:
        local = await self._inner.status(user_id, feature=feature)
        if not local.allowed:
            return local
        spent = await self._spent_today()
        if spent >= self._daily_limit_usd:
            logger.warning("ai.global_budget_exceeded", spent_usd=str(spent))
            return BudgetStatus(
                allowed=False,
                spent_usd=spent,
                limit_usd=self._daily_limit_usd,
                reason="The platform's daily AI budget is exhausted. Core lessons are unaffected.",
            )
        return local


def budget_for(
    session: AsyncSession,
    settings: Settings,
    *,
    cache: CachePort | None = None,
) -> BudgetPort:
    user_limit = Decimal(str(settings.ai_daily_user_budget_usd))
    global_limit = Decimal(str(settings.ai_daily_global_budget_usd))
    if user_limit <= 0 and global_limit <= 0:
        return UnmeteredBudget()

    inner: BudgetPort
    if user_limit <= 0:
        inner = UnmeteredBudget()
    else:

        async def resolver(user_id: UserId) -> str | None:
            return await _profile_timezone(session, user_id)

        inner = SqlAlchemyBudget(session, daily_limit_usd=user_limit, timezone_resolver=resolver)

    if global_limit > 0 and cache is not None:
        return GlobalBudget(inner, session, daily_limit_usd=global_limit, cache=cache)
    return inner
