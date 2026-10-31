"""The progress surface against a real schema with the real repositories.

What only the database can prove lives here: that the unlock map joins lessons through
their unit *and* level so a lesson under a draft ancestor disappears, that the summary's
answer tally really reads across every session the learner owns, and that the XP feed
pages by ``(completed_at, id)`` without repeating or skipping a row on the boundary.

The other theme is derivation. ``progress`` never writes, so every figure is checked
against rows the attempt engine actually produced — a score the learner never earned has
no path into these responses.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.registry import import_all_models
from app.core.types import ContentState, UserId
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)
from app.modules.quiz.application.progress_service import ProgressService
from app.modules.quiz.application.quiz_service import QuizService
from app.modules.quiz.domain.entities import QuizSession, SessionMode
from app.modules.quiz.domain.policy import SubmittedAnswer
from app.modules.users.infrastructure.models import UserModel

pytestmark = pytest.mark.integration

_PUBLISHED = ContentState.PUBLISHED.value
_DRAFT = ContentState.DRAFT.value


# ------------------------------------------------------------------------ fixtures
@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest.fixture
def service(uow: SqlAlchemyUnitOfWork) -> ProgressService:
    return ProgressService(uow)


@pytest.fixture
def quiz(uow: SqlAlchemyUnitOfWork) -> QuizService:
    return QuizService(uow, get_settings())


@pytest.fixture
def session(uow: SqlAlchemyUnitOfWork) -> AsyncSession:
    return uow.session


# -------------------------------------------------------------------- row builders
def _user_row(session: AsyncSession) -> UserModel:
    row = UserModel(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        status="active",
    )
    session.add(row)
    return row


def _unit_with_lessons(
    session: AsyncSession,
    *,
    code: str,
    lessons: list[tuple[int, str]],
    level_position: int = 1,
    unit_position: int = 1,
    level_state: str = _PUBLISHED,
    unit_state: str = _PUBLISHED,
    lesson_state: str = _PUBLISHED,
) -> list[LessonModel]:
    """One level → unit with a handful of lessons, each at the given position."""
    level = LevelModel(
        id=uuid.uuid4(), code=code, title=code, position=level_position, state=level_state
    )
    session.add(level)
    unit = UnitModel(
        id=uuid.uuid4(),
        level_id=level.id,
        title=f"{code} unit",
        position=unit_position,
        state=unit_state,
    )
    session.add(unit)
    rows: list[LessonModel] = []
    for position, title in lessons:
        lesson = LessonModel(
            id=uuid.uuid4(),
            unit_id=unit.id,
            title=title,
            summary=f"{title} summary",
            position=position,
            estimated_minutes=5,
            state=lesson_state,
        )
        session.add(lesson)
        rows.append(lesson)
    return rows


def _questions(session: AsyncSession, lesson: LessonModel) -> list[QuestionModel]:
    """Three questions of mixed points, so a scored total cannot be right by accident."""
    rows = [
        QuestionModel(
            id=uuid.uuid4(),
            lesson_id=lesson.id,
            position=1,
            kind="multiple_choice",
            skill="vocabulary",
            prompt="Which one means 'hello'?",
            choices=["Hello", "Goodbye", "Thank you"],
            accepted_answers=[],
            correct_index=0,
            explanation="Hello is the greeting.",
            points=2,
        ),
        QuestionModel(
            id=uuid.uuid4(),
            lesson_id=lesson.id,
            position=2,
            kind="fill_blank",
            skill="grammar",
            prompt="She ___ to school every day.",
            choices=[],
            accepted_answers=["goes"],
            correct_index=None,
            explanation="Third person singular takes -s.",
            points=3,
        ),
        QuestionModel(
            id=uuid.uuid4(),
            lesson_id=lesson.id,
            position=3,
            kind="true_false",
            skill="grammar",
            prompt="'He go to school' is correct.",
            choices=["True", "False"],
            accepted_answers=[],
            correct_index=1,
            explanation="It should be 'He goes'.",
            points=1,
        ),
    ]
    session.add_all(rows)
    return rows


async def _completed_session_row(
    uow: SqlAlchemyUnitOfWork,
    user_id: UserId,
    lesson_id: uuid.UUID,
    *,
    at: datetime,
    correct: int = 2,
    total: int = 3,
    xp: int = 4,
) -> QuizSession:
    """A finished sitting with an explicit completion time, for the streak and feed.

    Built directly rather than through the service because the tests need to place
    completions on chosen days; the service would stamp them all "now".
    """
    sitting = QuizSession(
        id=uuid.uuid4(),
        user_id=user_id,
        mode=SessionMode.LESSON,
        lesson_id=lesson_id,
        question_ids=tuple(uuid.uuid4() for _ in range(total)),
        started_at=at,
        expires_at=at + timedelta(hours=2),
    )
    sitting.complete(xp_awarded=xp, at=at)
    sitting.correct_count = correct
    await uow.quiz_sessions.add(sitting)
    await uow.commit()
    return sitting


async def _attempt(
    quiz: QuizService, user_id: UserId, lesson: LessonModel, questions: list[QuestionModel]
) -> None:
    """Answer the lesson's questions correctly and complete the sitting."""
    detail = await quiz.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson.id)
    session_id = detail.session.id
    await quiz.submit_answer(
        user_id, session_id, question_id=questions[0].id, answer=SubmittedAnswer(choice="Hello")
    )
    await quiz.submit_answer(
        user_id, session_id, question_id=questions[1].id, answer=SubmittedAnswer(text="goes")
    )
    await quiz.submit_answer(
        user_id, session_id, question_id=questions[2].id, answer=SubmittedAnswer(choice="False")
    )
    await quiz.complete_session(user_id, session_id)


