"""Read models for the catalogue.

These are the objects the public API is built from, and :class:`QuestionView` is the
most important type in the file.

The catalogue endpoints are unauthenticated: a published curriculum is not account data,
and requiring a token to browse it would mean an app shell with no way to show anything
before sign-in. The consequence is that **anything omitted from a view is not leaked,
and anything included in one is public.** So the question view is assembled by omission
rather than by copying the aggregate: it carries the prompt, the options, the point value
and the optional ``image_url`` and ``audio_url`` aids, and deliberately not
``correct_index``, ``accepted_answers`` or ``explanation``. Those are consumed by the
attempt engine after a learner has answered, and serving them here would hand every
caller the answer key to a public practice test. The media aids are different: they
illustrate or pronounce the word or sentence being taught *before* the attempt, so they
teach rather than reveal and belong on the public view.

``test_the_detail_does_not_carry_the_answer_key`` guards this, and it deliberately
asserts on ``hasattr`` rather than on values: adding a field to the aggregate must never
be enough to publish it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.types import QuestionKind, SkillKind


@dataclass(frozen=True, slots=True)
class LevelView:
    id: uuid.UUID
    code: str
    title: str
    description: str | None
    position: int
    unit_count: int


@dataclass(frozen=True, slots=True)
class UnitView:
    id: uuid.UUID
    level_code: str
    title: str
    description: str | None
    position: int
    lesson_count: int


@dataclass(frozen=True, slots=True)
class LessonSummaryView:
    id: uuid.UUID
    unit_id: uuid.UUID
    title: str
    summary: str
    position: int
    estimated_minutes: int
    question_count: int


@dataclass(frozen=True, slots=True)
class QuestionView:
    """A practice item as a learner sees it *before* attempting it."""

    id: uuid.UUID
    kind: QuestionKind
    skill: SkillKind
    prompt: str
    choices: tuple[str, ...]
    points: int
    image_url: str | None
    audio_url: str | None


@dataclass(frozen=True, slots=True)
class LessonDetailView(LessonSummaryView):
    questions: tuple[QuestionView, ...] = ()


@dataclass(frozen=True, slots=True)
class UnitNodeView(UnitView):
    """A unit in the catalogue tree, carrying its lessons in curriculum order.

    A subclass rather than a parallel type so the unit's own fields stay defined in one
    place; ``lessons`` defaults to empty so the base ``UnitView`` construction still
    works and the field ordering rule (defaults after the inherited fields) is honoured.
    """

    lessons: tuple[LessonSummaryView, ...] = ()


@dataclass(frozen=True, slots=True)
class LevelNodeView(LevelView):
    """A level in the catalogue tree, carrying its units in curriculum order."""

    units: tuple[UnitNodeView, ...] = ()


__all__ = [
    "LessonDetailView",
    "LessonSummaryView",
    "LevelNodeView",
    "LevelView",
    "QuestionView",
    "UnitNodeView",
    "UnitView",
]
