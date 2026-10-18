"""Wire schemas for the admin surface.

The request models here are the other half of the PATCH-sentinel decision documented in
``application/dto.py``: every field of an update request is optional and defaults to
``None``, and the *route* decides which fields the client actually sent by reading
``model_fields_set``. That is what lets ``null`` mean "clear this field" for a nullable one
(``description``, ``image_url``) while an absent key means "leave it alone" — without the
request model having to carry a sentinel of its own.

``QuestionAdminResponse`` is the one response on this API that *does* carry an answer key.
That is not a leak: this router is role-gated at ``content_editor`` or above, and the person
fixing a mis-keyed question is exactly who needs to see the key. It is kept separate from the
public ``content`` schemas so the two field lists can never be confused.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import ConfigDict, Field

from app.core.responses import WireModel
from app.core.types import CEFRLevel, ContentState, QuestionKind, SkillKind

__all__ = [
    "AdminStatsResponse",
    "AdminUserListResponse",
    "AdminUserResponse",
    "LessonCreateRequest",
    "LessonDetailResponse",
    "LessonResponse",
    "LessonUpdateRequest",
    "LevelCreateRequest",
    "LevelResponse",
    "LevelUpdateRequest",
    "QuestionAdminResponse",
    "QuestionBulkRequest",
    "QuestionBulkResponse",
    "QuestionCreateRequest",
    "QuestionDuplicateRequest",
    "QuestionExportItem",
    "QuestionExportResponse",
    "QuestionImportItem",
    "QuestionImportRequest",
    "QuestionListResponse",
    "QuestionReorderRequest",
    "QuestionUpdateRequest",
    "UnitCreateRequest",
    "UnitResponse",
    "UnitUpdateRequest",
    "UserRolesRequest",
]


class _AdminWire(WireModel):
    """A wire model that also reads attributes off an application read model."""

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------- levels
class LevelCreateRequest(_AdminWire):
    code: CEFRLevel
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    position: int | None = Field(default=None, ge=1)


class LevelUpdateRequest(_AdminWire):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    position: int | None = Field(default=None, ge=1)
    state: ContentState | None = None


class LevelResponse(_AdminWire):
    id: uuid.UUID
    code: str
    title: str
    description: str | None = None
    position: int
    state: str


# ----------------------------------------------------------------------- units
class UnitCreateRequest(_AdminWire):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    position: int | None = Field(default=None, ge=1)


class UnitUpdateRequest(_AdminWire):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    position: int | None = Field(default=None, ge=1)
    state: ContentState | None = None


class UnitResponse(_AdminWire):
    id: uuid.UUID
    level_id: uuid.UUID
    title: str
    description: str | None = None
    position: int
    state: str


# --------------------------------------------------------------------- lessons
class LessonCreateRequest(_AdminWire):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    estimated_minutes: int = Field(ge=1, le=240)
    position: int | None = Field(default=None, ge=1)


class LessonUpdateRequest(_AdminWire):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = Field(default=None, min_length=1, max_length=1000)
    estimated_minutes: int | None = Field(default=None, ge=1, le=240)
    position: int | None = Field(default=None, ge=1)
    state: ContentState | None = None


class LessonResponse(_AdminWire):
    id: uuid.UUID
    unit_id: uuid.UUID
    title: str
    summary: str
    position: int
    estimated_minutes: int
    state: str


# ------------------------------------------------------------------- questions
class QuestionCreateRequest(_AdminWire):
    kind: QuestionKind
    skill: SkillKind
    prompt: str = Field(min_length=1, max_length=2000)
    choices: list[str] = Field(default_factory=list)
    accepted_answers: list[str] = Field(default_factory=list)
    correct_index: int | None = None
    explanation: str = Field(default="", max_length=2000)
    image_url: str | None = None
    audio_url: str | None = None
    points: int = Field(default=1, ge=1, le=100)
    position: int | None = Field(default=None, ge=1)


class QuestionUpdateRequest(_AdminWire):
    kind: QuestionKind | None = None
    skill: SkillKind | None = None
    prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    choices: list[str] | None = None
    accepted_answers: list[str] | None = None
    correct_index: int | None = None
    explanation: str | None = Field(default=None, max_length=2000)
    image_url: str | None = None
    audio_url: str | None = None
    points: int | None = Field(default=None, ge=1, le=100)
    position: int | None = Field(default=None, ge=1)


class QuestionAdminResponse(_AdminWire):
    id: uuid.UUID
    lesson_id: uuid.UUID
    position: int
    kind: str
    skill: str
    prompt: str
    choices: list[str]
    accepted_answers: list[str]
    correct_index: int | None = None
    explanation: str
    image_url: str | None = None
    audio_url: str | None = None
    points: int


class QuestionListResponse(_AdminWire):
    items: list[QuestionAdminResponse] = Field(default_factory=list)
    total: int


class QuestionDuplicateRequest(_AdminWire):
    """Where the copy should land. Omitting both fields copies beside the original."""

    lesson_id: uuid.UUID | None = None
    position: int | None = Field(default=None, ge=1)


class QuestionBulkRequest(_AdminWire):
    """Several questions in one call.

    ``dryRun`` validates the whole batch and writes nothing, which is what an import preview
    uses: it answers "would these be accepted" without leaving a half-imported lesson behind.
    """

    questions: list[QuestionCreateRequest] = Field(min_length=1, max_length=200)
    dry_run: bool = False


class QuestionBulkResponse(_AdminWire):
    """What a bulk write changed, or — for a dry run — what it would have.

    ``items`` is empty on a dry run and ``accepted`` is the count that passed validation, so a
    caller never has to infer success from an empty list.
    """

    items: list[QuestionAdminResponse] = Field(default_factory=list)
    total: int
    accepted: int
    dry_run: bool = False


class QuestionExportItem(_AdminWire):
    """One portable question: content and key, but no id, lesson or position.

    Leaving out the identity fields is the contract that makes import an append rather than a
    collision, and it means the same package can be imported into any lesson.
    """

    kind: str
    skill: str
    prompt: str
    choices: list[str] = Field(default_factory=list)
    accepted_answers: list[str] = Field(default_factory=list)
    correct_index: int | None = None
    explanation: str
    image_url: str | None = None
    audio_url: str | None = None
    points: int


class QuestionExportResponse(_AdminWire):
    """A lesson's questions as a package whose shape ``QuestionImportRequest`` accepts.

    ``version`` is the round-trip contract; an importer that does not recognise it must
    refuse the whole package rather than half-read it.
    """

    version: int
    lesson_id: uuid.UUID
    lesson_title: str
    exported_at: datetime
    questions: list[QuestionExportItem] = Field(default_factory=list)


class QuestionImportItem(_AdminWire):
    """A question read from an export.

    No ``position``: an import appends in the order the package lists, which is the export's
    own order. Unknown keys are ignored on purpose, so a package written by a slightly newer
    build still imports its known fields rather than being refused outright.
    """

    kind: QuestionKind
    skill: SkillKind
    prompt: str = Field(min_length=1, max_length=2000)
    choices: list[str] = Field(default_factory=list)
    accepted_answers: list[str] = Field(default_factory=list)
    correct_index: int | None = None
    explanation: str = Field(default="", max_length=2000)
    image_url: str | None = None
    audio_url: str | None = None
    points: int = Field(default=1, ge=1, le=100)


class QuestionImportRequest(_AdminWire):
    """A package produced by the export endpoint, optionally as a validation-only dry run."""

    version: int = 1
    questions: list[QuestionImportItem] = Field(min_length=1, max_length=200)
    dry_run: bool = False


class QuestionReorderRequest(_AdminWire):
    question_ids: list[uuid.UUID] = Field(min_length=1)


class LessonDetailResponse(LessonResponse):
    questions: list[QuestionAdminResponse] = Field(default_factory=list)


# ----------------------------------------------------------------------- users
class UserRolesRequest(_AdminWire):
    user_id: uuid.UUID
    roles: list[str] = Field(min_length=1)


class AdminUserResponse(_AdminWire):
    id: uuid.UUID
    email: str
    status: str
    roles: list[str]
    created_at: datetime


class AdminUserListResponse(_AdminWire):
    items: list[AdminUserResponse] = Field(default_factory=list)
    total: int
    has_more: bool
    next_cursor: str | None = None


# ----------------------------------------------------------------------- stats
class StatsUsersResponse(_AdminWire):
    """Account totals. ``active`` is scoped to the request's window; the rest are lifetime."""

    total: int = Field(ge=0)
    new: int = Field(ge=0)
    active: int = Field(ge=0)
    deleted: int = Field(ge=0)


