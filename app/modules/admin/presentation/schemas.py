"""Wire schemas for the admin surface.

The request models here are the other half of the PATCH-sentinel decision documented in
``application/dto.py``: every field of an update request is optional and defaults to
``None``, and the *route* decides which fields the client actually sent by reading
``model_fields_set``. That is what lets ``null`` mean "clear this field" for a nullable one
(``description``, ``image_url``) while an absent key means "leave it alone" — without the
request model having to carry a sentinel of its own. Video aids are also nullable and
follow the same rule: clearing an ``image_url``, ``audio_url`` or ``video_url`` is a real
edit, so PATCH must distinguish absence from ``null``.

``QuestionAdminResponse`` is the one response on this API that *does* carry an answer key.
That is not a leak: this router is admin-gated, and the person fixing a mis-keyed question is
exactly who needs to see the key. It is kept separate from the public ``content`` schemas so
the two field lists can never be confused.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import ConfigDict, Field

from app.core.responses import WireModel
from app.core.types import CEFRLevel, ContentState, QuestionKind, SkillKind

__all__ = [
    "AdminStatsResponse",
    "AdminUserListResponse",
    "AdminUserResponse",
    "LessonCreateRequest",
    "LessonDetailAdminResponse",
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
    "SetAdminRequest",
    "UnitCreateRequest",
    "UnitResponse",
    "UnitUpdateRequest",
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
    video_url: str | None = None
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
    video_url: str | None = None
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
    video_url: str | None = None
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
    video_url: str | None = None
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
    video_url: str | None = None
    points: int = Field(default=1, ge=1, le=100)


class QuestionImportRequest(_AdminWire):
    """A package produced by the export endpoint, optionally as a validation-only dry run."""

    version: int = 1
    questions: list[QuestionImportItem] = Field(min_length=1, max_length=200)
    dry_run: bool = False


class QuestionReorderRequest(_AdminWire):
    question_ids: list[uuid.UUID] = Field(min_length=1)


class LessonDetailAdminResponse(LessonResponse):
    """The admin view of one lesson: every question, answer key included.

    Named rather than plain ``LessonDetailResponse`` because the learner-facing module
    declares that name too. Two same-named models make FastAPI prefix both with their
    module path, so neither appears under the name a client or a document means.
    """

    questions: list[QuestionAdminResponse] = Field(default_factory=list)


# ----------------------------------------------------------------------- users
class SetAdminRequest(_AdminWire):
    user_id: uuid.UUID
    is_admin: bool


class AdminUserResponse(_AdminWire):
    id: uuid.UUID
    email: str
    name: str
    status: str
    is_admin: bool
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
    two-question sitting must not outweigh a twenty-question one. ``sessionsActive`` is the
    one live figure in the payload — sittings in flight right now, not a windowed count.
    """

    lessons_completed: int = Field(ge=0)
    sessions_completed: int = Field(ge=0)
    sessions_active: int = Field(ge=0)
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


class StatsQuestionsResponse(_AdminWire):
    """The question stock and its two breakdowns.

    ``published`` counts questions under a published lesson — a question has no editorial
    state of its own. The maps are always complete across their enums (a kind nobody has
    authored reads ``0``, never absent) so a chart's categories cannot shift under it.
    """

    total: int = Field(ge=0)
    published: int = Field(ge=0)
    by_kind: dict[str, int]
    by_skill: dict[str, int]


class StatsPerformanceResponse(_AdminWire):
    """Learner quality: finished-lesson scores and spaced-repetition grades.

    ``retentionPercent`` is passed reviews over all reviews, weighted by review rather
    than by learner; ``averageQuality`` is the mean SM-2 grade (0..5) with the same zero
    state as the rest of this payload — no reviews yet means ``0``, not ``null``.
    """

    average_score: int = Field(ge=0, le=100)
    reviews_total: int = Field(ge=0)
    reviews_passed: int = Field(ge=0)
    retention_percent: int = Field(ge=0, le=100)
    average_quality: float = Field(ge=0, le=5)


class StatsGamificationResponse(_AdminWire):
    """Reward state across the learner base. ``activeStreaks`` is streaks alive today."""

    xp_total: int = Field(ge=0)
    active_streaks: int = Field(ge=0)
    longest_streak: int = Field(ge=0)
    achievements_unlocked: int = Field(ge=0)


class StatsAiResponse(_AdminWire):
    """Lifetime metering, so the home page can show AI spend without a second request."""

    requests: int = Field(ge=0)
    errors: int = Field(ge=0)
    tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    conversations: int = Field(ge=0)


class StatsSeriesPointResponse(_AdminWire):
    """One day of the chart, UTC. Days nobody used are present with zeroes."""

    day: date
    new_users: int = Field(ge=0)
    sessions_completed: int = Field(ge=0)
    xp_awarded: int = Field(ge=0)
    ai_requests: int = Field(ge=0)


class AdminStatsResponse(_AdminWire):
    """The overview screen's whole payload: aggregated, never a list of rows.

    ``series`` is the one array here, and it is bounded by ``windowDays + 1`` — one point
    per day the window touches, its endpoints partial because the window opens at this
    clock time and today is still running.
    """

    generated_at: datetime
    window_days: int = Field(ge=1)
    users: StatsUsersResponse
    learning: StatsLearningResponse
    content: StatsContentResponse
    questions: StatsQuestionsResponse
    performance: StatsPerformanceResponse
    gamification: StatsGamificationResponse
    ai: StatsAiResponse
    series: list[StatsSeriesPointResponse]
