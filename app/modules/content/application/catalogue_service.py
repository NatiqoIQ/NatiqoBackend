"""Read-only use cases for the public catalogue.

Five entry points: browse levels, browse a level's units, browse a unit's lessons, open
a lesson, and read a question's post-attempt explanation. Each one applies the same two
rules, so they are written as one rule applied five times rather than five separate
rules:

**Only published content is visible.** The repositories already filter on state and this
layer never asks for anything else, so a draft cannot reach a learner. When something is
missing *or* unpublished the result is a 404, never a 403 — a 403 would answer a
question a learner should not be able to ask ("does this exist but is not ready yet?")
and turn the public API into a progress bar for the editorial pipeline.

**Counts cost one query per screen, not one per row.** A curriculum browser that shows
"12 lessons" cannot afford a per-row count, so the ``count_*_by_*`` reads are answered
by a single grouped aggregate regardless of page size.

``limit`` is expected to have been range-checked by the caller (the route does it with a
``Query`` constraint). This layer is not the boundary for that.
"""

from __future__ import annotations

import uuid
from collections import defaultdict

from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.pagination import (
    InvalidCursor,
    PageResult,
    PositionCursor,
    decode_position_cursor,
    paginate,
)
from app.core.types import CEFRLevel, ContentId
from app.core.uow import UnitOfWorkProtocol
from app.modules.content.application.dto import (
    LessonDetailView,
    LessonSummaryView,
    LevelView,
    QuestionExplanationView,
    QuestionView,
    UnitView,
)
from app.modules.content.domain.entities import Lesson, Level, Question, Unit


def _key(row: Level | Unit | Lesson) -> tuple[int, uuid.UUID]:
    return (row.position, row.id)


