"""SQLAlchemy adapters for the admin ports.

The editorial repository is the write half of the catalogue. It reuses the read-side
mappers from ``modules/content`` so a row read back here is the same aggregate the public
slice serves, and it writes columns explicitly — never ``session.add(domain_object)`` —
so a field added to an aggregate cannot silently fail to persist because a mapper forgot
it.

The two-phase :meth:`reorder_questions` exists because sibling positions are unique. Moving
row ``A`` onto ``B``'s slot would violate that constraint mid-reorder, so the first pass
parks every question above the current maximum (a range no occupied slot can be in) and the
second pass lays them down at ``1..n``. No intermediate flush is needed: the constraint is
checked per statement, and by the time the second pass runs, ``1..n`` is empty.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.core.pagination import PositionCursor
from app.core.types import CEFRLevel, ContentId
from app.modules.admin.domain.audit import AuditEntry
from app.modules.admin.infrastructure.models import AuditLogModel
from app.modules.content.domain.entities import Lesson, Level, Question, Unit
from app.modules.content.infrastructure.mappers import (
    to_lesson,
    to_level,
    to_question,
    to_unit,
)
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)


def _keyset(
    position_column: InstrumentedAttribute[int],
    id_column: InstrumentedAttribute[uuid.UUID],
    after: PositionCursor | None,
) -> ColumnElement[bool] | None:
    if after is None:
        return None
    return or_(
        position_column > after.position,
        and_(position_column == after.position, id_column > after.id),
    )


class SqlAlchemyAuditLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, entry: AuditEntry) -> None:
        self._session.add(
            AuditLogModel(
                actor_id=entry.actor_id,
                action=entry.action,
                entity_type=entry.entity_type,
                entity_id=entry.entity_id,
                summary=entry.summary,
                changes=dict(entry.changes),
            )
        )


class SqlAlchemyEditorialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ levels
    async def get_level(self, level_id: ContentId) -> Level | None:
        model = await self._session.get(LevelModel, level_id)
        return to_level(model) if model is not None else None

    async def get_level_by_code(self, code: CEFRLevel) -> Level | None:
        stmt = select(LevelModel).where(LevelModel.code == code.value)
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_level(model) if model is not None else None

    async def list_levels(self, *, limit: int, after: PositionCursor | None) -> list[Level]:
        stmt = select(LevelModel)
        predicate = _keyset(LevelModel.position, LevelModel.id, after)
        if predicate is not None:
            stmt = stmt.where(predicate)
        stmt = stmt.order_by(LevelModel.position, LevelModel.id).limit(limit)
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_level(model) for model in models]

    async def add_level(self, level: Level) -> None:
        self._session.add(
            LevelModel(
                id=level.id,
                code=level.code.value,
                title=level.title,
                description=level.description,
                position=level.position,
                state=level.state.value,
            )
        )

    async def save_level(self, level: Level) -> None:
        await self._session.execute(
            update(LevelModel)
            .where(LevelModel.id == level.id)
            .values(
                title=level.title,
                description=level.description,
                position=level.position,
                state=level.state.value,
            )
        )

    async def delete_level(self, level_id: ContentId) -> None:
        await self._session.execute(delete(LevelModel).where(LevelModel.id == level_id))

    async def next_level_position(self) -> int:
        highest = await self._session.scalar(select(func.max(LevelModel.position)))
        return int(highest or 0) + 1

    async def level_position_exists(self, position: int, *, exclude_id: uuid.UUID | None) -> bool:
        stmt = select(LevelModel.id).where(LevelModel.position == position)
        if exclude_id is not None:
            stmt = stmt.where(LevelModel.id != exclude_id)
        return (await self._session.execute(stmt.limit(1))).first() is not None

    # ------------------------------------------------------------------- units
    async def get_unit(self, unit_id: ContentId) -> Unit | None:
        model = await self._session.get(UnitModel, unit_id)
        return to_unit(model) if model is not None else None

    async def list_units_for_level(
        self, level_id: ContentId, *, limit: int, after: PositionCursor | None
    ) -> list[Unit]:
        stmt = select(UnitModel).where(UnitModel.level_id == level_id)
        predicate = _keyset(UnitModel.position, UnitModel.id, after)
        if predicate is not None:
            stmt = stmt.where(predicate)
        stmt = stmt.order_by(UnitModel.position, UnitModel.id).limit(limit)
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_unit(model) for model in models]

    async def add_unit(self, unit: Unit) -> None:
        self._session.add(
            UnitModel(
                id=unit.id,
                level_id=unit.level_id,
                title=unit.title,
                description=unit.description,
                position=unit.position,
                state=unit.state.value,
            )
        )

    async def save_unit(self, unit: Unit) -> None:
        await self._session.execute(
            update(UnitModel)
            .where(UnitModel.id == unit.id)
            .values(
                title=unit.title,
                description=unit.description,
                position=unit.position,
                state=unit.state.value,
            )
        )

    async def delete_unit(self, unit_id: ContentId) -> None:
        await self._session.execute(delete(UnitModel).where(UnitModel.id == unit_id))

    async def next_unit_position(self, level_id: ContentId) -> int:
        highest = await self._session.scalar(
            select(func.max(UnitModel.position)).where(UnitModel.level_id == level_id)
        )
        return int(highest or 0) + 1

    async def unit_position_exists(
        self, level_id: ContentId, position: int, *, exclude_id: uuid.UUID | None
    ) -> bool:
        stmt = select(UnitModel.id).where(
            UnitModel.level_id == level_id, UnitModel.position == position
        )
        if exclude_id is not None:
            stmt = stmt.where(UnitModel.id != exclude_id)
        return (await self._session.execute(stmt.limit(1))).first() is not None

    # ----------------------------------------------------------------- lessons
    async def get_lesson(self, lesson_id: ContentId) -> Lesson | None:
        model = await self._session.get(LessonModel, lesson_id)
        return to_lesson(model) if model is not None else None

    async def list_lessons_for_unit(
        self, unit_id: ContentId, *, limit: int, after: PositionCursor | None
    ) -> list[Lesson]:
        stmt = select(LessonModel).where(LessonModel.unit_id == unit_id)
        predicate = _keyset(LessonModel.position, LessonModel.id, after)
        if predicate is not None:
            stmt = stmt.where(predicate)
        stmt = stmt.order_by(LessonModel.position, LessonModel.id).limit(limit)
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_lesson(model) for model in models]

    async def add_lesson(self, lesson: Lesson) -> None:
        self._session.add(
            LessonModel(
                id=lesson.id,
                unit_id=lesson.unit_id,
                title=lesson.title,
                summary=lesson.summary,
                position=lesson.position,
                estimated_minutes=lesson.estimated_minutes,
                state=lesson.state.value,
            )
        )

    async def save_lesson(self, lesson: Lesson) -> None:
        await self._session.execute(
            update(LessonModel)
            .where(LessonModel.id == lesson.id)
            .values(
                title=lesson.title,
                summary=lesson.summary,
                position=lesson.position,
                estimated_minutes=lesson.estimated_minutes,
                state=lesson.state.value,
            )
        )

    async def delete_lesson(self, lesson_id: ContentId) -> None:
        await self._session.execute(delete(LessonModel).where(LessonModel.id == lesson_id))

    async def next_lesson_position(self, unit_id: ContentId) -> int:
        highest = await self._session.scalar(
            select(func.max(LessonModel.position)).where(LessonModel.unit_id == unit_id)
        )
        return int(highest or 0) + 1

    async def lesson_position_exists(
        self, unit_id: ContentId, position: int, *, exclude_id: uuid.UUID | None
    ) -> bool:
        stmt = select(LessonModel.id).where(
            LessonModel.unit_id == unit_id, LessonModel.position == position
        )
        if exclude_id is not None:
            stmt = stmt.where(LessonModel.id != exclude_id)
        return (await self._session.execute(stmt.limit(1))).first() is not None

    # --------------------------------------------------------------- questions
    async def get_question(self, question_id: ContentId) -> Question | None:
        model = await self._session.get(QuestionModel, question_id)
        return to_question(model) if model is not None else None

    async def list_questions_for_lesson(self, lesson_id: ContentId) -> list[Question]:
        stmt = (
            select(QuestionModel)
            .where(QuestionModel.lesson_id == lesson_id)
            .order_by(QuestionModel.position, QuestionModel.id)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_question(model) for model in models]

    async def add_question(self, question: Question) -> None:
        self._session.add(
            QuestionModel(
                id=question.id,
                lesson_id=question.lesson_id,
                position=question.position,
                kind=question.kind.value,
                skill=question.skill.value,
                prompt=question.prompt,
                choices=list(question.choices),
                accepted_answers=list(question.accepted_answers),
                correct_index=question.correct_index,
                explanation=question.explanation,
                image_url=question.image_url,
                audio_url=question.audio_url,
                points=question.points,
            )
        )

    async def save_question(self, question: Question) -> None:
        await self._session.execute(
            update(QuestionModel)
            .where(QuestionModel.id == question.id)
            .values(
                position=question.position,
                kind=question.kind.value,
                skill=question.skill.value,
                prompt=question.prompt,
                choices=list(question.choices),
                accepted_answers=list(question.accepted_answers),
                correct_index=question.correct_index,
                explanation=question.explanation,
                image_url=question.image_url,
                audio_url=question.audio_url,
                points=question.points,
            )
        )

    async def delete_question(self, question_id: ContentId) -> None:
        await self._session.execute(delete(QuestionModel).where(QuestionModel.id == question_id))

    async def next_question_position(self, lesson_id: ContentId) -> int:
        highest = await self._session.scalar(
            select(func.max(QuestionModel.position)).where(QuestionModel.lesson_id == lesson_id)
        )
        return int(highest or 0) + 1

    async def question_position_exists(
        self, lesson_id: ContentId, position: int, *, exclude_id: uuid.UUID | None
    ) -> bool:
        stmt = select(QuestionModel.id).where(
            QuestionModel.lesson_id == lesson_id, QuestionModel.position == position
        )
        if exclude_id is not None:
            stmt = stmt.where(QuestionModel.id != exclude_id)
        return (await self._session.execute(stmt.limit(1))).first() is not None

    async def reorder_questions(self, lesson_id: ContentId, ordered_ids: list[ContentId]) -> None:
        base = len(ordered_ids) + 1
        for offset, question_id in enumerate(ordered_ids):
            await self._session.execute(
                update(QuestionModel)
                .where(QuestionModel.id == question_id, QuestionModel.lesson_id == lesson_id)
                .values(position=base + offset)
            )
        for offset, question_id in enumerate(ordered_ids):
            await self._session.execute(
                update(QuestionModel)
                .where(QuestionModel.id == question_id, QuestionModel.lesson_id == lesson_id)
                .values(position=offset + 1)
            )


__all__ = [
    "SqlAlchemyAuditLogRepository",
    "SqlAlchemyEditorialRepository",
]
