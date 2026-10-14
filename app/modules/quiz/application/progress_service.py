"""Progress use cases: everything the learner has already done.

Read-only, and deliberately offset from the attempt engine. :class:`QuizService` *writes*
the durable record — a session, its answers, a ``lesson_progress`` row — while this slice
only ever restates it. Keeping the two apart means a progress screen can be built, cached
and reasoned about without touching grading, and a bug in a streak calculation cannot
corrupt a score.

Every figure here is derived, never projected. ``summary`` counts from the rows the engine
actually wrote, so it cannot report a number the learner did not earn, and nothing matters
that does not yet have a backing store (hearts, reviews due) appears at all. The one value
computed outside SQL is the streak, and that is on purpose: a streak is a *calendar*
question, so it is answered in the learner's own timezone (``app/core/time.py``) rather than
hard-coding a zone into the query.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from itertools import pairwise

from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.pagination import (
    InvalidCursor,
    PageResult,
    TimeCursor,
    decode_time_cursor,
    paginate_time,
)
from app.core.time import local_date, utc_now
from app.core.types import ContentId, UserId
from app.core.uow import UnitOfWorkProtocol
from app.modules.content.domain.entities import LessonPlacement
from app.modules.quiz.application.progress_dto import (
    LessonProgressView,
    ProgressSummaryView,
    UnlockMapItemView,
    XpEventView,
)
from app.modules.quiz.domain.entities import LessonProgress, ProgressState, QuizSession

__all__ = ["ProgressService"]

#: With no gating rule in force yet, nothing is locked. This is a named constant rather
#: than an inline ``False`` so that landing sequential unlocking is a change to one
#: expression here, and so the unlock-map tests have something to point at.
_LOCKED: bool = False


class ProgressService:
    def __init__(self, uow: UnitOfWorkProtocol) -> None:
        self._uow = uow

    # ================================================================ summary
    async def summary(self, user_id: UserId) -> ProgressSummaryView:
        """Roll the learner's durable record into the headline figures.

        Three reads, not one per lesson: the progress rows, an answer tally and the
        completion times. The timezone is read from the learner's profile so the streak
        is their calendar, not the server's, and a missing profile falls back to the
        default zone rather than UTC — the product default is Asia/Baghdad.
        """
        progress_rows = await self._uow.lesson_progress.list_for_user(user_id)
        tally = await self._uow.quiz_answers.tally_for_user(user_id)
        completion_times = await self._uow.quiz_sessions.list_completion_times(user_id)
        timezone = await self._profile_timezone(user_id)

        current_streak, longest_streak = _streaks(completion_times, timezone)

        return ProgressSummaryView(
            total_xp=sum(row.xp_earned for row in progress_rows),
            lessons_completed=sum(
                1 for row in progress_rows if row.state is ProgressState.COMPLETED
            ),
            lessons_in_progress=sum(
                1 for row in progress_rows if row.state is ProgressState.IN_PROGRESS
            ),
            lessons_started=len(progress_rows),
            correct_count=tally.correct,
            answered_count=tally.answered,
            accuracy_percent=tally.accuracy_percent,
            current_streak_days=current_streak,
            longest_streak_days=longest_streak,
            last_activity_at=completion_times[0] if completion_times else None,
        )

    # ============================================================ one lesson
    async def lesson_progress(self, user_id: UserId, lesson_id: uuid.UUID) -> LessonProgressView:
        """One lesson's placement and the learner's record against it.

        The placement is required to be published through its whole chain — lesson, unit
        and level — so a lesson that is not currently reachable reads as a 404 rather than
        exposing the editor's draft tree. A learner who has never touched the lesson still
        gets a view, with the zeroed record.
        """
        placement = await self._uow.lessons.get_published_placement(ContentId(lesson_id))
        if placement is None:
            raise NotFoundError(
                "There is no published lesson with that id.",
                code=ErrorCode.LESSON_NOT_FOUND,
            )
        row = await self._uow.lesson_progress.get(user_id, lesson_id)
        return _lesson_view(placement, row)

    # ============================================================= unlock map
    async def unlock_map(self, user_id: UserId) -> list[UnlockMapItemView]:
        """Every published lesson, in curriculum order, coloured by the learner's record.

        The whole course at once, because that is what a map draws. The catalogue is the
        bound on size. Lessons the learner has not started come back ``available`` with a
        zero record rather than being omitted, so the client can render the road ahead
        without a second query.
        """
        placements = await self._uow.lessons.list_published_ordered()
        rows = await self._uow.lesson_progress.list_for_user(user_id)
        by_lesson = {row.lesson_id: row for row in rows}
        return [
            _map_item(placement, by_lesson.get(placement.lesson.id)) for placement in placements
        ]

    # ============================================================= xp history
    async def xp_history(
        self, user_id: UserId, *, limit: int, cursor: str | None = None
    ) -> PageResult[XpEventView]:
        """The learner's completed sessions, newest first, as an XP feed.

        Only ``completed`` sessions appear: an abandoned sitting earned nothing and has no
        place in a history of what was achieved. Paginated by ``(completed_at, id)`` — a
        stream has no editorial position, so the instant is the natural sort key.
        """
        after = _decode_cursor(cursor)
        sessions = await self._uow.quiz_sessions.list_completed_for_user(
            user_id, limit=limit + 1, after=after
        )
        page = paginate_time(sessions, limit=limit, key_of=_session_key)
        return PageResult(
            items=[_xp_event(session) for session in page.items],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    # =============================================================== internals
    async def _profile_timezone(self, user_id: UserId) -> str | None:
        profile = await self._uow.users.get_profile(user_id)
        return profile.timezone if profile is not None else None


# ------------------------------------------------------------------ helpers
def _session_key(session: QuizSession) -> tuple[datetime, uuid.UUID]:
    """Sort key for the XP feed: when it finished, then the id as a tiebreaker."""
    return (session.completed_at or session.started_at, session.id)


def _decode_cursor(cursor: str | None) -> TimeCursor | None:
    if cursor is None:
        return None
    try:
        return decode_time_cursor(cursor)
    except InvalidCursor as exc:
        raise ValidationFailed(
            "The cursor is not usable. Start again from the first page.",
            details={"field": "cursor"},
        ) from exc


def _streaks(times: list[datetime], timezone: str | None) -> tuple[int, int]:
    """Current and longest run of consecutive days with a completed session.

    The current streak counts back from today, and only if the learner practiced today or
    yesterday — a gap of a day ends it rather than pausing it. Both runs are measured in
    the learner's local calendar, so "midnight" is their midnight.
    """
    days = sorted({local_date(moment, timezone) for moment in times})
    if not days:
        return 0, 0

    longest = _longest_run(days)

    today = local_date(utc_now(), timezone)
    descending = list(reversed(days))
    current = 0
    if descending[0] == today or descending[0] == today - timedelta(days=1):
        current = 1
        for later, earlier in pairwise(descending):
            if later - earlier == timedelta(days=1):
                current += 1
            else:
                break
    return current, longest


def _longest_run(days: list[date]) -> int:
    longest = run = 1
    for earlier, later in pairwise(days):
        if later - earlier == timedelta(days=1):
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    return longest


def _lesson_view(placement: LessonPlacement, row: LessonProgress | None) -> LessonProgressView:
    lesson = placement.lesson
    return LessonProgressView(
        lesson_id=lesson.id,
        unit_id=lesson.unit_id,
        level_code=placement.level_code.value,
        title=lesson.title,
        position=lesson.position,
        state=row.state.value if row is not None else "available",
        locked=_LOCKED,
        best_score=row.best_score if row is not None else 0,
        attempts=row.attempts if row is not None else 0,
        xp_earned=row.xp_earned if row is not None else 0,
        completed_at=row.completed_at if row is not None else None,
    )


def _map_item(placement: LessonPlacement, row: LessonProgress | None) -> UnlockMapItemView:
    lesson = placement.lesson
    return UnlockMapItemView(
        lesson_id=lesson.id,
        unit_id=lesson.unit_id,
        level_code=placement.level_code.value,
        title=lesson.title,
        position=lesson.position,
        state=row.state.value if row is not None else "available",
        locked=_LOCKED,
        best_score=row.best_score if row is not None else 0,
        attempts=row.attempts if row is not None else 0,
        xp_earned=row.xp_earned if row is not None else 0,
        completed_at=row.completed_at if row is not None else None,
    )


def _xp_event(session: QuizSession) -> XpEventView:
    return XpEventView(
        session_id=session.id,
        lesson_id=session.lesson_id,
        xp_awarded=session.xp_awarded,
        correct_count=session.correct_count,
        total_count=session.total,
        score_percent=_percentage(session.correct_count, session.total),
        completed_at=session.completed_at,
    )


def _percentage(correct: int, total: int) -> int:
    if total <= 0:
        return 0
    return round(correct / total * 100)