class CatalogueService:
    def __init__(self, uow: UnitOfWorkProtocol) -> None:
        self._uow = uow

    # ------------------------------------------------------------------ levels
    async def list_levels(self, *, limit: int, cursor: str | None = None) -> PageResult[LevelView]:
        after = _decode(cursor)
        unit_counts = await self._uow.units.count_published_by_level()
        page = paginate(
            await self._uow.levels.list_published(limit=limit + 1, after=after),
            limit=limit,
            key_of=_key,
        )
        return PageResult(
            items=[
                LevelView(
                    id=level.id,
                    code=level.code.value,
                    title=level.title,
                    description=level.description,
                    position=level.position,
                    unit_count=unit_counts.get(level.id, 0),
                )
                for level in page.items
            ],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    # ------------------------------------------------------------------- units
    async def list_units(
        self, code: str | CEFRLevel, *, limit: int, cursor: str | None = None
    ) -> PageResult[UnitView]:
        """Published units of one level, addressed by its public CEFR code.

        ``code`` is accepted as a plain string on purpose. A route that typed the path
        parameter as ``CEFRLevel`` would have FastAPI reject an unknown code with a 422
        *before* this method ran, which would make "no such level" distinguishable from
        "a level that exists but is not published" — the exact leak the 404 rule below
        exists to close. Parsing here keeps both cases on one path.
        """
        resolved = _parse_code(code)
        level = (
            await self._uow.levels.get_published_by_code(resolved) if resolved is not None else None
        )
        if level is None:
            raise NotFoundError(
                f"There is no published level {_code_text(code)}.",
                code=ErrorCode.LEVEL_NOT_FOUND,
            )
        after = _decode(cursor)
        lesson_counts = await self._uow.lessons.count_published_by_unit()
        page = paginate(
            await self._uow.units.list_published_for_level(level.id, limit=limit + 1, after=after),
            limit=limit,
            key_of=_key,
        )
        return PageResult(
            items=[
                UnitView(
                    id=unit.id,
                    level_code=level.code.value,
                    title=unit.title,
                    description=unit.description,
                    position=unit.position,
                    lesson_count=lesson_counts.get(unit.id, 0),
                )
                for unit in page.items
            ],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    # ----------------------------------------------------------------- lessons
    async def list_lessons(
        self, unit_id: uuid.UUID, *, limit: int, cursor: str | None = None
    ) -> PageResult[LessonSummaryView]:
        unit = await self._uow.units.get_published(ContentId(unit_id))
        if unit is None:
            raise NotFoundError(
                "There is no published unit with that id.", code=ErrorCode.UNIT_NOT_FOUND
            )
        after = _decode(cursor)
        page = paginate(
            await self._uow.lessons.list_published_for_unit(unit.id, limit=limit + 1, after=after),
            limit=limit,
            key_of=_key,
        )
        # One query for the whole page's questions, then bucketed onto their lessons —
        # rather than a count query per lesson, which would make the screen scale with
        # the page size.
        questions = await self._uow.questions.list_for_lessons([lesson.id for lesson in page.items])
        counts: dict[ContentId, int] = defaultdict(int)
        for question in questions:
            counts[question.lesson_id] += 1
        return PageResult(
            items=[_lesson_summary(lesson, counts.get(lesson.id, 0)) for lesson in page.items],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    async def get_lesson(self, lesson_id: uuid.UUID) -> LessonDetailView:
        lesson = await self._uow.lessons.get_published(ContentId(lesson_id))
        if lesson is None:
            raise NotFoundError(
                "There is no published lesson with that id.", code=ErrorCode.LESSON_NOT_FOUND
            )
        questions = await self._uow.questions.list_for_lessons([lesson.id])
        # Built field by field rather than with ``dataclasses.replace``: ``replace``
        # rebuilds ``type(obj)``, which here is the summary, so the extra
        # ``questions`` field would be rejected.
        return LessonDetailView(
            id=lesson.id,
            unit_id=lesson.unit_id,
            title=lesson.title,
            summary=lesson.summary,
            position=lesson.position,
            estimated_minutes=lesson.estimated_minutes,
            question_count=len(questions),
            questions=tuple(_question_view(question) for question in questions),
        )

    async def get_question_explanation(self, question_id: uuid.UUID) -> QuestionExplanationView:
        """The rationale behind one question — the single support aid a learner gets.

        Unlike :meth:`get_lesson`, this deliberately *does* return the explanation. It
        is the one surface that reveals an answer, so it is a separate endpoint rather
        than a field nobody asked for on the lesson payload: the lesson stays safe
        until the client decides the learner has answered and asks here.
        """
        question = await self._uow.questions.get_published(ContentId(question_id))
        if question is None:
            raise NotFoundError(
                "There is no published question with that id.", code=ErrorCode.QUESTION_NOT_FOUND
            )
        return QuestionExplanationView(id=question.id, explanation=question.explanation)


# ------------------------------------------------------------------- internals
def _parse_code(code: str | CEFRLevel) -> CEFRLevel | None:
    """A code that is not CEFR is treated as *absent* rather than *invalid*.

    Returning ``None`` keeps an unparsable code on the same 404 as a level that was never
    created, so the endpoint gives away nothing about which CEFR codes exist.
    """
    if isinstance(code, CEFRLevel):
        return code
    try:
        return CEFRLevel(code.strip().upper())
    except ValueError:
        return None


def _code_text(code: str | CEFRLevel) -> str:
    return code.value if isinstance(code, CEFRLevel) else code


def _decode(cursor: str | None) -> PositionCursor | None:
    """Turn an opaque cursor into a sort key, or ``None`` for the first page.

    A malformed cursor is a 422 rather than something we repair. Guessing a position
    would silently skip content, and a learner seeing a gap has no way to tell that from
    a bug.
    """
    if cursor is None:
        return None
    try:
        return decode_position_cursor(cursor)
    except InvalidCursor as exc:
        raise ValidationFailed(
            "The cursor is not usable. Start again from the first page.",
            details={"field": "cursor"},
        ) from exc


def _lesson_summary(lesson: Lesson, question_count: int) -> LessonSummaryView:
    return LessonSummaryView(
        id=lesson.id,
        unit_id=lesson.unit_id,
        title=lesson.title,
        summary=lesson.summary,
        position=lesson.position,
        estimated_minutes=lesson.estimated_minutes,
        question_count=question_count,
    )


def _question_view(question: Question) -> QuestionView:
    """Project a question aggregate onto what a learner may see *before* answering.

    See the docstring in ``dto.py``: this projection is a security boundary, and the
    fields left off it are the answer key.
    """
    return QuestionView(
        id=question.id,
        kind=question.kind,
        skill=question.skill,
        prompt=question.prompt,
        choices=question.choices,
        points=question.points,
    )


__all__ = ["CatalogueService"]
