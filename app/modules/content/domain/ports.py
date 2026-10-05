"""Ports the application layer uses to reach catalogue data.

Each port is owned by the domain and stated in domain terms, so the application layer
never learns that the catalogue happens to live in SQL. The read methods are named for
the promise they make (``get_published_by_code``) rather than for the query behind them,
because "published only" is a rule of the catalogue, not a filter a repository may or
may not apply.

All list operations return ``limit + 1`` rows at most. The service trims the extra row
and turns it into ``has_more``, which keeps the "is there another page" question in one
place instead of in every query.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from app.core.pagination import PositionCursor
from app.core.types import CEFRLevel, ContentId
from app.modules.content.domain.entities import Lesson, Level, Question, Unit


@runtime_checkable
class LevelRepositoryPort(Protocol):
    async def get_published_by_code(self, code: CEFRLevel) -> Level | None:
        """The published level for a CEFR code, or ``None``.

        ``None`` covers both "no such level" and "not published yet". The two are not
        distinguished on purpose: telling a caller that A2 exists but is still in draft
        leaks the editorial pipeline.
        """
        ...

    async def list_published(self, *, limit: int, after: PositionCursor | None) -> list[Level]:
        """Published levels ordered by ``(position, id)``, starting after ``after``."""
        ...


@runtime_checkable
class UnitRepositoryPort(Protocol):
    async def get_published(self, unit_id: ContentId) -> Unit | None:
        """The published unit, or ``None`` if it is missing or not published."""
        ...

    async def list_published_for_level(
        self, level_id: ContentId, *, limit: int, after: PositionCursor | None
    ) -> list[Unit]:
        """Published units of one level ordered by ``(position, id)``."""
        ...

    async def count_published_by_level(self) -> Mapping[ContentId, int]:
        """Published unit count per level id, in a single grouped query.

        Counting this way keeps a page of levels at one query regardless of page size;
        a per-row count would make the levels screen scale with the page.
        """
        ...


@runtime_checkable
class LessonRepositoryPort(Protocol):
    async def get_published(self, lesson_id: ContentId) -> Lesson | None:
        """The published lesson, or ``None`` if it is missing or not published."""
        ...

    async def list_published_for_unit(
        self, unit_id: ContentId, *, limit: int, after: PositionCursor | None
    ) -> list[Lesson]:
        """Published lessons of one unit ordered by ``(position, id)``."""
        ...

    async def count_published_by_unit(self) -> Mapping[ContentId, int]:
        """Published lesson count per unit id, in a single grouped query."""
        ...


@runtime_checkable
class QuestionRepositoryPort(Protocol):
    async def list_for_lessons(self, lesson_ids: Sequence[ContentId]) -> list[Question]:
        """Questions for the given lessons, grouped by lesson and ordered by position.

        There is no state filter here, and that is intentional rather than an omission:
        a question has no lifecycle of its own, so it is visible exactly when its
        lesson is. Callers must therefore only pass lessons they have already checked
        are published.
        """
        ...