# ==================================================================== unlock map
class TestUnlockMap:
    async def test_it_lists_every_published_lesson_in_curriculum_order(
        self, service: ProgressService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        _unit_with_lessons(
            session, code="A1", lessons=[(1, "A1-L1"), (2, "A1-L2")], level_position=1
        )
        _unit_with_lessons(session, code="A2", lessons=[(1, "A2-L1")], level_position=2)
        await session.flush()

        items = await service.unlock_map(UserId(user.id))

        assert [item.title for item in items] == ["A1-L1", "A1-L2", "A2-L1"]
        assert [item.level_code for item in items] == ["A1", "A1", "A2"]

    async def test_an_unstarted_lesson_is_available_and_unlocked(
        self, service: ProgressService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        await session.flush()

        (item,) = await service.unlock_map(UserId(user.id))

        assert item.state == "available"
        assert item.locked is False
        assert item.is_completed is False
        assert item.is_unlocked is True
        assert item.best_score == 0
        assert item.completed_at is None

    async def test_a_started_lesson_reads_in_progress(
        self, service: ProgressService, quiz: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        questions = _questions(session, lesson)
        await session.flush()
        await quiz.start_session(UserId(user.id), mode=SessionMode.LESSON, lesson_id=lesson.id)

        (item,) = await service.unlock_map(UserId(user.id))

        assert item.state == "in_progress"
        assert questions  # the lesson is attemptable

    async def test_a_completed_lesson_reads_completed_with_its_score(
        self, service: ProgressService, quiz: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        questions = _questions(session, lesson)
        await session.flush()
        await _attempt(quiz, UserId(user.id), lesson, questions)

        (item,) = await service.unlock_map(UserId(user.id))

        assert item.state == "completed"
        assert item.locked is False
        assert item.is_completed is True
        assert item.is_unlocked is True
        assert item.best_score == 100

    async def test_subsequent_lessons_in_unit_are_locked_until_predecessors_completed(
        self, service: ProgressService, quiz: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        lessons = _unit_with_lessons(session, code="A1", lessons=[(1, "L1"), (2, "L2"), (3, "L3")])
        questions_l1 = _questions(session, lessons[0])
        _questions(session, lessons[1])
        _questions(session, lessons[2])
        await session.flush()

        items = await service.unlock_map(UserId(user.id))
        assert [i.is_unlocked for i in items] == [True, False, False]
        assert [i.locked for i in items] == [False, True, True]
        assert [i.is_completed for i in items] == [False, False, False]

        await _attempt(quiz, UserId(user.id), lessons[0], questions_l1)

        items = await service.unlock_map(UserId(user.id))
        assert [i.is_unlocked for i in items] == [True, True, False]
        assert [i.locked for i in items] == [False, False, True]
        assert [i.is_completed for i in items] == [True, False, False]

    async def test_a_lesson_under_a_draft_unit_is_excluded(
        self, service: ProgressService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        _unit_with_lessons(session, code="A1", lessons=[(1, "L1")], unit_state=_DRAFT)
        await session.flush()

        assert await service.unlock_map(UserId(user.id)) == []

    async def test_a_lesson_under_a_draft_level_is_excluded(
        self, service: ProgressService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        _unit_with_lessons(session, code="A1", lessons=[(1, "L1")], level_state=_DRAFT)
        await session.flush()

        assert await service.unlock_map(UserId(user.id)) == []

    async def test_the_map_is_scoped_to_the_caller(
        self, service: ProgressService, quiz: QuizService, session: AsyncSession
    ) -> None:
        owner = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        questions = _questions(session, lesson)
        other = _user_row(session)
        await session.flush()
        await _attempt(quiz, UserId(owner.id), lesson, questions)

        (item,) = await service.unlock_map(UserId(other.id))

        assert item.state == "available"


# ================================================================ one lesson
class TestLessonProgress:
    async def test_a_published_lesson_returns_its_placement_and_zero_record(
        self, service: ProgressService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "Greetings")])
        await session.flush()

        view = await service.lesson_progress(UserId(user.id), lesson.id)

        assert view.lesson_id == lesson.id
        assert view.unit_id == lesson.unit_id
        assert view.level_code == "A1"
        assert view.title == "Greetings"
        assert view.state == "available"
        assert view.locked is False
        assert view.is_completed is False
        assert view.is_unlocked is True
        assert (view.best_score, view.attempts, view.xp_earned) == (0, 0, 0)

    async def test_a_completed_lesson_reports_its_record(
        self, service: ProgressService, quiz: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        questions = _questions(session, lesson)
        await session.flush()
        await _attempt(quiz, UserId(user.id), lesson, questions)

        view = await service.lesson_progress(UserId(user.id), lesson.id)

        assert view.state == "completed"
        assert view.best_score == 100
        assert view.attempts == 1
        assert view.xp_earned == 6
        assert view.completed_at is not None

    async def test_an_unknown_lesson_is_not_found(
        self, service: ProgressService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.lesson_progress(UserId(user.id), uuid.uuid4())

        assert raised.value.code is ErrorCode.LESSON_NOT_FOUND

    async def test_a_draft_lesson_is_not_found(
        self, service: ProgressService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")], lesson_state=_DRAFT)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.lesson_progress(UserId(user.id), lesson.id)

        assert raised.value.code is ErrorCode.LESSON_NOT_FOUND

    async def test_a_lesson_under_a_draft_unit_is_not_found(
        self, service: ProgressService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")], unit_state=_DRAFT)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.lesson_progress(UserId(user.id), lesson.id)

        assert raised.value.code is ErrorCode.LESSON_NOT_FOUND


# ==================================================================== summary
class TestSummary:
    async def test_a_learner_with_no_history_reports_zero_everything(
        self, service: ProgressService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        await session.flush()

        view = await service.summary(UserId(user.id))

        assert view.total_xp == 0
        assert view.lessons_started == 0
        assert view.lessons_completed == 0
        assert view.accuracy_percent == 0
        assert view.current_streak_days == 0
        assert view.longest_streak_days == 0
        assert view.last_activity_at is None

    async def test_it_sums_xp_and_counts_a_completed_lesson(
        self, service: ProgressService, quiz: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        questions = _questions(session, lesson)
        await session.flush()
        await _attempt(quiz, UserId(user.id), lesson, questions)

        view = await service.summary(UserId(user.id))

        assert view.total_xp == 6
        assert view.lessons_started == 1
        assert view.lessons_completed == 1
        assert view.lessons_in_progress == 0
        assert view.answered_count == 3
        assert view.correct_count == 3
        assert view.accuracy_percent == 100
        assert view.current_streak_days == 1
        assert view.longest_streak_days == 1
        assert view.last_activity_at is not None

    async def test_accuracy_is_counted_from_answers_not_from_a_guess(
        self, service: ProgressService, quiz: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        questions = _questions(session, lesson)
        await session.flush()
        detail = await quiz.start_session(
            UserId(user.id), mode=SessionMode.LESSON, lesson_id=lesson.id
        )
        session_id = detail.session.id
        # One wrong, two right: 2 of 3 answered correctly.
        await quiz.submit_answer(
            UserId(user.id),
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),
        )
        await quiz.submit_answer(
            UserId(user.id),
            session_id,
            question_id=questions[1].id,
            answer=SubmittedAnswer(text="goes"),
        )
        await quiz.submit_answer(
            UserId(user.id),
            session_id,
            question_id=questions[2].id,
            answer=SubmittedAnswer(choice="False"),
        )
        await quiz.complete_session(UserId(user.id), session_id)

        view = await service.summary(UserId(user.id))

        assert (view.answered_count, view.correct_count) == (3, 2)
        assert view.accuracy_percent == 67

    async def test_an_unfinished_lesson_counts_as_in_progress(
        self, service: ProgressService, quiz: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        _questions(session, lesson)
        await session.flush()
        started = await quiz.start_session(
            UserId(user.id), mode=SessionMode.LESSON, lesson_id=lesson.id
        )
        await quiz.abandon_session(UserId(user.id), started.session.id)

        view = await service.summary(UserId(user.id))

        assert view.lessons_started == 1
        assert view.lessons_in_progress == 1
        assert view.lessons_completed == 0
        # An abandoned sitting earned nothing and is not "activity".
        assert view.last_activity_at is None
        assert view.total_xp == 0

    async def test_the_streak_counts_the_learner_local_days(
        self, service: ProgressService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        await session.flush()
        now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        for days_ago in range(3):
            await _completed_session_row(
                uow, UserId(user.id), lesson.id, at=now - timedelta(days=days_ago)
            )

        view = await service.summary(UserId(user.id))

        assert view.current_streak_days == 3
        assert view.longest_streak_days == 3


# ================================================================== xp history
class TestXpHistory:
    async def test_only_completed_sessions_appear(
        self, service: ProgressService, quiz: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        questions = _questions(session, lesson)
        await session.flush()
        # One completed sitting, then an abandoned one.
        await _attempt(quiz, UserId(user.id), lesson, questions)
        abandoned = await quiz.start_session(
            UserId(user.id), mode=SessionMode.LESSON, lesson_id=lesson.id
        )
        await quiz.abandon_session(UserId(user.id), abandoned.session.id)

        page = await service.xp_history(UserId(user.id), limit=10)

        assert len(page.items) == 1

    async def test_sessions_are_newest_first(
        self, service: ProgressService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        await session.flush()
        base = datetime(2026, 2, 1, 9, 0, 0, tzinfo=UTC)
        oldest = await _completed_session_row(uow, UserId(user.id), lesson.id, at=base)
        middle = await _completed_session_row(
            uow, UserId(user.id), lesson.id, at=base + timedelta(hours=1)
        )
        newest = await _completed_session_row(
            uow, UserId(user.id), lesson.id, at=base + timedelta(hours=2)
        )

        page = await service.xp_history(UserId(user.id), limit=10)

        assert [event.session_id for event in page.items] == [
            newest.id,
            middle.id,
            oldest.id,
        ]

    async def test_the_cursor_pages_without_overlap(
        self, service: ProgressService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        await session.flush()
        base = datetime(2026, 2, 1, 9, 0, 0, tzinfo=UTC)
        created = [
            await _completed_session_row(
                uow, UserId(user.id), lesson.id, at=base + timedelta(hours=index)
            )
            for index in range(3)
        ]

        first = await service.xp_history(UserId(user.id), limit=2)
        assert first.has_more is True
        assert first.next_cursor is not None

        second = await service.xp_history(UserId(user.id), limit=2, cursor=first.next_cursor)

        seen = [event.session_id for event in first.items] + [
            event.session_id for event in second.items
        ]
        assert seen == [created[2].id, created[1].id, created[0].id]
        assert len(set(seen)) == 3
        assert second.has_more is False
        assert second.next_cursor is None

    async def test_the_score_reflects_the_session(
        self, service: ProgressService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        (lesson,) = _unit_with_lessons(session, code="A1", lessons=[(1, "L1")])
        await session.flush()
        await _completed_session_row(
            uow,
            UserId(user.id),
            lesson.id,
            at=datetime(2026, 2, 1, 9, 0, 0, tzinfo=UTC),
            correct=1,
            total=4,
            xp=3,
        )

        (event,) = (await service.xp_history(UserId(user.id), limit=10)).items

        assert event.lesson_id == lesson.id
        assert event.correct_count == 1
        assert event.total_count == 4
        assert event.score_percent == 25
        assert event.xp_awarded == 3

    async def test_an_invalid_cursor_is_refused(
        self, service: ProgressService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        await session.flush()

        with pytest.raises(ValidationFailed):
            await service.xp_history(UserId(user.id), limit=10, cursor="not-a-real-cursor")


@pytest.mark.asyncio
async def test_completed_lesson_ids_is_a_membership_read(uow: SqlAlchemyUnitOfWork) -> None:
    """The unlock check needs a set of ids, not every progress row.

    Opening one lesson used to hydrate the learner's whole record just to ask whether a
    predecessor was completed. This read returns one indexed column and only the rows
    that are actually completed; a lesson in progress must not unlock its successor.
    """
    from app.modules.quiz.domain.entities import LessonProgress, ProgressState

    user = _user_row(uow.session)
    finished, started = _unit_with_lessons(
        uow.session, code="A1", lessons=[(1, "Finished"), (2, "Started")]
    )
    await uow.session.flush()
    await uow.lesson_progress.save(
        LessonProgress(
            id=uuid.uuid4(),
            user_id=UserId(user.id),
            lesson_id=finished.id,
            state=ProgressState.COMPLETED,
        )
    )
    await uow.lesson_progress.save(
        LessonProgress(
            id=uuid.uuid4(),
            user_id=UserId(user.id),
            lesson_id=started.id,
            state=ProgressState.IN_PROGRESS,
        )
    )
    await uow.commit()

    assert await uow.lesson_progress.completed_lesson_ids(UserId(user.id)) == {finished.id}
