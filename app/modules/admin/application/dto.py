"""Read models and commands for the admin surface.

The admin views differ from the public catalogue's in exactly one way, and it is the whole
point of the surface: they carry the fields an *author* needs and a *learner* must not see.
:class:`QuestionAdminView` includes ``correct_index``, ``accepted_answers`` and
``explanation``; the public ``QuestionView`` deliberately omits all three. Keeping the two
projections in separate DTOs means the answer key can never leak by one view growing a field
the other shares.

Update commands carry a :data:`MISSING` sentinel rather than ``None`` for every optional
field. PATCH semantics are "leave what I did not mention alone", and ``None`` is a value on
some of these fields — clearing a ``description``, ``image_url`` or ``video_url`` is a
real edit. Without the sentinel, "clear this field" and "I did not mention this field" would
be the same input, and one of the two behaviours would be wrong.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TypeGuard

from app.core.types import CEFRLevel, ContentState, QuestionKind, SkillKind

__all__ = [
    "MISSING",
    "AdminUserListView",
    "AdminUserView",
    "LessonAdminDetailView",
    "LessonAdminView",
    "LessonCreateCommand",
    "LessonUpdateCommand",
    "LevelAdminView",
    "LevelCreateCommand",
    "LevelUpdateCommand",
    "QuestionAdminView",
    "QuestionBulkResult",
    "QuestionCreateCommand",
    "QuestionDuplicateCommand",
    "QuestionExportItemView",
    "QuestionUpdateCommand",
    "QuestionsExportView",
    "UnitAdminView",
    "UnitCreateCommand",
    "UnitUpdateCommand",
]


class _Missing:
    """The "field not provided" marker for PATCH commands.

    A private singleton rather than ``None`` or a sentinel string, so it can never collide
    with a legitimate value and so a stray ``if value:`` typo cannot mistake it for falsy
    data. Callers use :func:`is_given`.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"


#: Placeholder meaning "the client did not include this field".
MISSING = _Missing()


def is_given[T](value: T | _Missing) -> TypeGuard[T]:
    """True when a PATCH field was supplied, narrowing its type at the same time.

    A :class:`TypeGuard` rather than a plain ``bool`` so the services can write
    ``if is_given(command.title):`` and have the checked-and-``MISSING`` union collapse to
    the real type inside the block, instead of casting at every use.
    """
    return not isinstance(value, _Missing)


# ------------------------------------------------------------------- read models
@dataclass(frozen=True, slots=True)
class LevelAdminView:
    id: uuid.UUID
    code: str
    title: str
    description: str | None
    position: int
    state: str


@dataclass(frozen=True, slots=True)
class UnitAdminView:
    id: uuid.UUID
    level_id: uuid.UUID
    title: str
    description: str | None
    position: int
    state: str


@dataclass(frozen=True, slots=True)
class LessonAdminView:
    id: uuid.UUID
    unit_id: uuid.UUID
    title: str
    summary: str
    position: int
    estimated_minutes: int
    state: str


@dataclass(frozen=True, slots=True)
class QuestionAdminView:
    """An authored question, answer key included.

    This is an editor-facing projection, not a learner-facing one. ``correct_index``,
    ``accepted_answers`` and ``explanation`` are all here because the person fixing a
    mis-keyed question needs to see the key.
    """

    id: uuid.UUID
    lesson_id: uuid.UUID
    position: int
    kind: str
    skill: str
    prompt: str
    choices: tuple[str, ...]
    accepted_answers: tuple[str, ...]
    correct_index: int | None
    explanation: str
    image_url: str | None
    audio_url: str | None
    video_url: str | None
    points: int


@dataclass(frozen=True, slots=True)
class LessonAdminDetailView(LessonAdminView):
    questions: tuple[QuestionAdminView, ...] = ()


@dataclass(frozen=True, slots=True)
class QuestionExportItemView:
    """One question as a portable record.

    Deliberately carries no ``id``, no ``lesson_id`` and no ``position``: an export is a
    template for authoring, not a set of ids a second database would have no use for, and
    leaving the slot out is what lets an import append instead of colliding with whatever
    already sits at that position.
    """

    kind: str
    skill: str
    prompt: str
    choices: tuple[str, ...]
    accepted_answers: tuple[str, ...]
    correct_index: int | None
    explanation: str
    image_url: str | None
    audio_url: str | None
    video_url: str | None
    points: int


