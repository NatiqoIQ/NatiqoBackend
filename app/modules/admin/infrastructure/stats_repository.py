"""SQLAlchemy adapter for the overview's aggregate reads.

Nineteen queries, each a ``count``, a ``sum`` or a ``group by`` over a day — and not one
*table* loaded into Python. That is the whole point of the class: a dashboard that counted
by fetching would get slower every week the product ran, and its cost would grow with the
data rather than with the screen. The only rows that reach Python are the grouped day
buckets of the activity series, bounded by the window (at most one row per day per query)
because a chart has no aggregate that expresses "per day" without grouping.

Two definitions worth stating because nothing in the SQL says them out loud:

* **Active** means "signed in during the window", read from ``user_identities.last_login_at``
  and counted distinct, because one person may hold a Google and an Apple identity. It is the
  widest honest definition: a learner who opens the app and reads a lesson without finishing
  it is still active, and counting completed sessions would undercount them.
* **Accuracy** is weighted by answers, computed in the application layer from
  ``answers_graded`` and ``correct_answers`` here. Averaging per-session percentages would
  let a two-question sitting outweigh a twenty-question one.

The conditional sums use ``sum(case(...))`` rather than ``count(*) filter (where ...)``
because the latter is PostgreSQL-only and the suite runs on SQLite. The same reasoning
applies to ``date(...)`` for the day buckets: it is a function on both engines, and grouping
in SQL keeps a year of history from ever becoming a year of rows in the service.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import ContentState, QuestionKind, SkillKind, UserStatus
from app.modules.admin.domain.stats import (
    AdminOverview,
    AiTotals,
    DailyActivity,
    GamificationTotals,
    PerformanceTotals,
    QuestionCounts,
)
from app.modules.ai.infrastructure.models import AiConversationModel, AiUsageRecord
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)
from app.modules.gamification.infrastructure.models import (
    UserAchievementModel,
    UserStatsModel,
)
from app.modules.quiz.domain.entities import ProgressState, SessionStatus
from app.modules.quiz.domain.review import PASSING_QUALITY
from app.modules.quiz.infrastructure.models import (
    LessonProgressModel,
    QuizAnswerModel,
    QuizSessionModel,
)
from app.modules.quiz.infrastructure.review_models import ReviewLogModel
from app.modules.users.infrastructure.models import UserIdentityModel, UserModel

__all__ = ["SqlAlchemyAdminStatsRepository"]

#: The status the metering recorder writes for a call that succeeded. Every other value is
#: a failure, which is what ``errors`` counts.
_AI_OK = "ok"


def _int(value: Any) -> int:
    """A nullable aggregate as an int.

    ``sum`` over zero rows is ``NULL``, not ``0``, so every conditional sum is wrapped here
    once rather than at each use.
    """
    return int(value or 0)


def _float(value: Any) -> float:
    """A nullable numeric aggregate as a float — same ``NULL``-over-no-rows reasoning."""
    return float(value or 0)


def _day(value: Any) -> date:
    """A grouped day column as a ``date``.

    SQLite returns ``date(...)`` as text and PostgreSQL as a driver date, so the one place
    that has to care about the difference is here rather than at every call site.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class SqlAlchemyAdminStatsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def overview(self, *, since: datetime) -> AdminOverview:
        users = (
            await self._session.execute(
                select(
                    func.count(UserModel.id),
                    func.sum(case((UserModel.created_at >= since, 1), else_=0)),
                    func.sum(case((UserModel.status == UserStatus.DELETED.value, 1), else_=0)),
                )
            )
        ).one()

        active = await self._session.scalar(
            select(func.count(func.distinct(UserIdentityModel.user_id))).where(
                UserIdentityModel.last_login_at >= since
            )
        )

        sessions = (
            await self._session.execute(
                select(
                    func.sum(
                        case(
                            (QuizSessionModel.status == SessionStatus.COMPLETED.value, 1),
                            else_=0,
                        )
                    ),
                    func.sum(
                        case(
                            (QuizSessionModel.status == SessionStatus.ACTIVE.value, 1),
                            else_=0,
                        )
                    ),
                    func.sum(QuizSessionModel.xp_awarded),
                )
            )
        ).one()

        completed_lessons = await self._session.scalar(
            select(func.count(LessonProgressModel.id)).where(
                LessonProgressModel.state == ProgressState.COMPLETED.value
            )
        )

        answers = (
            await self._session.execute(
                select(
                    func.count(QuizAnswerModel.id),
                    func.sum(case((QuizAnswerModel.is_correct, 1), else_=0)),
                )
            )
        ).one()

        levels, level_drafts, level_review, _ = await self._content_counts(LevelModel)
        units, unit_drafts, unit_review, _ = await self._content_counts(UnitModel)
        lessons, lesson_drafts, lesson_review, lesson_published = await self._content_counts(
            LessonModel
        )
        questions = await self._question_counts()
        performance = await self._performance_totals()
        gamification = await self._gamification_totals()
        ai = await self._ai_totals()
        activity = await self._activity(since=since)

        return AdminOverview(
            users_total=_int(users[0]),
            users_new=_int(users[1]),
            users_active=_int(active),
            users_deleted=_int(users[2]),
            lessons_completed=_int(completed_lessons),
            sessions_completed=_int(sessions[0]),
            sessions_active=_int(sessions[1]),
            answers_graded=_int(answers[0]),
            correct_answers=_int(answers[1]),
            xp_awarded=_int(sessions[2]),
            levels=levels,
            units=units,
            lessons=lessons,
            published_lessons=lesson_published,
            # Drafts and reviews are summed across all three aggregates because the screen
            # shows one queue — "18 items await review" — not three separate ones.
            drafts=level_drafts + unit_drafts + lesson_drafts,
            in_review=level_review + unit_review + lesson_review,
            questions=questions,
            performance=performance,
            gamification=gamification,
            ai=ai,
            activity=activity,
        )

    async def _content_counts(self, model: Any) -> tuple[int, int, int, int]:
        """``(total, drafts, in_review, published)`` for one curriculum table."""
        state = model.state
        row = (
            await self._session.execute(
                select(
                    func.count(),
                    func.sum(case((state == ContentState.DRAFT.value, 1), else_=0)),
                    func.sum(case((state == ContentState.REVIEW.value, 1), else_=0)),
                    func.sum(case((state == ContentState.PUBLISHED.value, 1), else_=0)),
                )
            )
        ).one()
        return _int(row[0]), _int(row[1]), _int(row[2]), _int(row[3])

    async def _question_counts(self) -> QuestionCounts:
        """Question stock: size, live size, and the two breakdowns the charts show.

        One grouped query serves both breakdowns — ``(kind, skill)`` pairs are counted once
        and folded into each map, since every question has exactly one of each. The maps
        start zero-filled from the enums so a category nobody has authored yet still
        renders as ``0`` rather than as a missing key.
        """
        totals = (
            await self._session.execute(
                select(
                    func.count(QuestionModel.id),
                    func.sum(case((LessonModel.state == ContentState.PUBLISHED.value, 1), else_=0)),
                )
                .select_from(QuestionModel)
                .join(LessonModel, LessonModel.id == QuestionModel.lesson_id)
            )
        ).one()

        pairs = (
            await self._session.execute(
                select(QuestionModel.kind, QuestionModel.skill, func.count()).group_by(
                    QuestionModel.kind, QuestionModel.skill
                )
            )
        ).all()

        by_kind = {kind.value: 0 for kind in QuestionKind}
        by_skill = {skill.value: 0 for skill in SkillKind}
        for kind, skill, count in pairs:
            by_kind[kind] += _int(count)
            by_skill[skill] += _int(count)

        return QuestionCounts(
            total=_int(totals[0]),
            published=_int(totals[1]),
            by_kind=by_kind,
            by_skill=by_skill,
        )

    async def _performance_totals(self) -> PerformanceTotals:
        """Grades and review quality, as the sums the service divides."""
        progress = (
            await self._session.execute(
                select(
                    func.count(LessonProgressModel.id),
                    func.sum(LessonProgressModel.best_score),
                ).where(LessonProgressModel.state == ProgressState.COMPLETED.value)
            )
        ).one()

        reviews = (
            await self._session.execute(
                select(
                    func.count(ReviewLogModel.id),
                    func.sum(case((ReviewLogModel.quality >= PASSING_QUALITY, 1), else_=0)),
                    func.sum(ReviewLogModel.quality),
                )
            )
        ).one()

        return PerformanceTotals(
            rated_lessons=_int(progress[0]),
            score_total=_int(progress[1]),
            reviews_total=_int(reviews[0]),
            reviews_passed=_int(reviews[1]),
            quality_total=_int(reviews[2]),
        )

    async def _gamification_totals(self) -> GamificationTotals:
        """XP, live streaks and unlocks across every learner row."""
        stats = (
            await self._session.execute(
                select(
                    func.sum(UserStatsModel.xp_total),
                    func.sum(case((UserStatsModel.current_streak > 0, 1), else_=0)),
                    func.max(UserStatsModel.longest_streak),
                )
            )
        ).one()
        unlocks = await self._session.scalar(select(func.count(UserAchievementModel.id)))

        return GamificationTotals(
            xp_total=_int(stats[0]),
            active_streaks=_int(stats[1]),
            longest_streak=_int(stats[2]),
            achievements_unlocked=_int(unlocks),
        )

    async def _ai_totals(self) -> AiTotals:
        """Lifetime metering: what the AI features have cost, and how often they failed."""
        usage = (
            await self._session.execute(
                select(
                    func.count(AiUsageRecord.id),
                    func.sum(case((AiUsageRecord.status != _AI_OK, 1), else_=0)),
                    func.sum(AiUsageRecord.input_tokens + AiUsageRecord.output_tokens),
                    func.sum(AiUsageRecord.cost_usd),
                )
            )
        ).one()
        conversations = await self._session.scalar(select(func.count(AiConversationModel.id)))

        cost = usage[3]
        return AiTotals(
            requests=_int(usage[0]),
            errors=_int(usage[1]),
            tokens=_int(usage[2]),
            cost_usd=float(cost) if isinstance(cost, Decimal) else _float(cost),
            conversations=_int(conversations),
        )

    async def _activity(self, *, since: datetime) -> tuple[DailyActivity, ...]:
        """Days inside the window that saw *something*, ascending.

        Three grouped reads — accounts, completed sessions, metered AI calls — folded into
        one row per day that has data. Days with no data at all are absent here on purpose:
        filling them is the use case's job, because only it knows where the window starts
        and ends.
        """
        user_days = await self._grouped_day_counts(
            select(func.date(UserModel.created_at), func.count())
            .where(UserModel.created_at >= since)
            .group_by(func.date(UserModel.created_at))
        )

        session_rows = (
            await self._session.execute(
                select(
                    func.date(QuizSessionModel.completed_at),
                    func.count(),
                    func.sum(QuizSessionModel.xp_awarded),
                )
                .where(
                    QuizSessionModel.status == SessionStatus.COMPLETED.value,
                    QuizSessionModel.completed_at >= since,
                )
                .group_by(func.date(QuizSessionModel.completed_at))
            )
        ).all()
        session_days = {_day(day): (_int(count), _int(xp)) for day, count, xp in session_rows}

        ai_days = await self._grouped_day_counts(
            select(func.date(AiUsageRecord.created_at), func.count())
            .where(AiUsageRecord.created_at >= since)
            .group_by(func.date(AiUsageRecord.created_at))
        )

        days = sorted(user_days.keys() | session_days.keys() | ai_days.keys())
        return tuple(
            DailyActivity(
                day=day,
                new_users=user_days.get(day, 0),
                sessions_completed=session_days.get(day, (0, 0))[0],
                xp_awarded=session_days.get(day, (0, 0))[1],
                ai_requests=ai_days.get(day, 0),
            )
            for day in days
        )

    async def _grouped_day_counts(self, statement: Any) -> dict[date, int]:
        """Run a ``select(date, count)`` grouped statement into a day → count map."""
        rows = cast("list[tuple[Any, Any]]", (await self._session.execute(statement)).all())
        return {_day(day): _int(count) for day, count in rows}
