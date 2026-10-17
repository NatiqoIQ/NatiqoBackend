"""SQLAlchemy implementations of the gamification ports.

``save`` follows the modules' insert-or-update pattern: it looks the row up by primary key
and either adds a new one or applies the entity's current values, so a caller never has to
know whether the row already existed. That matters here because the projection is built
lazily — a read that finds no ``user_stats`` row creates one and hands it straight to
``save``.

``upsert_catalog`` reconciles the ``achievements`` table with the code-defined catalogue.
It is idempotent and cheap (a handful of rows, checked once), so it can run on the read
path without an admin deploy to keep the table in step with ``domain/catalog.py``.

None of these methods commit. The unit of work owns the transaction, so a lesson completion
and the stats and achievements it unlocked land together or not at all.
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import UserId
from app.modules.gamification.domain.entities import (
    Achievement,
    LeagueTier,
    UserAchievement,
    UserStats,
)
from app.modules.gamification.infrastructure.models import (
    AchievementModel,
    UserAchievementModel,
    UserStatsModel,
)

_PUBLISHED = "published"


# ------------------------------------------------------------------- mappers
def _to_stats(model: UserStatsModel) -> UserStats:
    return UserStats(
        user_id=UserId(model.user_id),
        xp_total=model.xp_total,
        gems=model.gems,
        current_streak=model.current_streak,
        longest_streak=model.longest_streak,
        last_activity_date=model.last_activity_date,
        freezes_available=model.freezes_available,
        league_tier=LeagueTier(model.league_tier),
        league_updated_at=model.league_updated_at,
        updated_at=model.updated_at,
    )


def _to_stats_model(stats: UserStats) -> UserStatsModel:
    return UserStatsModel(
        user_id=stats.user_id,
        xp_total=stats.xp_total,
        gems=stats.gems,
        current_streak=stats.current_streak,
        longest_streak=stats.longest_streak,
        last_activity_date=stats.last_activity_date,
        freezes_available=stats.freezes_available,
        league_tier=stats.league_tier.value,
        league_updated_at=stats.league_updated_at,
    )


def _apply_stats(stats: UserStats, model: UserStatsModel) -> None:
    model.xp_total = stats.xp_total
    model.gems = stats.gems
    model.current_streak = stats.current_streak
    model.longest_streak = stats.longest_streak
    model.last_activity_date = stats.last_activity_date
    model.freezes_available = stats.freezes_available
    model.league_tier = stats.league_tier.value
    model.league_updated_at = stats.league_updated_at


def _to_achievement(model: AchievementModel) -> Achievement:
    return Achievement(
        code=model.code,
        title_key=model.title_key,
        description_key=model.description_key,
        icon_key=model.icon_key,
        condition=dict(model.condition or {}),
        xp_reward=model.xp_reward,
        gems_reward=model.gems_reward,
        sort_order=model.sort_order,
    )


def _to_achievement_model(achievement: Achievement) -> AchievementModel:
    return AchievementModel(
        code=achievement.code,
        title_key=achievement.title_key,
        description_key=achievement.description_key,
        icon_key=achievement.icon_key,
        condition=dict(achievement.condition),
        xp_reward=achievement.xp_reward,
        gems_reward=achievement.gems_reward,
        sort_order=achievement.sort_order,
        state=_PUBLISHED,
    )


def _to_user_achievement(model: UserAchievementModel) -> UserAchievement:
    return UserAchievement(
        id=model.id,
        user_id=UserId(model.user_id),
        achievement_code=model.achievement_code,
        unlocked_at=model.unlocked_at,
        progress=model.progress,
        claimed_at=model.claimed_at,
    )


def _to_user_achievement_model(record: UserAchievement) -> UserAchievementModel:
    return UserAchievementModel(
        id=record.id,
        user_id=record.user_id,
        achievement_code=record.achievement_code,
        unlocked_at=record.unlocked_at,
        progress=record.progress,
        claimed_at=record.claimed_at,
    )


def _apply_user_achievement(record: UserAchievement, model: UserAchievementModel) -> None:
    model.progress = record.progress
    model.claimed_at = record.claimed_at


# ------------------------------------------------------------------ storage
class SqlAlchemyUserStatsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UserId) -> UserStats | None:
        model = await self._session.get(UserStatsModel, user_id)
        return _to_stats(model) if model is not None else None

    async def add(self, stats: UserStats) -> None:
        self._session.add(_to_stats_model(stats))

    async def save(self, stats: UserStats) -> None:
        model = await self._session.get(UserStatsModel, stats.user_id)
        if model is None:
            self._session.add(_to_stats_model(stats))
            return
        _apply_stats(stats, model)


class SqlAlchemyAchievementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, code: str) -> Achievement | None:
        model = await self._session.get(AchievementModel, code)
        return _to_achievement(model) if model is not None else None

    async def list_published(self) -> list[Achievement]:
        stmt = (
            select(AchievementModel)
            .where(AchievementModel.state == _PUBLISHED)
            .order_by(AchievementModel.sort_order, AchievementModel.code)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [_to_achievement(model) for model in models]

    async def upsert_catalog(self, achievements: tuple[Achievement, ...]) -> None:
        existing = set((await self._session.execute(select(AchievementModel.code))).scalars().all())
        for achievement in achievements:
            if achievement.code in existing:
                # Targeted UPDATE: the seeds of the catalogue can change between releases
                # (a reward rebalanced, a description reworded), and reconciling the row
                # keeps the table honest without a delete-and-reinsert that would cascade
                # away learners' unlock records.
                await self._session.execute(
                    update(AchievementModel)
                    .where(AchievementModel.code == achievement.code)
                    .values(
                        title_key=achievement.title_key,
                        description_key=achievement.description_key,
                        icon_key=achievement.icon_key,
                        condition=dict(achievement.condition),
                        xp_reward=achievement.xp_reward,
                        gems_reward=achievement.gems_reward,
                        sort_order=achievement.sort_order,
                    )
                )
            else:
                self._session.add(_to_achievement_model(achievement))


class SqlAlchemyUserAchievementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UserId, code: str) -> UserAchievement | None:
        stmt = select(UserAchievementModel).where(
            UserAchievementModel.user_id == user_id,
            UserAchievementModel.achievement_code == code,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_user_achievement(model) if model is not None else None

    async def list_for_user(self, user_id: UserId) -> list[UserAchievement]:
        stmt = (
            select(UserAchievementModel)
            .where(UserAchievementModel.user_id == user_id)
            .order_by(UserAchievementModel.unlocked_at, UserAchievementModel.id)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [_to_user_achievement(model) for model in models]

    async def add(self, record: UserAchievement) -> None:
        self._session.add(_to_user_achievement_model(record))

    async def save(self, record: UserAchievement) -> None:
        model = await self._session.get(UserAchievementModel, record.id)
        if model is None:
            self._session.add(_to_user_achievement_model(record))
            return
        _apply_user_achievement(record, model)


__all__ = [
    "SqlAlchemyAchievementRepository",
    "SqlAlchemyUserAchievementRepository",
    "SqlAlchemyUserStatsRepository",
]
