"""Wire schemas for the progress surface.

These list their fields by hand, the same way the catalogue does: a progress view is built
up from durable rows, and declaring the fields explicitly means adding something to the
underlying entity cannot silently start leaking. Nothing here carries an answer key —
progress only restates scores the learner has already been given.

``from_attributes`` is what lets a route hand a read model straight to a response model. It
copies the *named* attributes only, so a field that is not declared below never appears in
the JSON regardless of what sits upstream.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import ConfigDict, Field

from app.core.responses import Page, WireModel

__all__ = [
    "ProgressOverviewResponse",
    "ProgressSummaryResponse",
    "UnitProgressItemResponse",
    "UnitsResponse",
    "UnlockMapItemResponse",
    "UnlockMapResponse",
    "XpEventResponse",
    "XpHistoryPage",
]


class _ProgressWire(WireModel):
    """A wire model that reads attributes off a read model.

    Subclass rather than repeating ``model_config`` on each schema, so the camelCase
    aliasing and ``from_attributes`` can never drift apart by one of them being edited.
    """

    model_config = ConfigDict(from_attributes=True)


class ProgressSummaryResponse(_ProgressWire):
    """The headline figures, and only the ones the server can actually derive.

    Hearts and ``reviewsDue`` are deliberately absent: those belong to phases with no
    backing store yet, so they are omitted here rather than returned as zeros that a
    client would render as real data.
    """

    total_xp: int = Field(ge=0)
    lessons_completed: int = Field(ge=0)
    lessons_in_progress: int = Field(ge=0)
    lessons_started: int = Field(ge=0)
    correct_count: int = Field(ge=0)
    answered_count: int = Field(ge=0)
    accuracy_percent: int = Field(ge=0, le=100)
    current_streak_days: int = Field(ge=0)
    longest_streak_days: int = Field(ge=0)
    last_activity_at: datetime | None = None


class UnlockMapItemResponse(_ProgressWire):
    """One node of the curriculum-ordered progress map, with the learner's record.

    ``attempts`` and ``xpEarned`` are here because the map is the only place a single
    lesson's record is read now: a client that wants to show "3 attempts, 40 XP" on a node
    reads it off the map rather than making a second request.
    """

    lesson_id: uuid.UUID
    unit_id: uuid.UUID
    level_code: str
    title: str
    position: int
    state: str
    locked: bool
    is_completed: bool
    is_unlocked: bool
    best_score: int = Field(ge=0, le=100)
    attempts: int = Field(ge=0)
    xp_earned: int = Field(ge=0)
    completed_at: datetime | None = None


class UnlockMapResponse(_ProgressWire):
    """The whole published curriculum in order, each node coloured by the learner.

    An envelope rather than a bare list, so the shape stays extensible — a ``total`` here
    is free today and a breaking list-to-object change is not.
    """

    items: list[UnlockMapItemResponse] = Field(default_factory=list)
    total: int = Field(ge=0, description="Published lessons in the curriculum.")


class UnitProgressItemResponse(_ProgressWire):
    """One unit's placement plus its checkpoint standing.

    ``locked`` and ``checkpointReady`` answer different questions — may I start this
    unit at all, and have I finished its lessons — and both are sent so the client picks
    the next action without a third read.
    """

    unit_id: uuid.UUID
    level_code: str
    title: str
    position: int
    locked: bool
    passed: bool
    best_score: int = Field(ge=0, le=100)
    attempts: int = Field(ge=0)
    passed_at: datetime | None = None
    lessons_total: int = Field(ge=0)
    lessons_completed: int = Field(ge=0)
    checkpoint_ready: bool


class UnitsResponse(_ProgressWire):
    """Every published unit in curriculum order. Enveloped for the same reason as the map."""

    items: list[UnitProgressItemResponse] = Field(default_factory=list)
    total: int = Field(ge=0, description="Published units in the curriculum.")


class XpEventResponse(_ProgressWire):
    """One completed session as a row of the XP history feed.

    ``lessonId`` and ``unitId`` are the two scopes a session can have, so a checkpoint
    row is labelable without a second lookup.
    """

    session_id: uuid.UUID
    lesson_id: uuid.UUID | None
    xp_awarded: int = Field(ge=0)
    correct_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    score_percent: int = Field(ge=0, le=100)
    completed_at: datetime | None = None
    unit_id: uuid.UUID | None = None


XpHistoryPage = Page[XpEventResponse]


class ProgressOverviewResponse(ProgressSummaryResponse):
    """The summary and the first page of the XP feed, in one read.

    Flattened rather than wrapped: the summary fields stay at the top level (``totalXp``,
    ``currentStreakDays``, ...) so a client that already reads the summary shape keeps
    working, and the feed hangs off ``xp`` beside them. Merging the two reads that a
    progress screen always made together into one round trip is the point; changing the
    summary's own shape was not.
    """

    xp: XpHistoryPage
