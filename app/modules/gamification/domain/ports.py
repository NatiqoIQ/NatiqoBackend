"""Repository ports for the gamification module.

Application code depends on these, never on the SQLAlchemy classes. They are expressed as
``Protocol`` so the concrete repositories only have to be structurally correct — the same
approach the rest of the modules take.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.types import UserId
from app.modules.gamification.domain.entities import Achievement, UserAchievement, UserStats

__all__ = [
    "AchievementRepositoryPort",
    "UserAchievementRepositoryPort",
    "UserStatsRepositoryPort",
]


@runtime_checkable
class UserStatsRepositoryPort(Protocol):
    async def get(self, user_id: UserId) -> UserStats | None: ...
    async def add(self, stats: UserStats) -> None: ...
    async def save(self, stats: UserStats) -> None: ...


@runtime_checkable
class AchievementRepositoryPort(Protocol):
    async def list_published(self) -> list[Achievement]: ...
    async def get(self, code: str) -> Achievement | None: ...
    async def upsert_catalog(self, achievements: tuple[Achievement, ...]) -> None: ...


@runtime_checkable
class UserAchievementRepositoryPort(Protocol):
    async def list_for_user(self, user_id: UserId) -> list[UserAchievement]: ...
    async def get(self, user_id: UserId, code: str) -> UserAchievement | None: ...
    async def add(self, record: UserAchievement) -> None: ...
    async def save(self, record: UserAchievement) -> None: ...
