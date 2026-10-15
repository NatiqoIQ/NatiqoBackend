"""Gamification use cases: the hub read, the achievements list, claiming, and the streak.

Two roles live here:

* **The read/mutation surface** behind ``/gamification``, ``/achievements`` and ``/streak``.
* **The lesson-completion subscriber** (:meth:`GamificationService.apply_lesson_completed`).
  It is the *only* writer of ``user_stats`` on the hot path, and it runs inside the
  completing request's transaction (registered by ``application/handlers.py``), so a lesson
  completion and the XP, streak and achievements it earned land together or not at all.

A learner who predates this phase has no ``user_stats`` row. Every read therefore *projects*
one from history when the row is missing — summing ``lesson_progress.xp_earned`` and running
the shared streak calendar over session completion times — so the hub never shows a zero the
learner did not earn, and ``/progress`` and ``/gamification`` agree. The projection is only
persisted when a mutation (a claim, a freeze) or the completion handler has a reason to write
it; a pure read leaves the database untouched, matching every other read slice.
"""

from __future__ import annotations

from datetime import datetime

from app.core.config import Settings
from app.core.errors import ConflictError, ErrorCode, NotFoundError, PremiumRequiredError
from app.core.time import local_date, streak_runs, utc_now
from app.core.types import UserId, new_id
from app.core.uow import UnitOfWorkProtocol
from app.modules.gamification.application.dto import (
    AchievementView,
    ClaimResultView,
    GamificationSummaryView,
    StreakView,
    UserStatsView,
)
from app.modules.gamification.domain.catalog import ACHIEVEMENTS
from app.modules.gamification.domain.entities import Achievement, UserAchievement, UserStats
from app.modules.gamification.domain.policy import (
    condition_metric,
    condition_target,
    is_condition_met,
    league_for_xp,
)
from app.modules.quiz.domain.entities import LessonProgress, ProgressState

__all__ = ["GamificationService"]


