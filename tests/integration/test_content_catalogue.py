"""Catalogue service tests against a real SQLite schema with the real repositories.

The things that only break against a real database, and which a hand-written fake would
have asserted away, are all in here:

- the keyset predicate ``(position, id) > after`` translated by a real SQL dialect;
- the composite catalogue indexes actually being usable;
- the ``UNIQUE (parent_id, position)`` and ``CHECK`` guards;
- ``ORDER BY`` stability across pages.

Visibility is the other theme. Every listing has to hide drafts, and every "not found"
has to be indistinguishable between "does not exist" and "not published yet" ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â otherwise
the public catalogue becomes a map of the editorial pipeline.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.errors import ErrorCode, NotFoundError
from app.core.pagination import (
    PositionCursor,
    decode_position_cursor,
    encode_position_cursor,
)
from app.core.registry import import_all_models
from app.core.types import CEFRLevel, ContentId, ContentState, QuestionKind, SkillKind
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.content.application.catalogue_service import CatalogueService
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest.fixture
def service(uow: SqlAlchemyUnitOfWork) -> CatalogueService:
    return CatalogueService(uow)


@pytest.fixture
def session(uow: SqlAlchemyUnitOfWork) -> AsyncSession:
    return uow.session


# ----------------------------------------------------------------- row builders
def _level_row(
    session: AsyncSession,
    *,
    code: str = "A1",
    title: str = "Beginner",
    position: int = 1,
    state: str = ContentState.PUBLISHED.value,
) -> LevelModel:
    row = LevelModel(id=uuid.uuid4(), code=code, title=title, position=position, state=state)
    session.add(row)
    return row


def _unit_row(
    session: AsyncSession,
    level_id: uuid.UUID,
    *,
    title: str = "Introductions",
    position: int = 1,
    state: str = ContentState.PUBLISHED.value,
) -> UnitModel:
    row = UnitModel(id=uuid.uuid4(), level_id=level_id, title=title, position=position, state=state)
    session.add(row)
    return row


def _lesson_row(
    session: AsyncSession,
    unit_id: uuid.UUID,
    *,
    title: str = "Greetings",
    position: int = 1,
    estimated_minutes: int = 8,
    state: str = ContentState.PUBLISHED.value,
) -> LessonModel:
    row = LessonModel(
        id=uuid.uuid4(),
        unit_id=unit_id,
        title=title,
        summary=f"About {title}.",
        position=position,
        estimated_minutes=estimated_minutes,
        state=state,
    )
    session.add(row)
    return row


def _question_row(
    session: AsyncSession,
    lesson_id: uuid.UUID,
    *,
    position: int = 1,
    kind: str = QuestionKind.MULTIPLE_CHOICE.value,
    skill: str = SkillKind.VOCABULARY.value,
    prompt: str = "Which one means 'hello'?",
    choices: list[str] | None = None,
    accepted_answers: list[str] | None = None,
    correct_index: int | None = 0,
    explanation: str | None = "A greeting.",
    image_url: str | None = None,
    audio_url: str | None = None,
    video_url: str | None = None,
    points: int = 1,
) -> QuestionModel:
    row = QuestionModel(
        id=uuid.uuid4(),
        lesson_id=lesson_id,
        position=position,
        kind=kind,
        skill=skill,
        prompt=prompt,
        choices=["Hello", "Goodbye", "Thank you"] if choices is None else choices,
        accepted_answers=accepted_answers or [],
        correct_index=correct_index,
        explanation=explanation,
        image_url=image_url,
        audio_url=audio_url,
        video_url=video_url,
        points=points,
    )
    session.add(row)
    return row


# ================================================================== list levels
class TestListLevels:
    async def test_published_levels_come_back_in_curriculum_order(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        _level_row(session, code="B1", title="Intermediate", position=3)
        _level_row(session, code="A1", title="Beginner", position=1)
        _level_row(session, code="A2", title="Elementary", position=2)
        await session.flush()

        page = await service.list_levels(limit=20)

        assert [level.code for level in page.items] == ["A1", "A2", "B1"]
        assert page.has_more is False
        assert page.next_cursor is None

    async def test_drafts_never_appear(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        """A draft in a learner's level list is a broken promise about what they can
        study, so this is the assertion the whole slice is built around."""
        _level_row(session, code="A1", position=1)
        _level_row(session, code="A2", position=2, state=ContentState.REVIEW.value)
        _level_row(session, code="B1", position=3, state=ContentState.APPROVED.value)
        _level_row(session, code="B2", position=4, state=ContentState.ARCHIVED.value)
        await session.flush()

        page = await service.list_levels(limit=20)

        assert [level.code for level in page.items] == ["A1"]

    async def test_each_level_reports_its_published_unit_count(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1", position=1)
        _unit_row(session, level.id, title="One", position=1)
        _unit_row(session, level.id, title="Two", position=2)
        _unit_row(session, level.id, title="Draft", position=3, state=ContentState.DRAFT.value)
        await session.flush()

        page = await service.list_levels(limit=20)

        assert page.items[0].unit_count == 2

    async def test_a_level_with_no_units_reports_zero(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        _level_row(session, code="A1", position=1)
        await session.flush()

        page = await service.list_levels(limit=20)

        assert page.items[0].unit_count == 0


# =================================================================== pagination
class TestCursorPaging:
    async def test_paging_walks_every_level_exactly_once(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        """The property that makes a cursor worth its bytes: a full walk of a page loop
        yields each row once, in order, with no gap and no repeat."""
        for index, code in enumerate(["A1", "A2", "B1", "B2", "C1", "C2"], start=1):
            _level_row(session, code=code, position=index)
        await session.flush()

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):  # generous bound: the loop must finish well before this
            page = await service.list_levels(limit=2, cursor=cursor)
            seen.extend(level.code for level in page.items)
            if not page.has_more:
                break
            assert page.next_cursor is not None
            cursor = page.next_cursor

        assert seen == ["A1", "A2", "B1", "B2", "C1", "C2"]

    async def test_the_final_page_reports_no_more_and_no_cursor(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        for index, code in enumerate(["A1", "A2", "B1"], start=1):
            _level_row(session, code=code, position=index)
        await session.flush()

        page = await service.list_levels(limit=3)

        assert len(page.items) == 3
        assert page.has_more is False
        assert page.next_cursor is None

    async def test_has_more_is_true_when_a_row_remains(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        for index, code in enumerate(["A1", "A2", "B1"], start=1):
            _level_row(session, code=code, position=index)
        await session.flush()

        page = await service.list_levels(limit=2)

        assert len(page.items) == 2
        assert page.has_more is True
        assert page.next_cursor is not None

    async def test_the_cursor_is_the_last_delivered_row(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        """Not the first row of the next page. Getting this off by one is the classic
        cursor bug: the boundary row is either skipped or served twice."""
        first = _level_row(session, code="A1", position=1)
        _level_row(session, code="A2", position=2)
        await session.flush()

        page = await service.list_levels(limit=1)

        assert page.next_cursor is not None, "a full page must hand back a cursor"
        assert decode_position_cursor(page.next_cursor) == PositionCursor(
            position=first.position, id=first.id
        )

    async def test_a_cursor_past_the_end_yields_an_empty_page(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        _level_row(session, code="A1", position=1)
        await session.flush()
        cursor = encode_position_cursor(PositionCursor(position=99, id=uuid.uuid4()))

        page = await service.list_levels(limit=20, cursor=cursor)

        assert page.items == []
        assert page.has_more is False


# =================================================================== list units
class TestListUnits:
    async def test_units_come_back_in_position_order(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        _unit_row(session, level.id, title="Third", position=3)
        _unit_row(session, level.id, title="First", position=1)
        _unit_row(session, level.id, title="Second", position=2)
        await session.flush()

        page = await service.list_units(CEFRLevel.A1, limit=20)

        assert [unit.title for unit in page.items] == ["First", "Second", "Third"]

    async def test_draft_units_are_hidden(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        _unit_row(session, level.id, title="Live", position=1)
        _unit_row(session, level.id, title="Draft", position=2, state=ContentState.DRAFT.value)
        await session.flush()

        page = await service.list_units(CEFRLevel.A1, limit=20)

        assert [unit.title for unit in page.items] == ["Live"]

    async def test_units_of_other_levels_are_not_mixed_in(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        a1 = _level_row(session, code="A1", position=1)
        a2 = _level_row(session, code="A2", position=2)
        _unit_row(session, a1.id, title="A1 unit", position=1)
        _unit_row(session, a2.id, title="A2 unit", position=1)
        await session.flush()

        page = await service.list_units(CEFRLevel.A1, limit=20)

        assert [unit.title for unit in page.items] == ["A1 unit"]

    async def test_each_unit_reports_its_published_lesson_count(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id, position=1)
        _lesson_row(session, unit.id, position=1)
        _lesson_row(session, unit.id, position=2)
        _lesson_row(session, unit.id, position=3, state=ContentState.DRAFT.value)
        await session.flush()

        page = await service.list_units(CEFRLevel.A1, limit=20)

        assert page.items[0].lesson_count == 2

    async def test_a_draft_level_reports_not_found(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        """A draft level must be indistinguishable from one that was never created.
        Otherwise the endpoint is a public map of the editorial pipeline."""
        _level_row(session, code="A1", state=ContentState.DRAFT.value)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.list_units(CEFRLevel.A1, limit=20)

        assert raised.value.code is ErrorCode.LEVEL_NOT_FOUND

    async def test_an_unknown_level_reports_the_same_error(self, service: CatalogueService) -> None:
        with pytest.raises(NotFoundError) as raised:
            await service.list_units(CEFRLevel.C2, limit=20)

        assert raised.value.code is ErrorCode.LEVEL_NOT_FOUND

    async def test_units_page_by_cursor(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        for index in range(1, 4):
            _unit_row(session, level.id, title=f"Unit {index}", position=index)
        await session.flush()

        first = await service.list_units(CEFRLevel.A1, limit=2)
        second = await service.list_units(CEFRLevel.A1, limit=2, cursor=first.next_cursor)

        assert [unit.title for unit in first.items] == ["Unit 1", "Unit 2"]
        assert [unit.title for unit in second.items] == ["Unit 3"]
        assert second.has_more is False


# ================================================================= list lessons
class TestListLessons:
    async def test_lessons_come_back_in_position_order(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        _lesson_row(session, unit.id, title="Third", position=3)
        _lesson_row(session, unit.id, title="First", position=1)
        _lesson_row(session, unit.id, title="Second", position=2)
        await session.flush()

        page = await service.list_lessons(unit.id, limit=20)

        assert [lesson.title for lesson in page.items] == ["First", "Second", "Third"]

    async def test_draft_lessons_are_hidden(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        _lesson_row(session, unit.id, title="Live", position=1)
        _lesson_row(session, unit.id, title="Draft", position=2, state=ContentState.REVIEW.value)
        await session.flush()

        page = await service.list_lessons(unit.id, limit=20)

        assert [lesson.title for lesson in page.items] == ["Live"]

    async def test_each_lesson_reports_its_question_count(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        _question_row(session, lesson.id, position=1)
        _question_row(session, lesson.id, position=2)
        await session.flush()

        page = await service.list_lessons(unit.id, limit=20)

        assert page.items[0].question_count == 2

    async def test_a_draft_unit_reports_not_found(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id, state=ContentState.DRAFT.value)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.list_lessons(unit.id, limit=20)

        assert raised.value.code is ErrorCode.UNIT_NOT_FOUND

    async def test_an_unknown_unit_reports_not_found(self, service: CatalogueService) -> None:
        with pytest.raises(NotFoundError) as raised:
            await service.list_lessons(uuid.uuid4(), limit=20)

        assert raised.value.code is ErrorCode.UNIT_NOT_FOUND

    async def test_lessons_page_by_cursor(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        for index in range(1, 4):
            _lesson_row(session, unit.id, title=f"Lesson {index}", position=index)
        await session.flush()

        first = await service.list_lessons(unit.id, limit=2)
        second = await service.list_lessons(unit.id, limit=2, cursor=first.next_cursor)

        assert [lesson.title for lesson in first.items] == ["Lesson 1", "Lesson 2"]
        assert [lesson.title for lesson in second.items] == ["Lesson 3"]


# =============================================================== get one lesson
class TestGetLesson:
    async def test_the_detail_carries_its_questions_in_order(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        _question_row(session, lesson.id, position=3, prompt="Third")
        _question_row(session, lesson.id, position=1, prompt="First")
        _question_row(session, lesson.id, position=2, prompt="Second")
        await session.flush()

        detail = await service.get_lesson(lesson.id)

        assert detail.title == "Greetings"
        assert [question.prompt for question in detail.questions] == [
            "First",
            "Second",
            "Third",
        ]

    async def test_the_detail_does_not_carry_the_answer_key(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        """The most important assertion in this file.

        The catalogue is public and unauthenticated, so anything the response model
        forgets to exclude is published for every caller. The answer key is the reason
        a public read model has to be built by omission rather than by copying the
        aggregate.
        """
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        _question_row(session, lesson.id, position=1, correct_index=2, explanation="Because.")
        await session.flush()

        detail = await service.get_lesson(lesson.id)
        question = detail.questions[0]

        for leaked in ("correct_index", "accepted_answers", "explanation"):
            assert not hasattr(question, leaked), f"{leaked} must not reach a public payload"

    async def test_the_detail_carries_the_visual_aid(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        """The image travels with the public question: it teaches the word before an
        attempt, so unlike the explanation it is not part of the answer key."""
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        _question_row(session, lesson.id, position=1, image_url="https://cdn.test/hello.png")
        await session.flush()

        detail = await service.get_lesson(lesson.id)

        assert detail.questions[0].image_url == "https://cdn.test/hello.png"

    async def test_a_question_without_an_image_reports_none(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        _question_row(session, lesson.id, position=1)
        await session.flush()

        detail = await service.get_lesson(lesson.id)

        assert detail.questions[0].image_url is None

    async def test_the_detail_carries_the_audio_aid(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        """The audio aid travels with the public question for the same reason as the
        image: it pronounces the taught word before an attempt rather than revealing it."""
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        _question_row(session, lesson.id, position=1, audio_url="https://cdn.test/hello.mp3")
        await session.flush()

        detail = await service.get_lesson(lesson.id)

        assert detail.questions[0].audio_url == "https://cdn.test/hello.mp3"

    async def test_a_question_without_audio_reports_none(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        _question_row(session, lesson.id, position=1)
        await session.flush()

        detail = await service.get_lesson(lesson.id)

        assert detail.questions[0].audio_url is None


    async def test_the_detail_carries_the_video_aid(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        _question_row(session, lesson.id, position=1, video_url="https://cdn.test/hello.mp4")
        await session.flush()

        detail = await service.get_lesson(lesson.id)

        assert detail.questions[0].video_url == "https://cdn.test/hello.mp4"

    async def test_a_question_without_video_reports_none(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        _question_row(session, lesson.id, position=1)
        await session.flush()

        detail = await service.get_lesson(lesson.id)

        assert detail.questions[0].video_url is None
    async def test_a_free_text_question_still_exposes_its_choices_shape(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        """Free-text items carry no choices, and the client needs an empty list rather
        than a missing key to render the right input."""
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        _question_row(
            session,
            lesson.id,
            position=1,
            kind=QuestionKind.FILL_BLANK.value,
            prompt="She ___ to school.",
            choices=[],
            accepted_answers=["goes"],
            correct_index=None,
        )
        await session.flush()

        detail = await service.get_lesson(lesson.id)

        assert detail.questions[0].choices == ()
        assert detail.questions[0].kind == "fill_blank"

    async def test_a_draft_lesson_reports_not_found(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, state=ContentState.APPROVED.value)
        _question_row(session, lesson.id, position=1)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.get_lesson(lesson.id)

        assert raised.value.code is ErrorCode.LESSON_NOT_FOUND

    async def test_an_unknown_lesson_reports_not_found(self, service: CatalogueService) -> None:
        with pytest.raises(NotFoundError) as raised:
            await service.get_lesson(uuid.uuid4())

        assert raised.value.code is ErrorCode.LESSON_NOT_FOUND

    async def test_a_published_lesson_with_no_questions_is_still_a_page(
        self, service: CatalogueService, session: AsyncSession
    ) -> None:
        """An empty question list is a content gap, not an error: the lesson is real and
        a client should render it rather than receive a 404."""
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, position=1)
        await session.flush()

        detail = await service.get_lesson(lesson.id)

        assert detail.questions == ()
        assert detail.question_count == 0


class TestQuestionRepository:
    """The repository behind ``get_lesson``, exercised on its own boundary.

    The service cannot reach the empty-input branch: it 404s long before it would ask for
    zero lessons, so the only way to pin the guard down is to call the repository
    directly.
    """

    async def test_asking_for_no_lessons_queries_nothing(self, uow: SqlAlchemyUnitOfWork) -> None:
        """An empty id list must not become ``WHERE lesson_id IN ()``.

        The guard exists so the branch stays correct without relying on how a given dialect
        renders an empty ``IN``; the behaviour it promises is simply "no questions", and
        that is what a caller gets.
        """
        assert await uow.questions.list_for_lessons([]) == []

    async def test_get_published_returns_the_aggregate_for_a_published_lesson(
        self, uow: SqlAlchemyUnitOfWork, session: AsyncSession
    ) -> None:
        """The state predicate lives in the query (see the repository docstring for why
        a ``get_published_*`` that can return a draft would be a trap)."""
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id)
        row = _question_row(session, lesson.id, position=1, explanation="Bye is the farewell.")
        await session.flush()

        question = await uow.questions.get_published(ContentId(row.id))

        assert question is not None
        assert question.explanation == "Bye is the farewell."

    async def test_get_published_hides_a_question_under_a_draft_lesson(
        self, uow: SqlAlchemyUnitOfWork, session: AsyncSession
    ) -> None:
        level = _level_row(session, code="A1")
        unit = _unit_row(session, level.id)
        lesson = _lesson_row(session, unit.id, state=ContentState.DRAFT.value)
        row = _question_row(session, lesson.id, position=1)
        await session.flush()

        assert await uow.questions.get_published(ContentId(row.id)) is None
