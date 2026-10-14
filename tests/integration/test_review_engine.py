"""The spaced-repetition surface against a real schema and the real repositories.

What only the database can prove lives here: that the due queue is a projection of cards
onto *published* questions so a card whose lesson has slipped back to draft disappears,
that a card id belonging to another account reads as absent, that answering advances the
schedule and appends the log in one transaction, and that the attempt engine seeds exactly
one card per question the first time it is answered correctly.

The other theme is that the schedule is derived, never invented. Every number a review
returns is restated from what :meth:`ReviewCard.schedule` actually computed, so a figure
the learner never earned has no path into these responses.

The SM-2 arithmetic itself is pinned in ``tests/unit/test_review_sm2.py``; this file is
about the wiring around it.
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
from app.core.errors import ErrorCode, NotFoundError
from app.core.registry import import_all_models
from app.core.types import ContentState, UserId
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)
from app.modules.quiz.application.quiz_service import QuizService
from app.modules.quiz.application.review_service import ReviewService
from app.modules.quiz.domain.entities import SessionMode
from app.modules.quiz.domain.policy import SubmittedAnswer
from app.modules.quiz.domain.review import ReviewCard, ReviewState
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
def service(uow: SqlAlchemyUnitOfWork) -> ReviewService:
    return ReviewService(uow)


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
        roles=["user"],
    )
    session.add(row)
    return row


def _published_lesson(
    session: AsyncSession, *, questions: int = 1, lesson_state: str = _PUBLISHED
) -> tuple[LessonModel, list[QuestionModel]]:
    """One published level → unit → lesson, with answerable multiple-choice questions."""
    level = LevelModel(id=uuid.uuid4(), code="A1", title="A1", position=1, state=_PUBLISHED)
    session.add(level)
    unit = UnitModel(
        id=uuid.uuid4(),
        level_id=level.id,
        title="Introductions",
        position=1,
        state=_PUBLISHED,
    )
    session.add(unit)
    lesson = LessonModel(
        id=uuid.uuid4(),
        unit_id=unit.id,
        title="Greetings",
        summary="A short summary.",
        position=1,
        estimated_minutes=5,
        state=lesson_state,
    )
    session.add(lesson)
    rows = [
        QuestionModel(
            id=uuid.uuid4(),
            lesson_id=lesson.id,
            position=position,
            kind="multiple_choice",
            skill="vocabulary",
            prompt="Which one means 'hello'?",
            choices=["Hello", "Goodbye", "Thank you"],
            accepted_answers=[],
            correct_index=0,
            explanation="Hello is the greeting.",
            points=1,
        )
        for position in range(1, questions + 1)
    ]
    session.add_all(rows)
    return lesson, rows


async def _card(
    uow: SqlAlchemyUnitOfWork,
    user_id: UserId,
    question_id: uuid.UUID,
    *,
    due_at: datetime,
    repetitions: int = 0,
    interval_days: int = 0,
    lapses: int = 0,
    state: ReviewState = ReviewState.NEW,
) -> ReviewCard:
    """Plant a card at a chosen point in its life, so the queue can be populated on demand.

    Built directly rather than grown through the scheduler because the tests need to place
    due dates in the past; answering would stamp everything "now".
    """
    card = ReviewCard(
        id=uuid.uuid4(),
        user_id=user_id,
        question_id=question_id,
        ease_factor=2.5,
        interval_days=interval_days,
        repetitions=repetitions,
        lapses=lapses,
        due_at=due_at,
        last_reviewed_at=None,
        state=state,
    )
    await uow.review_cards.add(card)
    await uow.commit()
    return card


def _now() -> datetime:
    return datetime.now(UTC)


# ==================================================================== the due queue
class TestDueQueue:
    async def test_it_lists_only_cards_that_are_due(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        _, questions = _published_lesson(session, questions=2)
        await session.flush()
        await _card(uow, UserId(user.id), questions[0].id, due_at=_now() - timedelta(days=1))
        await _card(uow, UserId(user.id), questions[1].id, due_at=_now() + timedelta(days=3))

        page = await service.due_cards(UserId(user.id), limit=20)

        assert [card.question.id for card in page.cards] == [questions[0].id]
        assert page.due_count == 1
        assert page.has_more is False

    async def test_the_soonest_card_comes_first(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        _, questions = _published_lesson(session, questions=3)
        await session.flush()
        await _card(uow, UserId(user.id), questions[0].id, due_at=_now() - timedelta(hours=1))
        await _card(uow, UserId(user.id), questions[1].id, due_at=_now() - timedelta(days=2))
        await _card(uow, UserId(user.id), questions[2].id, due_at=_now() - timedelta(days=1))

        page = await service.due_cards(UserId(user.id), limit=20)

        assert [card.question.id for card in page.cards] == [
            questions[1].id,
            questions[2].id,
            questions[0].id,
        ]

    async def test_the_card_carries_its_question_without_the_answer(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """The queue renders like a lesson, so the key still only arrives on an answer."""
        user = _user_row(session)
        _, questions = _published_lesson(session)
        await session.flush()
        await _card(
            uow,
            UserId(user.id),
            questions[0].id,
            due_at=_now() - timedelta(days=1),
            repetitions=2,
            interval_days=6,
            state=ReviewState.REVIEW,
        )

        (card,) = (await service.due_cards(UserId(user.id), limit=20)).cards

        assert card.question.prompt == "Which one means 'hello'?"
        assert card.question.choices == ("Hello", "Goodbye", "Thank you")
        assert card.repetitions == 2
        assert card.interval_days == 6
        assert card.state == "review"

    async def test_a_card_whose_lesson_slipped_back_to_draft_is_dropped(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """A card can outlive its question's publication; the queue hides it rather than
        surfacing a review with nothing to grade against."""
        user = _user_row(session)
        _, questions = _published_lesson(session, lesson_state=_DRAFT)
        await session.flush()
        await _card(uow, UserId(user.id), questions[0].id, due_at=_now() - timedelta(days=1))

        page = await service.due_cards(UserId(user.id), limit=20)

        assert page.cards == ()

    async def test_a_strict_queue_that_is_empty_is_not_found(
        self, service: ReviewService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.due_cards(UserId(user.id), limit=20, strict=True)

        assert raised.value.code is ErrorCode.REVIEW_NOTHING_DUE

    async def test_a_non_strict_empty_queue_is_an_empty_page(
        self, service: ReviewService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        await session.flush()

        page = await service.due_cards(UserId(user.id), limit=20)

        assert page.cards == ()
        assert page.due_count == 0

    async def test_the_page_boundary_reports_more_to_come(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        _, questions = _published_lesson(session, questions=3)
        await session.flush()
        for index, question in enumerate(questions):
            await _card(
                uow,
                UserId(user.id),
                question.id,
                due_at=_now() - timedelta(hours=index + 1),
            )

        first = await service.due_cards(UserId(user.id), limit=2)

        assert len(first.cards) == 2
        assert first.has_more is True
        assert first.due_count == 3

    async def test_the_queue_is_scoped_to_the_caller(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        owner = _user_row(session)
        other = _user_row(session)
        _, questions = _published_lesson(session)
        await session.flush()
        await _card(uow, UserId(owner.id), questions[0].id, due_at=_now() - timedelta(days=1))

        page = await service.due_cards(UserId(other.id), limit=20)

        assert page.cards == ()

    async def test_preview_counts_without_loading_the_queue(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        _, questions = _published_lesson(session, questions=2)
        await session.flush()
        await _card(uow, UserId(user.id), questions[0].id, due_at=_now() - timedelta(days=1))
        await _card(uow, UserId(user.id), questions[1].id, due_at=_now() + timedelta(days=1))

        assert (await service.preview(UserId(user.id))).due_count == 1


# ================================================================ answering a card
class TestAnswerCard:
    async def test_a_correct_answer_advances_the_schedule(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        _, questions = _published_lesson(session)
        await session.flush()
        card = await _card(uow, UserId(user.id), questions[0].id, due_at=_now() - timedelta(days=1))

        view = await service.answer_card(
            UserId(user.id), card.id, answer=SubmittedAnswer(choice="Hello")
        )

        assert view.is_correct is True
        assert view.quality == 5
        assert view.points_awarded == 1
        assert view.interval_days == 1
        assert view.repetitions == 1
        assert view.lapses == 0
        assert view.state == "learning"
        assert view.correct_answer == {"choice": "Hello"}
        assert view.explanation == "Hello is the greeting."
        assert view.due_at > _now()

    async def test_answering_persists_the_new_schedule(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        _, questions = _published_lesson(session)
        await session.flush()
        card = await _card(
            uow,
            UserId(user.id),
            questions[0].id,
            due_at=_now() - timedelta(days=1),
            repetitions=2,
            interval_days=6,
            state=ReviewState.REVIEW,
        )
        user_id = UserId(user.id)

        await service.answer_card(user_id, card.id, answer=SubmittedAnswer(choice="Hello"))

        reloaded = await uow.review_cards.get_for_user(card.id, user_id)
        assert reloaded is not None
        assert reloaded.interval_days == 15
        assert reloaded.repetitions == 3
        assert reloaded.state is ReviewState.REVIEW

    async def test_answering_appends_a_log_with_both_intervals(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """The card only remembers the latest interval, so the log has to keep the change."""
        user = _user_row(session)
        _, questions = _published_lesson(session)
        await session.flush()
        card = await _card(
            uow,
            UserId(user.id),
            questions[0].id,
            due_at=_now() - timedelta(days=1),
            repetitions=2,
            interval_days=6,
            state=ReviewState.REVIEW,
        )

        await service.answer_card(UserId(user.id), card.id, answer=SubmittedAnswer(choice="Hello"))

        stats = await uow.review_logs.stats_for_user(UserId(user.id))
        assert stats.total == 1
        assert stats.correct == 1

    async def test_a_wrong_answer_resets_the_card_to_relearning(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        _, questions = _published_lesson(session)
        await session.flush()
        card = await _card(
            uow,
            UserId(user.id),
            questions[0].id,
            due_at=_now() - timedelta(days=1),
            repetitions=3,
            interval_days=15,
            state=ReviewState.REVIEW,
        )

        view = await service.answer_card(
            UserId(user.id), card.id, answer=SubmittedAnswer(choice="Goodbye")
        )

        assert view.is_correct is False
        assert view.quality == 2
        assert view.interval_days == 1
        assert view.repetitions == 0
        assert view.lapses == 1
        assert view.state == "relearning"

    async def test_another_learners_card_reads_as_absent(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        owner = _user_row(session)
        other = _user_row(session)
        _, questions = _published_lesson(session)
        await session.flush()
        card = await _card(
            uow, UserId(owner.id), questions[0].id, due_at=_now() - timedelta(days=1)
        )

        with pytest.raises(NotFoundError) as raised:
            await service.answer_card(
                UserId(other.id), card.id, answer=SubmittedAnswer(choice="Hello")
            )

        assert raised.value.code is ErrorCode.NOT_FOUND

    async def test_an_unknown_card_is_not_found(
        self, service: ReviewService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.answer_card(
                UserId(user.id), uuid.uuid4(), answer=SubmittedAnswer(choice="Hello")
            )

        assert raised.value.code is ErrorCode.NOT_FOUND

    async def test_a_card_whose_question_is_unpublished_cannot_be_answered(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """Nothing to grade against, so the answer is refused rather than guessed at."""
        user = _user_row(session)
        _, questions = _published_lesson(session, lesson_state=_DRAFT)
        await session.flush()
        card = await _card(uow, UserId(user.id), questions[0].id, due_at=_now() - timedelta(days=1))

        with pytest.raises(NotFoundError) as raised:
            await service.answer_card(
                UserId(user.id), card.id, answer=SubmittedAnswer(choice="Hello")
            )

        assert raised.value.code is ErrorCode.QUESTION_NOT_FOUND


# ==================================================================== the figures
class TestStats:
    async def test_a_learner_with_no_cards_reports_zero_everything(
        self, service: ReviewService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        await session.flush()

        view = await service.stats(UserId(user.id))

        assert view.total_cards == 0
        assert (view.due_today, view.future) == (0, 0)
        assert view.reviewed_total == 0
        assert view.retention_percent == 0

    async def test_it_splits_cards_between_due_now_and_ahead(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        _, questions = _published_lesson(session, questions=3)
        await session.flush()
        await _card(uow, UserId(user.id), questions[0].id, due_at=_now() - timedelta(days=1))
        await _card(uow, UserId(user.id), questions[1].id, due_at=_now() - timedelta(hours=1))
        await _card(uow, UserId(user.id), questions[2].id, due_at=_now() + timedelta(days=2))

        view = await service.stats(UserId(user.id))

        assert view.total_cards == 3
        assert view.due_today == 2
        assert view.future == 1

    async def test_retention_is_derived_from_the_log(
        self, service: ReviewService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """One right, one wrong: the figure has to come from what was recorded."""
        user = _user_row(session)
        _, questions = _published_lesson(session, questions=2)
        await session.flush()
        first = await _card(
            uow, UserId(user.id), questions[0].id, due_at=_now() - timedelta(days=1)
        )
        second = await _card(
            uow, UserId(user.id), questions[1].id, due_at=_now() - timedelta(days=1)
        )
        user_id = UserId(user.id)
        await service.answer_card(user_id, first.id, answer=SubmittedAnswer(choice="Hello"))
        await service.answer_card(user_id, second.id, answer=SubmittedAnswer(choice="Goodbye"))

        view = await service.stats(user_id)

        assert view.reviewed_total == 2
        assert view.retention_percent == 50


# ============================================================== the engine's hook
class TestCorrectAnswersSeedCards:
    async def test_a_correct_answer_creates_one_card_due_tomorrow(
        self, quiz: QuizService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user = _user_row(session)
        lesson, questions = _published_lesson(session)
        await session.flush()
        user_id = UserId(user.id)

        detail = await quiz.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson.id)
        await quiz.submit_answer(
            user_id,
            detail.session.id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),
        )

        card = await uow.review_cards.get_for_question(user_id, questions[0].id)
        assert card is not None
        assert card.state is ReviewState.NEW
        assert card.repetitions == 0
        assert card.due_at > _now() + timedelta(hours=23)

    async def test_a_second_correct_answer_does_not_add_a_second_card(
        self, quiz: QuizService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """The unique constraint is the write half of "one card per question"."""
        user = _user_row(session)
        lesson, questions = _published_lesson(session)
        await session.flush()
        user_id = UserId(user.id)

        for _ in range(2):
            detail = await quiz.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson.id)
            await quiz.submit_answer(
                user_id,
                detail.session.id,
                question_id=questions[0].id,
                answer=SubmittedAnswer(choice="Hello"),
            )

        cards = await uow.review_cards.list_for_user(user_id)
        assert len(cards) == 1

    async def test_a_wrong_answer_seeds_nothing(
        self, quiz: QuizService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """A question the learner has not recalled is not worth scheduling yet."""
        user = _user_row(session)
        lesson, questions = _published_lesson(session)
        await session.flush()
        user_id = UserId(user.id)

        detail = await quiz.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson.id)
        await quiz.submit_answer(
            user_id,
            detail.session.id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),
        )

        assert await uow.review_cards.list_for_user(user_id) == []
