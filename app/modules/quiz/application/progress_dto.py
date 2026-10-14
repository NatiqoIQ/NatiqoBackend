"""Read models for the progress surface.

Separate from ``dto.py`` because these views are assembled from the *durable* record the
engine leaves behind — ``lesson_progress`` rows and finished sessions — rather than from a
live sitting. Nothing here can leak an answer key: a progress view only ever restates what
the learner has already been told.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "LessonProgressView",
    "ProgressSummaryView",
    "UnlockMapItemView",
    "XpEventView",
]


@dataclass(frozen=True, slots=True)
class ProgressSummaryView:
    """The headline figures for a learner's progress screen.

    Every field is derivable today; nothing is projected. Hearts and "reviews due" are
    deliberately absent — they belong to phases that do not have a backing store yet — so
    a client cannot render a number the server would be inventing.
    """

    total_xp: int
    lessons_completed: int
    lessons_in_progress: int
    lessons_started: int
    correct_count: int
    answered_count: int
    accuracy_percent: int
    current_streak_days: int
    longest_streak_days: int
    last_activity_at: datetime | None


@dataclass(frozen=True, slots=True)
class LessonProgressView:
    """One lesson's placement plus the learner's record against it.

    ``state`` and ``locked`` are separate on purpose: ``state`` is what the learner has
    done (``available`` / ``in_progress`` / ``completed``), ``locked`` is whether the
    curriculum currently lets them start. With no gating rule yet, ``locked`` is always
    ``false`` — but it is a field from day one so enabling a rule later is a data change,
    not a wire change.
    """

    lesson_id: uuid.UUID
    unit_id: uuid.UUID
    level_code: str
    title: str
    position: int
    state: str
    locked: bool
    best_score: int
    attempts: int
    xp_earned: int
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class UnlockMapItemView:
    """One node of the curriculum-ordered progress map.

    Carries the learner's record for the lesson — ``best_score``, ``attempts`` and
    ``xp_earned`` — not just enough to colour the node. The map is the only place a
    single lesson's record is read now that the per-lesson endpoint is gone, so the
    record travels with it rather than needing a second request the client would have to
    join back onto the map itself.
    """

    lesson_id: uuid.UUID
    unit_id: uuid.UUID
    level_code: str
    title: str
    position: int
    state: str
    locked: bool
    best_score: int
    attempts: int
    xp_earned: int
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class XpEventView:
    """One completed session, as a row of the XP history feed."""

    session_id: uuid.UUID
    lesson_id: uuid.UUID | None
    xp_awarded: int
    correct_count: int
    total_count: int
    score_percent: int
    completed_at: datetime | None
