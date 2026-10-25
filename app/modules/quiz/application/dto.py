"""Read models for the attempt engine.

These are the objects the routes are built from, and :class:`QuestionView` is the one that
matters most: it is what a session hands the client *before* an answer, so it is assembled
by omission — prompt, choices, points and the media aids, and deliberately not
``correct_index``, ``accepted_answers`` or ``explanation``. Those come back from grading,
per answer, and nowhere else. ``presentation/schemas.py`` repeats the omission at the wire
layer, the same belt-and-braces the catalogue uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.core.types import QuestionKind, SkillKind

__all__ = [
    "CompletedSessionView",
    "GradedAnswerView",
    "QuestionView",
    "SessionDetailView",
    "SessionView",
]


@dataclass(frozen=True, slots=True)
class QuestionView:
    """A practice item as a learner sees it inside a live session.

    ``prompts`` is the one field only a matching question fills: the items to be paired up,
    which arrive here in the order they were authored while ``choices`` carries the bank they
    are paired against, reshuffled away from that order. Both halves of a match are therefore
    visible and only their *pairing* is withheld — which is the same bargain a multiple choice
    strikes when it shows its options, and the reason neither field is key material.
    """

    id: uuid.UUID
    position: int
    kind: QuestionKind
    skill: SkillKind
    prompt: str
    choices: tuple[str, ...]
    points: int
    image_url: str | None
    audio_url: str | None
    video_url: str | None
    prompts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionView:
    """The state of a sitting. Carries no answer key and no per-question verdict."""

    id: uuid.UUID
    mode: str
    lesson_id: uuid.UUID | None
    status: str
    started_at: datetime
    expires_at: datetime
    completed_at: datetime | None
    total_count: int
    answered_count: int
    correct_count: int
    xp_awarded: int
    #: The unit a checkpoint session is scored against — what a client titles the sitting
    #: with, since no single lesson owns it. ``None`` in every other mode.
    unit_id: uuid.UUID | None = None
    #: Which questions are already answered, so a resumed client can skip exactly those
    #: rather than replay the whole lesson. It is the caller's own data.
    answered_question_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionDetailView:
    """A session and its questions — what start and resume both return."""

    session: SessionView
    questions: tuple[QuestionView, ...]


@dataclass(frozen=True, slots=True)
class GradedAnswerView:
    """The verdict on one answer, plus the running tally."""

    is_correct: bool
    correct_answer: dict[str, str]
    explanation: str
    points_awarded: int
    answered_count: int
    total_count: int
    correct_count: int
    is_complete: bool


@dataclass(frozen=True, slots=True)
class CompletedSessionView:
    """The finished sitting, its score, and what the score unlocked.

    One shape serves both modes so a client renders a result branch-free where it can:
    ``correctQuestionIds``/``missedQuestionIds`` always describe the sitting, and
    ``passed``/``unlockedUnitId`` carry meaning only for a checkpoint (``None`` for a
    lesson, where there is no such bar). ``retryAvailable`` means "the missed questions can
    be answered again *in this sitting*", so it is true for a lesson with misses and always
    false for a checkpoint: a checkpoint seals each question on its first answer, and a
    learner who wants another go starts a new sitting over a fresh sample.
    """

    session: SessionView
    score_percent: int
    #: Whether a checkpoint cleared its passing bar. ``None`` for a lesson sitting.
    passed: bool | None = None
    #: The unit a passing checkpoint opens, or ``None`` when it is the last one (or the
    #: mode has no units to unlock).
    unlocked_unit_id: uuid.UUID | None = None
    correct_question_ids: tuple[uuid.UUID, ...] = ()
    missed_question_ids: tuple[uuid.UUID, ...] = ()
    retry_available: bool = False
