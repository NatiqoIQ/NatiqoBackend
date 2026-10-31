"""Two completions for one learner must not lose an XP award.

XP is a read-modify-write on a single ``user_stats`` row. Two devices finishing two
lessons at the same moment both read the old total; without a lock the later write wins
and the earlier award disappears — silently, because every response looked successful.
The service must read the row under ``FOR UPDATE`` and, when two *first-ever*
completions race to insert it, add its own delta on top of the winner rather than
surface an IntegrityError.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.core.types import UserId
from app.modules.gamification.application.service import GamificationService
from app.modules.gamification.domain.entities import UserStats


class _Stats:
    """A stats repository that loses the first-ever insert race exactly once."""

    def __init__(self, *, winner_xp: int) -> None:
        self._winner_xp = winner_xp
        self.reads = 0
        self.saved: UserStats | None = None

    async def get_for_update(self, user_id: UserId) -> UserStats | None:
        self.reads += 1
        return None if self.reads == 1 else UserStats(user_id=user_id, xp_total=self._winner_xp)

    async def add(self, stats: UserStats) -> bool:
        return False

    async def save(self, stats: UserStats) -> None:
        self.saved = stats

    async def get(self, user_id: UserId) -> UserStats | None:  # pragma: no cover - must not be used
        raise AssertionError("the completion path must take the row lock")


class _Empty:
    async def list_for_user(self, user_id: UserId) -> list[object]:
        return []

    async def list_completion_times(self, user_id: UserId) -> list[datetime]:
        return []

    async def get_profile(self, user_id: UserId) -> None:
        return None

    async def upsert_catalog(self, achievements: object) -> None:
        return None

    async def list_published(self) -> list[object]:
        return []


class _Uow:
    def __init__(self, stats: _Stats) -> None:
        self.user_stats = stats
        self.lesson_progress = _Empty()
        self.quiz_sessions = _Empty()
        self.users = _Empty()
        self.achievements = _Empty()
        self.user_achievements = _Empty()


@pytest.mark.asyncio
async def test_a_lost_first_insert_keeps_both_awards() -> None:
    user_id = UserId(uuid.UUID("11111111-1111-1111-1111-111111111111"))
    stats = _Stats(winner_xp=50)
    service = GamificationService(_Uow(stats), get_settings())  # type: ignore[arg-type]

    await service.apply_lesson_completed(
        user_id,
        xp_awarded=10,
        on=datetime(2026, 10, 31, 9, 0, tzinfo=UTC),
    )

    assert stats.reads == 2, "the loser must re-read the winner's committed row"
    assert stats.saved is not None
    assert stats.saved.xp_total == 60, "50 from the winner + 10 from this completion"