class StatsLearningResponse(_AdminWire):
    """What learners have actually done, as raw counts plus the one derived ratio.

    ``accuracyPercent`` is weighted by answers, not an average of per-session percentages: a
    two-question sitting must not outweigh a twenty-question one.
    """

    lessons_completed: int = Field(ge=0)
    sessions_completed: int = Field(ge=0)
    answers_graded: int = Field(ge=0)
    correct_answers: int = Field(ge=0)
    accuracy_percent: int = Field(ge=0, le=100)
    xp_awarded: int = Field(ge=0)


class StatsContentResponse(_AdminWire):
    """The catalogue's size, and how much of it is still in the editorial pipeline.

    ``drafts`` and ``inReview`` sum levels, units and lessons into the two queues the screen
    shows, so a caller does not have to add three numbers itself and get the definition wrong.
    """

    levels: int = Field(ge=0)
    units: int = Field(ge=0)
    lessons: int = Field(ge=0)
    published_lessons: int = Field(ge=0)
    drafts: int = Field(ge=0)
    in_review: int = Field(ge=0)


class AdminStatsResponse(_AdminWire):
    """The overview screen's whole payload: aggregated, never a list of rows."""

    generated_at: datetime
    window_days: int = Field(ge=1)
    users: StatsUsersResponse
    learning: StatsLearningResponse
    content: StatsContentResponse
