"""Wire schemas for the attempt engine.

``SessionQuestionResponse`` is the schema that matters: it lists its fields by hand and
deliberately has no ``correctIndex``, no ``acceptedAnswers`` and no ``explanation``. A
question is served *before* the learner answers it, so anything these schemas omit cannot
leak and anything they include is public to whoever holds the session id. The verdict and
the rationale live on :class:`GradedAnswerResponse`, which a client only reaches by
answering.

Naming follows the rest of the API: camelCase on the wire, via ``WireModel``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.core.responses import WireModel
from app.core.types import QuestionKind, SkillKind
from app.modules.quiz.domain.entities import SessionMode

__all__ = [
    "AbandonCommand",
    "AbandonCommandResult",
    "AnswerCommand",
    "AnswerCommandResult",
    "AnswerPayload",
    "CompleteCommand",
    "CompleteCommandResult",
    "CompletedSessionResponse",
    "GradedAnswerResponse",
    "SessionCommand",
    "SessionCommandResponse",
    "SessionDetailResponse",
    "SessionQuestionResponse",
    "SessionResponse",
    "StartSessionRequest",
]


class StartSessionRequest(WireModel):
    """Open a sitting.

    ``mode`` picks the scope: ``lesson`` needs ``lessonId``, ``checkpoint`` needs
    ``unitId``. The field the chosen mode does not use is ignored rather than rejected —
    the service records only what the mode says — so a client may keep sending both.
    """

    mode: SessionMode = SessionMode.LESSON
    lesson_id: uuid.UUID | None = None
    unit_id: uuid.UUID | None = None


class AnswerPayload(WireModel):
    """What a learner chose, typed, selected or arranged. The service checks that it matches
    the question.

    ``choice`` answers a single-option question, ``text`` a free-text one, ``choices`` a
    multi-select one, ``order`` an ordering one, and ``matching`` a matching one — as the
    ``"left => right"`` pairs the learner built, in any order. They are separate fields so a
    submission shaped for the wrong kind is a named validation error rather than a silent
    wrong answer.
    """

    choice: str | None = None
    text: str | None = None
    choices: list[str] | None = None
    order: list[str] | None = None
    matching: list[str] | None = None


class SessionQuestionResponse(WireModel):
    """A question as it appears inside a live session — with no answer key.

    ``prompts`` is filled by a matching question alone: the items to pair up, against the
    ``choices`` bank. Both are public — the pairing is what the key holds — and both are
    reordered per attempt so their positions never spell out which goes with which.
    """

    id: uuid.UUID
    position: int
    kind: QuestionKind
    skill: SkillKind
    prompt: str
    choices: list[str]
    points: int = Field(ge=1)
    image_url: str | None = None
    audio_url: str | None = None
    video_url: str | None = None
    prompts: list[str] = Field(default_factory=list)


class SessionResponse(WireModel):
    id: uuid.UUID
    mode: str
    lesson_id: uuid.UUID | None = None
    status: str
    started_at: datetime
    expires_at: datetime
    completed_at: datetime | None = None
    total_count: int = Field(ge=0)
    answered_count: int = Field(ge=0)
    correct_count: int = Field(ge=0)
    xp_awarded: int = Field(ge=0)
    #: The unit a checkpoint sitting is scored against; ``null`` for a lesson sitting,
    #: where ``lesson_id`` carries the scope instead.
    unit_id: uuid.UUID | None = None
    answered_question_ids: list[uuid.UUID] = Field(default_factory=list)


class SessionDetailResponse(WireModel):
    session: SessionResponse
    questions: list[SessionQuestionResponse] = Field(default_factory=list)


class GradedAnswerResponse(WireModel):
    """The verdict. ``correctAnswer`` and ``explanation`` are only ever sent from here."""

    is_correct: bool
    correct_answer: dict[str, str] = Field(default_factory=dict)
    explanation: str
    points_awarded: int = Field(ge=0)
    answered_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    correct_count: int = Field(ge=0)
    is_complete: bool


class CompletedSessionResponse(WireModel):
    """The finished sitting, its score, and — for a checkpoint — the verdict.

    ``passed`` and ``unlockedUnitId`` are meaningful only for a checkpoint sitting and
    come back ``null`` for a lesson. The question id lists describe either mode, and
    ``retryAvailable`` is the client's cue to offer another pass at the missed questions in
    this same sitting: true for a lesson with misses, false for a checkpoint, which is
    re-sat from the start instead.
    """

    session: SessionResponse
    score_percent: int = Field(ge=0, le=100)
    passed: bool | None = None
    unlocked_unit_id: uuid.UUID | None = None
    correct_question_ids: list[uuid.UUID] = Field(default_factory=list)
    missed_question_ids: list[uuid.UUID] = Field(default_factory=list)
    retry_available: bool = False


# ----------------------------------------------------------------- commands
#
# One route, three verbs. Answering, finishing and abandoning a sitting are all "do
# something to an open session", so they share ``POST /quiz/sessions/{id}`` and are told
# apart by an ``op`` discriminator rather than by three sibling paths. The union is the
# contract: FastAPI publishes it as a ``oneOf`` with ``op`` as the discriminator, so a
# client (and the generated Dart model) sees the three shapes and their fields up front
# instead of discovering them across three endpoints.
#
# The ``op`` default exists so the handlers can build a result by naming the class alone;
# a request must still carry ``op`` — a discriminated union will not extract a tag that
# is absent, so an unlabelled body is a 422 rather than a silent guess.


class AnswerCommand(WireModel):
    """Grade one answer and return the verdict, the correct answer and the rationale."""

    op: Literal["answer"] = "answer"
    question_id: uuid.UUID
    answer: AnswerPayload


class CompleteCommand(WireModel):
    """Score a fully answered sitting and record it against the lesson."""

    op: Literal["complete"] = "complete"


class AbandonCommand(WireModel):
    """End a sitting without scoring it. Idempotent once abandoned."""

    op: Literal["abandon"] = "abandon"


SessionCommand = Annotated[
    AnswerCommand | CompleteCommand | AbandonCommand,
    Field(discriminator="op"),
]


class AnswerCommandResult(WireModel):
    op: Literal["answer"] = "answer"
    result: GradedAnswerResponse


class CompleteCommandResult(WireModel):
    op: Literal["complete"] = "complete"
    result: CompletedSessionResponse


class AbandonCommandResult(WireModel):
    op: Literal["abandon"] = "abandon"
    result: SessionResponse


SessionCommandResponse = Annotated[
    AnswerCommandResult | CompleteCommandResult | AbandonCommandResult,
    Field(discriminator="op"),
]