@dataclass(frozen=True, slots=True)
class QuestionsExportView:
    """A whole lesson's questions in the shape ``import`` accepts.

    ``version`` is the round-trip contract: an importer that meets a version it does not
    know must refuse rather than guess, because the failure mode of guessing is a silently
    mis-keyed question, not a loud error.
    """

    version: int
    lesson_id: uuid.UUID
    lesson_title: str
    exported_at: datetime
    questions: tuple[QuestionExportItemView, ...]


@dataclass(frozen=True, slots=True)
class QuestionBulkResult:
    """The outcome of a bulk write, or of the validation pass a dry run performs.

    ``accepted`` is what the caller needs from a dry run: the number of items that would
    have been written. ``items`` is empty there, because nothing was.
    """

    items: tuple[QuestionAdminView, ...]
    accepted: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class AdminUserView:
    id: uuid.UUID
    email: str
    status: str
    roles: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AdminUserListView:
    """A page of accounts, plus the count.

    Carries ``total`` where the content listings do not: an account list is a moderation
    queue, and "how many accounts are there" is a number an admin screen shows in its header
    and one the page cursor cannot answer.
    """

    items: tuple[AdminUserView, ...]
    total: int
    has_more: bool
    next_cursor: str | None


# ------------------------------------------------------------------------ commands
@dataclass(frozen=True, slots=True)
class LevelCreateCommand:
    code: CEFRLevel
    title: str
    description: str | None = None
    position: int | None = None


@dataclass(frozen=True, slots=True)
class LevelUpdateCommand:
    title: str | _Missing = MISSING
    description: str | _Missing | None = MISSING
    position: int | _Missing = MISSING
    state: ContentState | _Missing = MISSING


@dataclass(frozen=True, slots=True)
class UnitCreateCommand:
    title: str
    description: str | None = None
    position: int | None = None


@dataclass(frozen=True, slots=True)
class UnitUpdateCommand:
    title: str | _Missing = MISSING
    description: str | _Missing | None = MISSING
    position: int | _Missing = MISSING
    state: ContentState | _Missing = MISSING


@dataclass(frozen=True, slots=True)
class LessonCreateCommand:
    title: str
    summary: str
    estimated_minutes: int
    position: int | None = None


@dataclass(frozen=True, slots=True)
class LessonUpdateCommand:
    title: str | _Missing = MISSING
    summary: str | _Missing = MISSING
    estimated_minutes: int | _Missing = MISSING
    position: int | _Missing = MISSING
    state: ContentState | _Missing = MISSING


@dataclass(frozen=True, slots=True)
class QuestionCreateCommand:
    kind: QuestionKind
    skill: SkillKind
    prompt: str
    choices: tuple[str, ...] = ()
    accepted_answers: tuple[str, ...] = ()
    correct_index: int | None = None
    explanation: str = ""
    image_url: str | None = None
    audio_url: str | None = None
    video_url: str | None = None
    points: int = 1
    position: int | None = None


@dataclass(frozen=True, slots=True)
class QuestionDuplicateCommand:
    """Where a copy should land. Both fields default to "beside the original"."""

    lesson_id: uuid.UUID | None = None
    position: int | None = None


@dataclass(frozen=True, slots=True)
class QuestionUpdateCommand:
    kind: QuestionKind | _Missing = MISSING
    skill: SkillKind | _Missing = MISSING
    prompt: str | _Missing = MISSING
    choices: tuple[str, ...] | _Missing = MISSING
    accepted_answers: tuple[str, ...] | _Missing = MISSING
    correct_index: int | _Missing | None = MISSING
    explanation: str | _Missing = MISSING
    image_url: str | _Missing | None = MISSING
    audio_url: str | _Missing | None = MISSING
    video_url: str | _Missing | None = MISSING
    points: int | _Missing = MISSING
    position: int | _Missing = MISSING


@dataclass(frozen=True, slots=True)
class UserRolesCommand:
    roles: tuple[str, ...] = field(default_factory=tuple)
