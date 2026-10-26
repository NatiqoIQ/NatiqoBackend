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
from datetime import datetime

from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.pagination import (
    InvalidCursor,
    PageResult,
    TimeCursor,
    decode_time_cursor,
    paginate_time,
)
from app.core.time import local_date, streak_runs, utc_now
from app.core.types import ContentId, UserId
from app.core.uow import UnitOfWorkProtocol
from app.modules.content.domain.entities import LessonPlacement
from app.modules.quiz.application.progress_dto import (
    LessonProgressView,
    ProgressSummaryView,
    UnitProgressItemView,
    UnlockMapItemView,
    XpEventView,
)
from app.modules.quiz.domain.entities import LessonProgress, ProgressState, QuizSession
from app.modules.quiz.domain.unlock import lesson_locked, unit_locked

__all__ = ["ProgressService"]


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
            words_learned=sum(row.words_learned for row in progress_rows),
        )

    # ============================================================ one lesson
    async def lesson_progress(self, user_id: UserId, lesson_id: uuid.UUID) -> LessonProgressView:
        """One lesson's placement and the learner's record against it.

        The placement is required to be published through its whole chain — lesson, unit
        and level — so a lesson that is not currently reachable reads as a 404 rather than
        exposing the editor's draft tree. A learner who has never touched the lesson still
        gets a view, with the zeroed record.

        ``locked`` is the sequential-unlock rule: a lesson is open when its unit is open and
        its predecessor lesson has been completed.
        """
        placement = await self._uow.lessons.get_published_placement(ContentId(lesson_id))
        if placement is None:
            raise NotFoundError(
                "There is no published lesson with that id.",
                code=ErrorCode.LESSON_NOT_FOUND,
            )
        row = await self._uow.lesson_progress.get(user_id, lesson_id)
        ordered_units, passed_units = await self._unlock_inputs(user_id)
        all_placements = await self._uow.lessons.list_published_ordered()
        unit_lessons = [
            p.lesson.id for p in all_placements if p.lesson.unit_id == placement.lesson.unit_id
        ]
        rows = await self._uow.lesson_progress.list_for_user(user_id)
        completed_lessons = {r.lesson_id for r in rows if r.state is ProgressState.COMPLETED}
        is_u_locked = unit_locked(placement.lesson.unit_id, ordered_units, passed_units)
        l_locked = lesson_locked(
            placement.lesson.id,
            unit_lessons,
            completed_lessons,
            is_unit_locked=is_u_locked,
        )
        return _lesson_view(
            placement,
            row,
            locked=l_locked,
        )

    # ============================================================= unlock map
    async def unlock_map(self, user_id: UserId) -> list[UnlockMapItemView]:
        """Every published lesson, in curriculum order, coloured by the learner's record.

        The whole course at once, because that is what a map draws. The catalogue is the
        bound on size. Lessons the learner has not started come back ``available`` with a
        zero record rather than being omitted, so the client can render the road ahead
        without a second query.

        ``locked`` is the sequential-unlock rule: every lesson of a shut unit is locked,
        and inside an open unit a lesson opens once its predecessor lesson is completed.
        """
        placements = await self._uow.lessons.list_published_ordered()
        rows = await self._uow.lesson_progress.list_for_user(user_id)
        by_lesson = {row.lesson_id: row for row in rows}
        completed_lessons = {row.lesson_id for row in rows if row.state is ProgressState.COMPLETED}
        ordered_units, passed_units = await self._unlock_inputs(user_id)

        lessons_by_unit: dict[uuid.UUID, list[uuid.UUID]] = {}
        for placement in placements:
            lessons_by_unit.setdefault(placement.lesson.unit_id, []).append(placement.lesson.id)

        items: list[UnlockMapItemView] = []
        for placement in placements:
            unit_id = placement.lesson.unit_id
            is_u_locked = unit_locked(unit_id, ordered_units, passed_units)
            unit_lessons = lessons_by_unit.get(unit_id, [])
            l_locked = lesson_locked(
                placement.lesson.id,
                unit_lessons,
                completed_lessons,
                is_unit_locked=is_u_locked,
            )
            items.append(
                _map_item(
                    placement,
                    by_lesson.get(placement.lesson.id),
                    locked=l_locked,
                )
            )
        return items

    # ============================================================ unit list
    async def units(self, user_id: UserId) -> list[UnitProgressItemView]:
        """Every published unit in curriculum order, with its checkpoint standing.

        Four reads and a derived view per unit: the unit order (which carries the level
        and the sequence the unlock rule walks), every published lesson so the unit can
        count what is done, and the learner's lesson and unit records. The counts come
        from the placements rather than a per-unit query, so the screen costs the same
        whether the course has three units or thirty.

        ``checkpoint_ready`` states the lessons question only. Whether the unit is open is
        a separate fact — ``locked`` — and the two are deliberately not folded together:
        a shut unit can still have its lessons finished from an older era of the course,
        and a client that wants "can I sit the checkpoint *now*" asks for both.
        """
        unit_placements = await self._uow.units.list_published_ordered()
        lesson_placements = await self._uow.lessons.list_published_ordered()
        lesson_rows = await self._uow.lesson_progress.list_for_user(user_id)
        unit_rows = await self._uow.unit_progress.list_for_user(user_id)

        lessons_by_unit: dict[uuid.UUID, list[LessonPlacement]] = {}
        for placement in lesson_placements:
            lessons_by_unit.setdefault(placement.lesson.unit_id, []).append(placement)
        completed_lessons = {
            row.lesson_id for row in lesson_rows if row.state is ProgressState.COMPLETED
        }
        by_unit = {row.unit_id: row for row in unit_rows}
        ordered_units = [item.unit.id for item in unit_placements]
        passed_units = {row.unit_id for row in unit_rows if row.has_passed}

        items: list[UnitProgressItemView] = []
        for item in unit_placements:
            unit_lessons = lessons_by_unit.get(item.unit.id, [])
            completed = sum(
                1 for placement in unit_lessons if placement.lesson.id in completed_lessons
            )
            row = by_unit.get(item.unit.id)
            items.append(
                UnitProgressItemView(
                    unit_id=item.unit.id,
                    level_code=item.level_code.value,
                    title=item.unit.title,
                    position=item.unit.position,
                    locked=unit_locked(item.unit.id, ordered_units, passed_units),
                    passed=row.has_passed if row is not None else False,
                    best_score=row.best_score if row is not None else 0,
                    attempts=row.attempts if row is not None else 0,
                    passed_at=row.passed_at if row is not None else None,
                    lessons_total=len(unit_lessons),
                    lessons_completed=completed,
                    checkpoint_ready=len(unit_lessons) > 0 and completed == len(unit_lessons),
                )
            )
        return items

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
    async def _unlock_inputs(self, user_id: UserId) -> tuple[list[uuid.UUID], set[uuid.UUID]]:
        """The two facts the sequential-unlock rule consumes.

        The published units in curriculum order — the chain — and the units the learner
        has passed. Returned together because every caller needs both, and reading them
        once per view keeps a screen showing many lessons from asking the same question
        over and over.
        """
        units = await self._uow.units.list_published_ordered()
        rows = await self._uow.unit_progress.list_for_user(user_id)
        return [item.unit.id for item in units], {row.unit_id for row in rows if row.has_passed}

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
    the learner's local calendar, so "midnight" is their midnight. The calendar maths is
    shared with the gamification projection (:func:`app.core.time.streak_runs`) so the two
    surfaces can never disagree about what a streak is.
    """
    days = [local_date(moment, timezone) for moment in times]
    return streak_runs(days, today=local_date(utc_now(), timezone))


def _lesson_view(
    placement: LessonPlacement, row: LessonProgress | None, *, locked: bool
) -> LessonProgressView:
    lesson = placement.lesson
    is_completed = row is not None and row.state is ProgressState.COMPLETED
    return LessonProgressView(
        lesson_id=lesson.id,
        unit_id=lesson.unit_id,
        level_code=placement.level_code.value,
        title=lesson.title,
        position=lesson.position,
        state=row.state.value if row is not None else "available",
        locked=locked,
        is_completed=is_completed,
        is_unlocked=not locked,
        best_score=row.best_score if row is not None else 0,
        attempts=row.attempts if row is not None else 0,
        xp_earned=row.xp_earned if row is not None else 0,
        words_learned=row.words_learned if row is not None else 0,
        completed_at=row.completed_at if row is not None else None,
    )


def _map_item(
    placement: LessonPlacement, row: LessonProgress | None, *, locked: bool
) -> UnlockMapItemView:
    lesson = placement.lesson
    is_completed = row is not None and row.state is ProgressState.COMPLETED
    return UnlockMapItemView(
        lesson_id=lesson.id,
        unit_id=lesson.unit_id,
        level_code=placement.level_code.value,
        title=lesson.title,
        position=lesson.position,
        state=row.state.value if row is not None else "available",
        locked=locked,
        is_completed=is_completed,
        is_unlocked=not locked,
        best_score=row.best_score if row is not None else 0,
        attempts=row.attempts if row is not None else 0,
        xp_earned=row.xp_earned if row is not None else 0,
        words_learned=row.words_learned if row is not None else 0,
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
        unit_id=session.unit_id,
    )


def _percentage(correct: int, total: int) -> int:
    if total <= 0:
        return 0
    return round(correct / total * 100)
