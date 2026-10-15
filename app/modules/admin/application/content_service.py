"""Authoring use cases for the curriculum.

The write half of the catalogue. Every method here follows one shape: load the row, apply
the domain rule, persist, write an audit row, commit — all in the caller's unit of work, so
the change and the record of who made it either both land or neither does. A mutation that
committed without its audit row would leave a trail with a hole exactly where the useful
evidence is.

Three rules recur, so they are stated once here rather than in each method:

**NotFound is a 404, not a 403.** An editor asking for an id that was never issued and one
asking for a draft they may not see get the same answer, for the same reason the public
catalogue does: the difference is a hint about the editorial pipeline.

**Illegal editorial moves are a 409.** The lifecycle (``draft → review → approved →
published``) and the parent-published rules live on the aggregates; this layer only
translates :class:`InvalidContentState` into ``INVALID_STATE_TRANSITION``. So a lesson cannot
be published before its unit, and a unit before its level, no matter which endpoint is used.

**A taken sibling position is a 409, not a 500.** Positions are unique at the database
level; asking first gives a clean answer instead of letting a constraint violation surface as
a server error. Positions are auto-assigned (``max + 1``) when the caller omits one, because
forcing a client to compute the next slot is how two editors race onto the same number.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.core.errors import (
    ConflictError,
    ErrorCode,
    NotFoundError,
    ValidationFailed,
)
from app.core.pagination import (
    InvalidCursor,
    PageResult,
    PositionCursor,
    decode_position_cursor,
    paginate,
)
from app.core.types import ContentId, ContentState, UserId, new_id
from app.core.uow import UnitOfWorkProtocol
from app.modules.admin.application.dto import (
    LessonAdminDetailView,
    LessonAdminView,
    LessonCreateCommand,
    LessonUpdateCommand,
    LevelAdminView,
    LevelCreateCommand,
    LevelUpdateCommand,
    QuestionAdminView,
    QuestionCreateCommand,
    QuestionUpdateCommand,
    UnitAdminView,
    UnitCreateCommand,
    UnitUpdateCommand,
    is_given,
)
from app.modules.admin.domain.audit import (
    ENTITY_LESSON,
    ENTITY_LEVEL,
    ENTITY_QUESTION,
    ENTITY_UNIT,
    AuditAction,
    AuditEntry,
)
from app.modules.content.domain.entities import Lesson, Level, Question, Unit
from app.modules.content.domain.policy import (
    InvalidContentState,
    question_shape_problems,
)


def _key(row: Level | Unit | Lesson) -> tuple[int, uuid.UUID]:
    return (row.position, row.id)


def _decode(cursor: str | None) -> PositionCursor | None:
    if cursor is None:
        return None
    try:
        return decode_position_cursor(cursor)
    except InvalidCursor as exc:
        raise ValidationFailed(
            "The cursor is not usable. Start again from the first page.",
            details={"field": "cursor"},
        ) from exc


class AdminContentService:
    def __init__(self, uow: UnitOfWorkProtocol) -> None:
        self._uow = uow

    # ------------------------------------------------------------------ levels
    async def list_levels(
        self, *, limit: int, cursor: str | None = None
    ) -> PageResult[LevelAdminView]:
        after = _decode(cursor)
        page = paginate(
            await self._uow.editorial.list_levels(limit=limit + 1, after=after),
            limit=limit,
            key_of=_key,
        )
        return PageResult(
            items=[_level_view(level) for level in page.items],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    async def get_level(self, level_id: uuid.UUID) -> LevelAdminView:
        return _level_view(await self._require_level(level_id))

    async def create_level(
        self, actor_id: UserId, command: LevelCreateCommand
    ) -> LevelAdminView:
        _require_text("title", command.title)
        if await self._uow.editorial.get_level_by_code(command.code) is not None:
            raise ConflictError(
                f"A level with code {command.code.value} already exists.",
                code=ErrorCode.CONFLICT,
                details={"field": "code"},
            )
        position = await self._level_position(command.position)
        level = Level(
            id=ContentId(new_id()),
            code=command.code,
            title=command.title.strip(),
            description=command.description,
            position=position,
        )
        await self._uow.editorial.add_level(level)
        await self._audit(
            actor_id,
            AuditAction.LEVEL_CREATE,
            ENTITY_LEVEL,
            level.id,
            f"Created level {level.code.value}.",
            {"code": level.code.value, "position": position},
        )
        await self._uow.commit()
        return _level_view(level)

    async def update_level(
        self, actor_id: UserId, level_id: uuid.UUID, command: LevelUpdateCommand
    ) -> LevelAdminView:
        level = await self._require_level(level_id)
        changes: dict[str, Any] = {}

        if is_given(command.title):
            _require_text("title", command.title)
            level.title = command.title.strip()
            changes["title"] = level.title
        if is_given(command.description):
            level.description = command.description
            changes["description"] = command.description
        if is_given(command.position):
            _require_position(command.position)
            if command.position != level.position:
                await self._assert_level_position_free(command.position, exclude_id=level.id)
                level.position = command.position
                changes["position"] = command.position
        if is_given(command.state) and command.state is not level.state:
            target = command.state
            _apply(lambda: level.transition_to(target))
            changes["state"] = level.state.value

        if changes:
            await self._uow.editorial.save_level(level)
            await self._audit(
                actor_id,
                AuditAction.LEVEL_UPDATE,
                ENTITY_LEVEL,
                level.id,
                f"Updated level {level.code.value}.",
                changes,
            )
            await self._uow.commit()
        return _level_view(level)

    async def delete_level(self, actor_id: UserId, level_id: uuid.UUID) -> None:
        level = await self._require_level(level_id)
        await self._uow.editorial.delete_level(level.id)
        await self._audit(
            actor_id,
            AuditAction.LEVEL_DELETE,
            ENTITY_LEVEL,
            level.id,
            f"Deleted level {level.code.value}.",
            {"code": level.code.value},
        )
        await self._uow.commit()

    # ------------------------------------------------------------------- units
    async def list_units(
        self, level_id: uuid.UUID, *, limit: int, cursor: str | None = None
    ) -> PageResult[UnitAdminView]:
        level = await self._require_level(level_id)
        after = _decode(cursor)
        page = paginate(
            await self._uow.editorial.list_units_for_level(level.id, limit=limit + 1, after=after),
            limit=limit,
            key_of=_key,
        )
        return PageResult(
            items=[_unit_view(unit) for unit in page.items],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    async def get_unit(self, unit_id: uuid.UUID) -> UnitAdminView:
        return _unit_view(await self._require_unit(unit_id))

    async def create_unit(
        self, actor_id: UserId, level_id: uuid.UUID, command: UnitCreateCommand
    ) -> UnitAdminView:
        level = await self._require_level(level_id)
        _require_text("title", command.title)
        position = await self._unit_position(level.id, command.position)
        unit = Unit(
            id=ContentId(new_id()),
            level_id=level.id,
            title=command.title.strip(),
            description=command.description,
            position=position,
        )
        await self._uow.editorial.add_unit(unit)
        await self._audit(
            actor_id,
            AuditAction.UNIT_CREATE,
            ENTITY_UNIT,
            unit.id,
            f"Created unit {unit.title!r} in level {level.code.value}.",
            {"level_id": str(level.id), "position": position},
        )
        await self._uow.commit()
        return _unit_view(unit)

    async def update_unit(
        self, actor_id: UserId, unit_id: uuid.UUID, command: UnitUpdateCommand
    ) -> UnitAdminView:
        unit = await self._require_unit(unit_id)
        changes: dict[str, Any] = {}

        if is_given(command.title):
            _require_text("title", command.title)
            unit.title = command.title.strip()
            changes["title"] = unit.title
        if is_given(command.description):
            unit.description = command.description
            changes["description"] = command.description
        if is_given(command.position):
            _require_position(command.position)
            if command.position != unit.position:
                await self._assert_unit_position_free(unit.level_id, command.position, exclude_id=unit.id)
                unit.position = command.position
                changes["position"] = command.position
        if is_given(command.state) and command.state is not unit.state:
            target = command.state
            if target is ContentState.PUBLISHED:
                parent = await self._require_level(unit.level_id)
                _apply(lambda: unit.publish(parent))
            else:
                _apply(lambda: unit.transition_to(target))
            changes["state"] = unit.state.value

        if changes:
            await self._uow.editorial.save_unit(unit)
            await self._audit(
                actor_id,
                AuditAction.UNIT_UPDATE,
                ENTITY_UNIT,
                unit.id,
                f"Updated unit {unit.title!r}.",
                changes,
            )
            await self._uow.commit()
        return _unit_view(unit)

    async def delete_unit(self, actor_id: UserId, unit_id: uuid.UUID) -> None:
        unit = await self._require_unit(unit_id)
        await self._uow.editorial.delete_unit(unit.id)
        await self._audit(
            actor_id,
            AuditAction.UNIT_DELETE,
            ENTITY_UNIT,
            unit.id,
            f"Deleted unit {unit.title!r}.",
            {"level_id": str(unit.level_id)},
        )
        await self._uow.commit()

    # ----------------------------------------------------------------- lessons
    async def list_lessons(
        self, unit_id: uuid.UUID, *, limit: int, cursor: str | None = None
    ) -> PageResult[LessonAdminView]:
        unit = await self._require_unit(unit_id)
        after = _decode(cursor)
        page = paginate(
            await self._uow.editorial.list_lessons_for_unit(unit.id, limit=limit + 1, after=after),
            limit=limit,
            key_of=_key,
        )
        return PageResult(
            items=[_lesson_view(lesson) for lesson in page.items],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    async def get_lesson(self, lesson_id: uuid.UUID) -> LessonAdminDetailView:
        lesson = await self._require_lesson(lesson_id)
        questions = await self._uow.editorial.list_questions_for_lesson(lesson.id)
        base = _lesson_view(lesson)
        return LessonAdminDetailView(
            id=base.id,
            unit_id=base.unit_id,
            title=base.title,
            summary=base.summary,
            position=base.position,
            estimated_minutes=base.estimated_minutes,
            state=base.state,
            questions=tuple(_question_view(question) for question in questions),
        )

    async def create_lesson(
        self, actor_id: UserId, unit_id: uuid.UUID, command: LessonCreateCommand
    ) -> LessonAdminView:
        unit = await self._require_unit(unit_id)
        _require_text("title", command.title)
        _require_text("summary", command.summary)
        _require_positive("estimated_minutes", command.estimated_minutes)
        position = await self._lesson_position(unit.id, command.position)
        lesson = Lesson(
            id=ContentId(new_id()),
            unit_id=unit.id,
            title=command.title.strip(),
            summary=command.summary.strip(),
            position=position,
            estimated_minutes=command.estimated_minutes,
        )
        await self._uow.editorial.add_lesson(lesson)
        await self._audit(
            actor_id,
            AuditAction.LESSON_CREATE,
            ENTITY_LESSON,
            lesson.id,
            f"Created lesson {lesson.title!r} in unit {unit.id}.",
            {"unit_id": str(unit.id), "position": position},
        )
        await self._uow.commit()
        return _lesson_view(lesson)

    async def update_lesson(
        self, actor_id: UserId, lesson_id: uuid.UUID, command: LessonUpdateCommand
    ) -> LessonAdminView:
        lesson = await self._require_lesson(lesson_id)
        changes: dict[str, Any] = {}

        if is_given(command.title):
            _require_text("title", command.title)
            lesson.title = command.title.strip()
            changes["title"] = lesson.title
        if is_given(command.summary):
            _require_text("summary", command.summary)
            lesson.summary = command.summary.strip()
            changes["summary"] = lesson.summary
        if is_given(command.estimated_minutes):
            _require_positive("estimated_minutes", command.estimated_minutes)
            lesson.estimated_minutes = command.estimated_minutes
            changes["estimated_minutes"] = command.estimated_minutes
        if is_given(command.position):
            _require_position(command.position)
            if command.position != lesson.position:
                await self._assert_lesson_position_free(lesson.unit_id, command.position, exclude_id=lesson.id)
                lesson.position = command.position
                changes["position"] = command.position
        if is_given(command.state) and command.state is not lesson.state:
            target = command.state
            if target is ContentState.PUBLISHED:
                parent = await self._require_unit(lesson.unit_id)
                _apply(lambda: lesson.publish(parent))
            else:
                _apply(lambda: lesson.transition_to(target))
            changes["state"] = lesson.state.value

        if changes:
            await self._uow.editorial.save_lesson(lesson)
            await self._audit(
                actor_id,
                AuditAction.LESSON_UPDATE,
                ENTITY_LESSON,
                lesson.id,
                f"Updated lesson {lesson.title!r}.",
                changes,
            )
            await self._uow.commit()
        return _lesson_view(lesson)

    async def delete_lesson(self, actor_id: UserId, lesson_id: uuid.UUID) -> None:
        lesson = await self._require_lesson(lesson_id)
        await self._uow.editorial.delete_lesson(lesson.id)
        await self._audit(
            actor_id,
            AuditAction.LESSON_DELETE,
            ENTITY_LESSON,
            lesson.id,
            f"Deleted lesson {lesson.title!r}.",
            {"unit_id": str(lesson.unit_id)},
        )
        await self._uow.commit()

    async def publish_lesson(self, actor_id: UserId, lesson_id: uuid.UUID) -> LessonAdminView:
        """Move a lesson to ``published`` — the one editorial move with its own endpoint.

        Each transition needs its parent, so the aggregate is handed the unit rather than
        looking it up: a caller that forgot the parent cannot slip a lesson past the rule.
        """
        lesson = await self._require_lesson(lesson_id)
        unit = await self._require_unit(lesson.unit_id)
        _apply(lambda: lesson.publish(unit))
        await self._uow.editorial.save_lesson(lesson)
        await self._audit(
            actor_id,
            AuditAction.LESSON_PUBLISH,
            ENTITY_LESSON,
            lesson.id,
            f"Published lesson {lesson.title!r}.",
            {"state": lesson.state.value},
        )
        await self._uow.commit()
        return _lesson_view(lesson)

    # --------------------------------------------------------------- questions
    async def create_question(
        self, actor_id: UserId, lesson_id: uuid.UUID, command: QuestionCreateCommand
    ) -> QuestionAdminView:
        lesson = await self._require_lesson(lesson_id)
        position = await self._question_position(lesson.id, command.position)
        question = self._build_question(lesson.id, position, command)
        await self._uow.editorial.add_question(question)
        await self._audit(
            actor_id,
            AuditAction.QUESTION_CREATE,
            ENTITY_QUESTION,
            question.id,
            f"Created a question in lesson {lesson.id}.",
            {"lesson_id": str(lesson.id), "position": position, "kind": question.kind.value},
        )
        await self._uow.commit()
        return _question_view(question)

    async def update_question(
        self, actor_id: UserId, question_id: uuid.UUID, command: QuestionUpdateCommand
    ) -> QuestionAdminView:
        current = await self._require_question(question_id)
        changes: dict[str, Any] = {}

        position = current.position
        if is_given(command.position) and command.position != current.position:
            _require_position(command.position)
            await self._assert_question_position_free(current.lesson_id, command.position, exclude_id=current.id)
            position = command.position
            changes["position"] = position

        # A question is frozen, so an edit is a rebuild: merge the provided fields onto the
        # stored ones and re-validate the whole item. Re-validating matters because a change
        # to one field can invalidate another — switching to ``fill_blank`` must drop a
        # ``correct_index``, and switching to ``multiple_choice`` must supply choices.
        merged = QuestionCreateCommand(
            kind=command.kind if is_given(command.kind) else current.kind,
            skill=command.skill if is_given(command.skill) else current.skill,
            prompt=command.prompt if is_given(command.prompt) else current.prompt,
            choices=command.choices if is_given(command.choices) else current.choices,
            accepted_answers=(
                command.accepted_answers
                if is_given(command.accepted_answers)
                else current.accepted_answers
            ),
            correct_index=(
                command.correct_index if is_given(command.correct_index) else current.correct_index
            ),
            explanation=(
                command.explanation if is_given(command.explanation) else current.explanation
            ),
            image_url=command.image_url if is_given(command.image_url) else current.image_url,
            audio_url=command.audio_url if is_given(command.audio_url) else current.audio_url,
            points=command.points if is_given(command.points) else current.points,
            position=position,
        )
        question = self._build_question(current.lesson_id, position, merged, question_id=current.id)

        for name in (
            "kind",
            "skill",
            "prompt",
            "choices",
            "accepted_answers",
            "correct_index",
            "explanation",
            "image_url",
            "audio_url",
            "points",
        ):
            if is_given(getattr(command, name)):
                changes[name] = getattr(merged, name)

        await self._uow.editorial.save_question(question)
        if changes:
            await self._audit(
                actor_id,
                AuditAction.QUESTION_UPDATE,
                ENTITY_QUESTION,
                question.id,
                f"Updated a question in lesson {question.lesson_id}.",
                changes,
            )
        await self._uow.commit()
        return _question_view(question)

    async def delete_question(self, actor_id: UserId, question_id: uuid.UUID) -> None:
        question = await self._require_question(question_id)
        await self._uow.editorial.delete_question(question.id)
        await self._audit(
            actor_id,
            AuditAction.QUESTION_DELETE,
            ENTITY_QUESTION,
            question.id,
            f"Deleted a question in lesson {question.lesson_id}.",
            {"lesson_id": str(question.lesson_id)},
        )
        await self._uow.commit()

    async def reorder_questions(
        self, actor_id: UserId, lesson_id: uuid.UUID, ordered_ids: list[uuid.UUID]
    ) -> tuple[QuestionAdminView, ...]:
        """Rewrite a lesson's question order in one move.

        The new order must be a complete permutation of the lesson's questions: a partial
        list would leave the unmentioned questions at positions the reorder just overwrote,
        which is how a lesson silently loses an item. Anything else is a 422.
        """
        lesson = await self._require_lesson(lesson_id)
        existing = await self._uow.editorial.list_questions_for_lesson(lesson.id)
        existing_ids = {question.id for question in existing}
        if len(ordered_ids) != len(existing_ids) or set(ordered_ids) != existing_ids:
            raise ValidationFailed(
                "The reorder must list every question in the lesson exactly once.",
                details={"field": "question_ids"},
            )
        await self._uow.editorial.reorder_questions(
            lesson.id, [ContentId(question_id) for question_id in ordered_ids]
        )
        await self._audit(
            actor_id,
            AuditAction.LESSON_REORDER_QUESTIONS,
            ENTITY_LESSON,
            lesson.id,
            f"Reordered {len(ordered_ids)} questions in lesson {lesson.id}.",
            {"question_ids": [str(question_id) for question_id in ordered_ids]},
        )
        await self._uow.commit()
        refreshed = await self._uow.editorial.list_questions_for_lesson(lesson.id)
        return tuple(_question_view(question) for question in refreshed)

    # --------------------------------------------------------------- internals
    async def _require_level(self, level_id: uuid.UUID) -> Level:
        level = await self._uow.editorial.get_level(ContentId(level_id))
        if level is None:
            raise NotFoundError("No level with that id.", code=ErrorCode.LEVEL_NOT_FOUND)
        return level

    async def _require_unit(self, unit_id: uuid.UUID) -> Unit:
        unit = await self._uow.editorial.get_unit(ContentId(unit_id))
        if unit is None:
            raise NotFoundError("No unit with that id.", code=ErrorCode.UNIT_NOT_FOUND)
        return unit

    async def _require_lesson(self, lesson_id: uuid.UUID) -> Lesson:
        lesson = await self._uow.editorial.get_lesson(ContentId(lesson_id))
        if lesson is None:
            raise NotFoundError("No lesson with that id.", code=ErrorCode.LESSON_NOT_FOUND)
        return lesson

    async def _require_question(self, question_id: uuid.UUID) -> Question:
        question = await self._uow.editorial.get_question(ContentId(question_id))
        if question is None:
            raise NotFoundError("No question with that id.", code=ErrorCode.QUESTION_NOT_FOUND)
        return question

    async def _level_position(self, requested: int | None) -> int:
        if requested is None:
            return await self._uow.editorial.next_level_position()
        _require_position(requested)
        await self._assert_level_position_free(requested, exclude_id=None)
        return requested

    async def _unit_position(self, level_id: ContentId, requested: int | None) -> int:
        if requested is None:
            return await self._uow.editorial.next_unit_position(level_id)
        _require_position(requested)
        await self._assert_unit_position_free(level_id, requested, exclude_id=None)
        return requested

    async def _lesson_position(self, unit_id: ContentId, requested: int | None) -> int:
        if requested is None:
            return await self._uow.editorial.next_lesson_position(unit_id)
        _require_position(requested)
        await self._assert_lesson_position_free(unit_id, requested, exclude_id=None)
        return requested

    async def _question_position(self, lesson_id: ContentId, requested: int | None) -> int:
        if requested is None:
            return await self._uow.editorial.next_question_position(lesson_id)
        _require_position(requested)
        await self._assert_question_position_free(lesson_id, requested, exclude_id=None)
        return requested

    async def _assert_level_position_free(
        self, position: int, *, exclude_id: uuid.UUID | None
    ) -> None:
        if await self._uow.editorial.level_position_exists(position, exclude_id=exclude_id):
            raise _position_taken("level", position)

    async def _assert_unit_position_free(
        self, level_id: ContentId, position: int, *, exclude_id: uuid.UUID | None
    ) -> None:
        if await self._uow.editorial.unit_position_exists(
            level_id, position, exclude_id=exclude_id
        ):
            raise _position_taken("unit", position)

    async def _assert_lesson_position_free(
        self, unit_id: ContentId, position: int, *, exclude_id: uuid.UUID | None
    ) -> None:
        if await self._uow.editorial.lesson_position_exists(
            unit_id, position, exclude_id=exclude_id
        ):
            raise _position_taken("lesson", position)

    async def _assert_question_position_free(
        self, lesson_id: ContentId, position: int, *, exclude_id: uuid.UUID | None
    ) -> None:
        if await self._uow.editorial.question_position_exists(
            lesson_id, position, exclude_id=exclude_id
        ):
            raise _position_taken("question", position)

    def _build_question(
        self,
        lesson_id: ContentId,
        position: int,
        command: QuestionCreateCommand,
        *,
        question_id: uuid.UUID | None = None,
    ) -> Question:
        problems = question_shape_problems(
            command.kind,
            choices=command.choices,
            accepted_answers=command.accepted_answers,
            correct_index=command.correct_index,
            explanation=command.explanation,
            image_url=command.image_url,
            audio_url=command.audio_url,
        )
        if problems:
            raise ValidationFailed(
                "The question is not answerable as written.",
                details={"problems": list(problems)},
            )
        _require_positive("points", command.points)
        try:
            return Question(
                id=ContentId(question_id or new_id()),
                lesson_id=lesson_id,
                position=position,
                kind=command.kind,
                skill=command.skill,
                prompt=command.prompt,
                choices=tuple(command.choices),
                accepted_answers=tuple(command.accepted_answers),
                correct_index=command.correct_index,
                explanation=command.explanation,
                image_url=command.image_url,
                audio_url=command.audio_url,
                points=command.points,
            )
        except ValueError as exc:  # pragma: no cover - shape is pre-checked above
            raise ValidationFailed(str(exc)) from exc

    async def _audit(
        self,
        actor_id: UserId,
        action: AuditAction,
        entity_type: str,
        entity_id: uuid.UUID | None,
        summary: str,
        changes: dict[str, Any] | None = None,
    ) -> None:
        await self._uow.audit_logs.add(
            AuditEntry(
                actor_id=actor_id,
                action=str(action),
                entity_type=entity_type,
                entity_id=entity_id,
                summary=summary,
                changes=changes or {},
            )
        )


# ------------------------------------------------------------------ module helpers
def _apply(move: Any) -> None:
    """Run an editorial transition, turning the domain refusal into a 409."""
    try:
        move()
    except InvalidContentState as exc:
        raise ConflictError(str(exc), code=ErrorCode.INVALID_STATE_TRANSITION) from exc


def _position_taken(entity: str, position: int) -> ConflictError:
    return ConflictError(
        f"Another {entity} already sits at position {position}.",
        code=ErrorCode.CONFLICT,
        details={"field": "position"},
    )


def _require_text(field: str, value: str) -> None:
    if not value or not value.strip():
        raise ValidationFailed(
            f"{field} must not be blank.", details={"field": field}
        )


def _require_positive(field: str, value: int) -> None:
    if value < 1:
        raise ValidationFailed(
            f"{field} must be one or greater.", details={"field": field}
        )


def _require_position(value: int) -> None:
    if value < 1:
        raise ValidationFailed(
            "position must be one or greater.", details={"field": "position"}
        )


def _level_view(level: Level) -> LevelAdminView:
    return LevelAdminView(
        id=level.id,
        code=level.code.value,
        title=level.title,
        description=level.description,
        position=level.position,
        state=level.state.value,
    )


def _unit_view(unit: Unit) -> UnitAdminView:
    return UnitAdminView(
        id=unit.id,
        level_id=unit.level_id,
        title=unit.title,
        description=unit.description,
        position=unit.position,
        state=unit.state.value,
    )


def _lesson_view(lesson: Lesson) -> LessonAdminView:
    return LessonAdminView(
        id=lesson.id,
        unit_id=lesson.unit_id,
        title=lesson.title,
        summary=lesson.summary,
        position=lesson.position,
        estimated_minutes=lesson.estimated_minutes,
        state=lesson.state.value,
    )


def _question_view(question: Question) -> QuestionAdminView:
    return QuestionAdminView(
        id=question.id,
        lesson_id=question.lesson_id,
        position=question.position,
        kind=question.kind.value,
        skill=question.skill.value,
        prompt=question.prompt,
        choices=tuple(question.choices),
        accepted_answers=tuple(question.accepted_answers),
        correct_index=question.correct_index,
        explanation=question.explanation,
        image_url=question.image_url,
        audio_url=question.audio_url,
        points=question.points,
    )


__all__ = ["AdminContentService"]
