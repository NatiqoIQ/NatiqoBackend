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
from app.modules.content.domain.entities import Lesson, LessonPlacement, Level, Question, Unit


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

    async def list_published_ordered(self) -> list[LessonPlacement]:
        """Every published lesson in curriculum order, with its placement.

        Ordered by ``(level position, unit position, lesson position, id)``. A lesson
        counts as published only when its lesson, unit *and* level are all published —
        visibility is inherited down the tree, so a lesson under a draft unit is not
        reachable and must not appear in a learner's map.

        This is deliberately unpaginated: the caller is a progress screen that draws the
        whole course at once, and the published catalogue is the bound. If it ever grows
        past a page, this is the method to revisit.
        """
        ...

    async def get_published_placement(self, lesson_id: ContentId) -> LessonPlacement | None:
        """One published lesson with its placement, or ``None``.

        Used where a caller needs a lesson's level code and curriculum position alongside
        the lesson itself, and where the full chain must be published.
        """
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

    async def get_published(self, question_id: ContentId) -> Question | None:
        """One question, only when its lesson is published.

        A question inherits its lesson's visibility, so this method carries the state
        predicate itself — unlike :meth:`list_for_lessons` — because there is no
        caller-side "I already checked the lesson" step on this code path.
        """
        ...

    async def list_published_by_ids(self, question_ids: Sequence[ContentId]) -> list[Question]:
        """Several questions by id, keeping only those under a published lesson.

        Unlike :meth:`list_for_lessons` this carries the state predicate itself: the
        caller (the review queue, which holds raw question ids from a learner's cards)
        has not necessarily checked that each lesson is still published. Ids that are
        missing, or whose lesson has gone back to draft, are silently dropped, so the
        returned list may be shorter than the input.
        """
        ...
