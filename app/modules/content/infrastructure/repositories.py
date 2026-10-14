"""SQLAlchemy implementations of the catalogue ports.

Two things every method here does:

**Filters to published rows, unconditionally.** A repository method named
``get_published_*`` that can also return a draft would be a trap for the next caller, so
the state predicate is part of the query rather than something the caller remembers.
"Not published" and "does not exist" both come back as ``None`` and are answered with
the same 404, so the editorial pipeline is not observable from outside.

**Pages with keyset predicates, not offsets.** ``after`` is a ``(position, id)`` sort
key, and the SQL compares the tuple lexicographically:

    WHERE position > :position OR (position = :position AND id > :id)

That is the whole point of carrying the id alongside the position — see
``app/core/pagination.py``. Listings fetch ``limit + 1`` rows and let the service decide
whether a further page exists.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.core.pagination import PositionCursor
from app.core.types import CEFRLevel, ContentId, ContentState
from app.modules.content.domain.entities import Lesson, LessonPlacement, Level, Question, Unit
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

_PUBLISHED = ContentState.PUBLISHED.value


def _keyset_predicate(
    position_column: InstrumentedAttribute[int],
    id_column: InstrumentedAttribute[uuid.UUID],
    after: PositionCursor,
) -> ColumnElement[bool]:
    """``(position, id) > (after.position, after.id)`` for a two-column sort key.

    Written as the disjunction rather than a row-value comparison
    (``(position, id) > (:p, :i)``) because SQLite, which the test suite runs on, only
    learned row values in 3.15 — and a migration that works on the test database but
    not on the production one is worse than a slightly longer query.
    """
    return or_(
        position_column > after.position,
        and_(position_column == after.position, id_column > after.id),
    )


def _placement_select() -> Select[LessonModel, str, int, int]:
    """Lessons joined to their unit and level, restricted to published rows throughout.

    Visibility is inherited, so a lesson is only listed when all three of lesson, unit
    and level are published. Selects the level code and both ancestor positions so the
    caller can order the curriculum without a second query per lesson.
    """
    return (
        select(LessonModel, LevelModel.code, LevelModel.position, UnitModel.position)
        .join(UnitModel, UnitModel.id == LessonModel.unit_id)
        .join(LevelModel, LevelModel.id == UnitModel.level_id)
        .where(
            LessonModel.state == _PUBLISHED,
            UnitModel.state == _PUBLISHED,
            LevelModel.state == _PUBLISHED,
        )
    )


def _to_placement(row: Row[LessonModel, str, int, int]) -> LessonPlacement:
    model, level_code, level_position, unit_position = row
    return LessonPlacement(
        lesson=to_lesson(model),
        level_code=CEFRLevel(level_code),
        level_position=level_position,
        unit_position=unit_position,
    )


class SqlAlchemyLevelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_published_by_code(self, code: CEFRLevel) -> Level | None:
        stmt = select(LevelModel).where(
            LevelModel.code == code.value,
            LevelModel.state == _PUBLISHED,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_level(model) if model else None

    async def list_published(self, *, limit: int, after: PositionCursor | None) -> list[Level]:
        stmt = select(LevelModel).where(LevelModel.state == _PUBLISHED)
        if after is not None:
            stmt = stmt.where(_keyset_predicate(LevelModel.position, LevelModel.id, after))
        stmt = stmt.order_by(LevelModel.position, LevelModel.id).limit(limit)
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_level(model) for model in models]


class SqlAlchemyUnitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_published(self, unit_id: ContentId) -> Unit | None:
        stmt = select(UnitModel).where(UnitModel.id == unit_id, UnitModel.state == _PUBLISHED)
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_unit(model) if model else None

    async def list_published_for_level(
        self, level_id: ContentId, *, limit: int, after: PositionCursor | None
    ) -> list[Unit]:
        stmt = select(UnitModel).where(
            UnitModel.level_id == level_id,
            UnitModel.state == _PUBLISHED,
        )
        if after is not None:
            stmt = stmt.where(_keyset_predicate(UnitModel.position, UnitModel.id, after))
        stmt = stmt.order_by(UnitModel.position, UnitModel.id).limit(limit)
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_unit(model) for model in models]

    async def count_published_by_level(self) -> Mapping[ContentId, int]:
        stmt = (
            select(UnitModel.level_id, func.count())
            .where(UnitModel.state == _PUBLISHED)
            .group_by(UnitModel.level_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {ContentId(level_id): int(count) for level_id, count in rows}


class SqlAlchemyLessonRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_published(self, lesson_id: ContentId) -> Lesson | None:
        stmt = select(LessonModel).where(
            LessonModel.id == lesson_id,
            LessonModel.state == _PUBLISHED,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_lesson(model) if model else None

    async def list_published_for_unit(
        self, unit_id: ContentId, *, limit: int, after: PositionCursor | None
    ) -> list[Lesson]:
        stmt = select(LessonModel).where(
            LessonModel.unit_id == unit_id,
            LessonModel.state == _PUBLISHED,
        )
        if after is not None:
            stmt = stmt.where(_keyset_predicate(LessonModel.position, LessonModel.id, after))
        stmt = stmt.order_by(LessonModel.position, LessonModel.id).limit(limit)
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_lesson(model) for model in models]

    async def count_published_by_unit(self) -> Mapping[ContentId, int]:
        stmt = (
            select(LessonModel.unit_id, func.count())
            .where(LessonModel.state == _PUBLISHED)
            .group_by(LessonModel.unit_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {ContentId(unit_id): int(count) for unit_id, count in rows}

    async def list_published_ordered(self) -> list[LessonPlacement]:
        """See :meth:`LessonRepositoryPort.list_published_ordered`.

        The whole chain is filtered in one query: a lesson under a draft unit or level is
        not published, because visibility is inherited and not stored redundantly.
        """
        stmt = (
            _placement_select()
            .order_by(
                LevelModel.position,
                UnitModel.position,
                LessonModel.position,
                LessonModel.id,
            )
        )
        rows = (await self._session.execute(stmt)).all()
        return [_to_placement(row) for row in rows]

    async def get_published_placement(self, lesson_id: ContentId) -> LessonPlacement | None:
        """See :meth:`LessonRepositoryPort.get_published_placement`."""
        stmt = _placement_select().where(LessonModel.id == lesson_id)
        row = (await self._session.execute(stmt)).first()
        return _to_placement(row) if row is not None else None


class SqlAlchemyQuestionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_lessons(self, lesson_ids: Sequence[ContentId]) -> list[Question]:
        """Questions for several lessons at once, ordered by lesson then position.

        No state predicate, by design: a question inherits its lesson's visibility, and
        the caller has already established that every id passed in is published.
        """
        if not lesson_ids:
            return []
        stmt = (
            select(QuestionModel)
            .where(QuestionModel.lesson_id.in_(list(lesson_ids)))
            .order_by(QuestionModel.lesson_id, QuestionModel.position, QuestionModel.id)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_question(model) for model in models]

    async def get_published(self, question_id: ContentId) -> Question | None:
        """One question, and only when its lesson is published.

        The join carries the state predicate, so a question under a draft lesson is as
        invisible as one that was never created — matching the "not published" and
        "does not exist" collapse every other repository method already guarantees.
        """
        stmt = (
            select(QuestionModel)
            .join(LessonModel, LessonModel.id == QuestionModel.lesson_id)
            .where(
                QuestionModel.id == question_id,
                LessonModel.state == ContentState.PUBLISHED.value,
            )
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_question(model) if model is not None else None
