"""SQLAlchemy adapter for the overview's aggregate reads.

Seven queries, each a ``count`` or a ``sum``, and not one row loaded into Python. That is
the whole point of the class: a dashboard that counted by fetching would get slower every
week the product ran, and its cost would grow with the data rather than with the screen.

Two definitions worth stating because nothing in the SQL says them out loud:

* **Active** means "signed in during the window", read from ``user_identities.last_login_at``
  and counted distinct, because one person may hold a Google and an Apple identity. It is the
  widest honest definition: a learner who opens the app and reads a lesson without finishing
  it is still active, and counting completed sessions would undercount them.
* **Accuracy** is weighted by answers, computed in the application layer from
  ``answers_graded`` and ``correct_answers`` here. Averaging per-session percentages would
  let a two-question sitting outweigh a twenty-question one.

The conditional sums use ``sum(case(...))`` rather than ``count(*) filter (where ...)``
because the latter is PostgreSQL-only and the suite runs on SQLite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import ContentState, UserStatus
from app.modules.admin.domain.stats import AdminOverview
from app.modules.content.infrastructure.models import LessonModel, LevelModel, UnitModel
from app.modules.quiz.domain.entities import ProgressState, SessionStatus
from app.modules.quiz.infrastructure.models import (
    LessonProgressModel,
    QuizAnswerModel,
    QuizSessionModel,
)
from app.modules.users.infrastructure.models import UserIdentityModel, UserModel

__all__ = ["SqlAlchemyAdminStatsRepository"]


def _int(value: Any) -> int:
    """A nullable aggregate as an int.

    ``sum`` over zero rows is ``NULL``, not ``0``, so every conditional sum is wrapped here
    once rather than at each use.
    """
    return int(value or 0)


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

        return AdminOverview(
            users_total=_int(users[0]),
            users_new=_int(users[1]),
            users_active=_int(active),
            users_deleted=_int(users[2]),
            lessons_completed=_int(completed_lessons),
            sessions_completed=_int(sessions[0]),
            answers_graded=_int(answers[0]),
            correct_answers=_int(answers[1]),
            xp_awarded=_int(sessions[1]),
            levels=levels,
            units=units,
            lessons=lessons,
            published_lessons=lesson_published,
            # Drafts and reviews are summed across all three aggregates because the screen
            # shows one queue — "18 items await review" — not three separate ones.
            drafts=level_drafts + unit_drafts + lesson_drafts,
            in_review=level_review + unit_review + lesson_review,
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