class GamificationService:
    def __init__(self, uow: UnitOfWorkProtocol, settings: Settings) -> None:
        self._uow = uow
        self._settings = settings

    # ================================================================ summary
    async def summary(self, user_id: UserId) -> GamificationSummaryView:
        """The hub screen: stats, lessons completed and the achievement tally, in one read."""
        stats, lessons_completed = await self._stats_and_lessons(user_id)
        await self._ensure_catalog()
        catalog = await self._uow.achievements.list_published()
        unlocked = await self._uow.user_achievements.list_for_user(user_id)
        return GamificationSummaryView(
            stats=self._stats_view(stats),
            lessons_completed=lessons_completed,
            achievements_unlocked=len(unlocked),
            achievements_total=len(catalog),
        )

    # =========================================================== achievements
    async def list_achievements(self, user_id: UserId) -> list[AchievementView]:
        """Every published achievement, each coloured by the learner's record against it.

        Locked entries are included — a catalogue is something to aim at — with their live
        progress, so a "3/7 day streak" badge can be drawn before it is earned.
        """
        stats, lessons_completed = await self._stats_and_lessons(user_id)
        await self._ensure_catalog()
        catalog = await self._uow.achievements.list_published()
        records = {
            record.achievement_code: record
            for record in await self._uow.user_achievements.list_for_user(user_id)
        }
        return [
            self._achievement_view(
                achievement, stats, lessons_completed, records.get(achievement.code)
            )
            for achievement in catalog
        ]

    async def claim(self, user_id: UserId, code: str) -> ClaimResultView:
        """Pay an unlocked achievement's reward once.

        Claiming is separate from unlocking because the reward is a deliberate tap, not a
        background grant: a learner sees the badge pop and chooses to collect. A second claim
        is refused rather than silently ignored, so a retrying client can tell success from
        a no-op.
        """
        await self._ensure_catalog()
        record = await self._uow.user_achievements.get(user_id, code)
        if record is None:
            raise NotFoundError(
                "That achievement has not been unlocked yet.",
                code=ErrorCode.ACHIEVEMENT_NOT_FOUND,
            )
        if record.is_claimed:
            raise ConflictError(
                "This achievement reward has already been claimed.",
                code=ErrorCode.ALREADY_CLAIMED,
            )
        achievement = await self._uow.achievements.get(code)
        if achievement is None:
            raise NotFoundError(
                "There is no such achievement.", code=ErrorCode.ACHIEVEMENT_NOT_FOUND
            )

        stats = await self._stats(user_id)
        if achievement.xp_reward > 0:
            stats.award_xp(achievement.xp_reward)
            stats.set_league(league_for_xp(stats.xp_total))
        if achievement.gems_reward > 0:
            stats.add_gems(achievement.gems_reward)
        await self._uow.user_stats.save(stats)

        record.claimed_at = utc_now()
        await self._uow.user_achievements.save(record)
        await self._uow.commit()

        lessons_completed = await self._lessons_completed(user_id)
        return ClaimResultView(
            achievement=self._achievement_view(achievement, stats, lessons_completed, record),
            xp_awarded=achievement.xp_reward,
            gems_awarded=achievement.gems_reward,
        )

    # ================================================================ streak
    async def streak(self, user_id: UserId) -> StreakView:
        """The streak detail screen. ``premium`` tells the client whether freezing is even
        on offer, so it can show the paywall before the learner taps."""
        stats = await self._stats(user_id)
        return self._streak_view(stats)

    async def freeze(self, user_id: UserId) -> StreakView:
        """Spend a streak freeze to protect the run.

        Gated behind the premium flag for this phase: real subscriptions arrive later, so a
        non-premium deployment answers ``PREMIUM_REQUIRED`` with a purchase link. When the
        flag is on and no freeze remains, the request is a conflict rather than a crash.
        """
        if not self._settings.premium_enabled:
            raise PremiumRequiredError(
                "Streak freezes are part of Premium.",
                details={"purchase_url": self._settings.premium_purchase_url},
            )
        stats = await self._stats(user_id)
        if not stats.consume_freeze():
            raise ConflictError(
                "You have no streak freezes left.", code=ErrorCode.NO_FREEZES_AVAILABLE
            )
        await self._uow.user_stats.save(stats)
        await self._uow.commit()
        return self._streak_view(stats)

    # ==================================================== event subscriber
    async def apply_lesson_completed(
        self, user_id: UserId, *, xp_awarded: int, on: datetime
    ) -> None:
        """Fold a completion into the projection, then unlock anything it earned.

        Called by the event handler inside the completing transaction. The streak advances
        on the learner's own calendar day; the XP added is the delta the completion earned,
        never a re-derived total, so a retry that scores more cannot double-count.
        """
        await self._ensure_catalog()
        timezone = await self._timezone(user_id)
        today = local_date(on, timezone)

        stats = await self._uow.user_stats.get(user_id)
        if stats is None:
            # No row yet: project from history. The completion that triggered this event is
            # already staged and flushed by the caller, so the projection *includes* it and
            # adding ``xp_awarded`` on top would count the same XP twice.
            stats = await self._project(user_id)
            await self._uow.user_stats.add(stats)
        else:
            stats.award_xp(xp_awarded)
            stats.set_league(league_for_xp(stats.xp_total))
            stats.record_activity(today)
            await self._uow.user_stats.save(stats)

        await self._unlock_achievements(user_id, stats)

    # ============================================================= internals
    async def _ensure_catalog(self) -> None:
        """Reconcile the ``achievements`` table with the code catalogue (idempotent)."""
        await self._uow.achievements.upsert_catalog(ACHIEVEMENTS)

    async def _stats_and_lessons(self, user_id: UserId) -> tuple[UserStats, int]:
        """The learner's stats and completed-lesson count, sharing one history read."""
        rows = await self._uow.lesson_progress.list_for_user(user_id)
        lessons_completed = _completed_count(rows)
        stats = await self._uow.user_stats.get(user_id)
        if stats is None:
            stats = await self._project_from_rows(user_id, rows)
        return stats, lessons_completed

    async def _stats(self, user_id: UserId) -> UserStats:
        stats = await self._uow.user_stats.get(user_id)
        if stats is not None:
            return stats
        return await self._project(user_id)

    async def _project(self, user_id: UserId) -> UserStats:
        rows = await self._uow.lesson_progress.list_for_user(user_id)
        return await self._project_from_rows(user_id, rows)

    async def _project_from_rows(self, user_id: UserId, rows: list[LessonProgress]) -> UserStats:
        """Build a stats row from durable history for a learner who has none yet."""
        times = await self._uow.quiz_sessions.list_completion_times(user_id)
        timezone = await self._timezone(user_id)
        days = [local_date(moment, timezone) for moment in times]
        current, longest = streak_runs(days, today=local_date(utc_now(), timezone))
        xp_total = sum(row.xp_earned for row in rows)
        stats = UserStats(
            user_id=user_id,
            xp_total=xp_total,
            current_streak=current,
            longest_streak=longest,
            last_activity_date=max(days) if days else None,
            freezes_available=self._settings.max_streak_freezes,
        )
        stats.set_league(league_for_xp(xp_total))
        return stats

    async def _lessons_completed(self, user_id: UserId) -> int:
        return _completed_count(await self._uow.lesson_progress.list_for_user(user_id))

    async def _unlock_achievements(self, user_id: UserId, stats: UserStats) -> None:
        lessons_completed = await self._lessons_completed(user_id)
        catalog = await self._uow.achievements.list_published()
        already = {
            record.achievement_code
            for record in await self._uow.user_achievements.list_for_user(user_id)
        }
        for achievement in catalog:
            if achievement.code in already:
                continue
            if not is_condition_met(
                achievement.condition,
                xp_total=stats.xp_total,
                longest_streak=stats.longest_streak,
                lessons_completed=lessons_completed,
            ):
                continue
            await self._uow.user_achievements.add(
                UserAchievement(
                    id=new_id(),
                    user_id=user_id,
                    achievement_code=achievement.code,
                    unlocked_at=utc_now(),
                    progress=_metric(achievement, stats, lessons_completed),
                )
            )

    async def _timezone(self, user_id: UserId) -> str | None:
        profile = await self._uow.users.get_profile(user_id)
        return profile.timezone if profile is not None else None

    # ------------------------------------------------------------- views
    def _stats_view(self, stats: UserStats) -> UserStatsView:
        return UserStatsView(
            xp_total=stats.xp_total,
            gems=stats.gems,
            current_streak=stats.current_streak,
            longest_streak=stats.longest_streak,
            league_tier=stats.league_tier.value,
            freezes_available=stats.freezes_available,
            last_activity_date=stats.last_activity_date,
        )

    def _streak_view(self, stats: UserStats) -> StreakView:
        return StreakView(
            current_streak=stats.current_streak,
            longest_streak=stats.longest_streak,
            last_activity_date=stats.last_activity_date,
            freezes_available=stats.freezes_available,
            premium=self._settings.premium_enabled,
        )

    def _achievement_view(
        self,
        achievement: Achievement,
        stats: UserStats,
        lessons_completed: int,
        record: UserAchievement | None,
    ) -> AchievementView:
        unlocked = record is not None
        return AchievementView(
            code=achievement.code,
            title_key=achievement.title_key,
            description_key=achievement.description_key,
            icon_key=achievement.icon_key,
            xp_reward=achievement.xp_reward,
            gems_reward=achievement.gems_reward,
            target=condition_target(achievement.condition),
            progress=_metric(achievement, stats, lessons_completed),
            unlocked=unlocked,
            unlocked_at=record.unlocked_at if record is not None else None,
            claimed_at=record.claimed_at if record is not None else None,
            claimable=unlocked and not (record.is_claimed if record is not None else True),
        )


# ------------------------------------------------------------------ helpers
def _completed_count(rows: list[LessonProgress]) -> int:
    return sum(1 for row in rows if row.state is ProgressState.COMPLETED)


def _metric(achievement: Achievement, stats: UserStats, lessons_completed: int) -> int:
    return condition_metric(
        achievement.condition,
        xp_total=stats.xp_total,
        longest_streak=stats.longest_streak,
        lessons_completed=lessons_completed,
    )
