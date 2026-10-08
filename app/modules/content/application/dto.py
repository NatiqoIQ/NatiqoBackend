"""Read models for the catalogue.

These are the objects the public API is built from, and :class:`QuestionView` is the
most important type in the file.

The catalogue endpoints are unauthenticated: a published curriculum is not account data,
and requiring a token to browse it would mean an app shell with no way to show anything
before sign-in. The consequence is that **anything omitted from a view is not leaked,
and anything included in one is public.** So the question view is assembled by omission
rather than by copying the aggregate: it carries the prompt, the options and the point
value, and deliberately not ``correct_index``, ``accepted_answers`` or ``explanation``.
Those are consumed by the attempt engine after a learner has answered, and serving them
here would hand every caller the answer key to a public practice test.

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


@dataclass(frozen=True, slots=True)
class LessonDetailView(LessonSummaryView):
    questions: tuple[QuestionView, ...] = ()


@dataclass(frozen=True, slots=True)
class QuestionExplanationView:
    """The rationale behind a question's answer.

    Served only by the dedicated feedback endpoint, never inside a lesson payload:
    this is the answer key made human-readable, so it must not travel with the public
    catalogue. There is no attempt engine yet, so the *client* is the one that asks for
    it after the learner has answered.
    """

    id: uuid.UUID
    explanation: str


__all__ = [
    "LessonDetailView",
    "LessonSummaryView",
    "LevelView",
    "QuestionExplanationView",
    "QuestionView",
    "UnitView",
]
