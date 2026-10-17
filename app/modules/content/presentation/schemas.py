"""Wire schemas for the public catalogue.

The application layer already withholds the answer key from its read models; this file
withholds it a second time, from the JSON. Two independent places to get it right is the
point: a field added to the question aggregate today will not become part of tomorrow's
public response just because it exists upstream, because these schemas list their fields
by hand rather than exposing the ORM class.

``from_attributes`` is what lets the routes hand a read model straight to a response
model. It is worth being precise about what that does *not* do: it copies the named
attributes, it does not serialise whatever happens to be on the object. Adding
``correct_index`` to the aggregate therefore cannot leak it, because no schema here
declares a field for it.

``test_the_response_never_carries_the_answer_key`` pins the field list, so a future
"harmless" addition — a ``hint`` that turns out to spell out the answer — has to be a
deliberate edit to this file.
"""

from __future__ import annotations

import uuid

from pydantic import ConfigDict, Field

from app.core.responses import WireModel
from app.core.types import QuestionKind, SkillKind


class _CatalogueWire(WireModel):
    """A wire model that also reads attributes off a read model.

    A thin subclass rather than repeating ``model_config`` on each schema, because a schema
    that sets ``model_config`` to add only ``from_attributes`` would depend on Pydantic
    merging it with the inherited camelCase settings — a coupling worth being explicit about
    when getting it wrong silently reverts the naming convention.
    """

    model_config = ConfigDict(from_attributes=True)


class LevelResponse(_CatalogueWire):
    id: uuid.UUID
    code: str = Field(description="CEFR code, e.g. 'A1'. Stable public handle.")
    title: str
    description: str | None = None
    position: int
    unit_count: int = Field(ge=0, description="Published units in this level.")


class UnitResponse(_CatalogueWire):
    id: uuid.UUID
    level_code: str
    title: str
    description: str | None = None
    position: int
    lesson_count: int = Field(ge=0, description="Published lessons in this unit.")


class QuestionResponse(_CatalogueWire):
    """A practice item as it appears *before* an attempt.

    No ``correct_index``, no ``accepted_answers``, no ``explanation``. Post-attempt
    feedback belongs to the attempt engine, which returns it from the answer endpoint.

    ``image_url`` and ``audio_url`` *are* part of this pre-attempt shape, and that is a
    deliberate exception rather than an oversight: they are aids that illustrate or
    pronounce the taught word or sentence, so they teach the item rather than revealing
    the answer.

    ``prompts`` carries the items a matching question asks the learner to pair up. It is the
    same kind of exception: it says what is being asked and nothing about what the answer is.
    The bank of options they are paired against stays behind — this is a shared, cacheable
    read with no per-learner seed to shuffle that bank out of the order its pairs were
    authored in, so ``choices`` is empty for a matching question here.
    """

    id: uuid.UUID
    kind: QuestionKind
    skill: SkillKind
    prompt: str
    choices: list[str]
    points: int = Field(ge=1)
    image_url: str | None = None
    audio_url: str | None = None
    prompts: list[str] = Field(default_factory=list)


class LessonSummaryResponse(_CatalogueWire):
    id: uuid.UUID
    unit_id: uuid.UUID
    title: str
    summary: str
    position: int
    estimated_minutes: int = Field(ge=1)
    question_count: int = Field(ge=0)


class LessonDetailResponse(LessonSummaryResponse):
    questions: list[QuestionResponse] = Field(default_factory=list)


class UnitNodeResponse(UnitResponse):
    """A unit inside the catalogue tree, with its published lessons."""

    lessons: list[LessonSummaryResponse] = Field(default_factory=list)


class LevelNodeResponse(LevelResponse):
    """A level inside the catalogue tree, with its units."""

    units: list[UnitNodeResponse] = Field(default_factory=list)


class CatalogueTreeResponse(_CatalogueWire):
    """The whole published catalogue in one response.

    A wrapper object rather than a bare JSON array: a top-level array cannot grow a sibling
    (a ``meta`` or a schema version) without becoming a breaking change, and every other
    collection on this API is an object for the same reason.
    """

    items: list[LevelNodeResponse] = Field(default_factory=list)


__all__ = [
    "CatalogueTreeResponse",
    "LessonDetailResponse",
    "LessonSummaryResponse",
    "LevelNodeResponse",
    "LevelResponse",
    "QuestionResponse",
    "UnitNodeResponse",
    "UnitResponse",
]
