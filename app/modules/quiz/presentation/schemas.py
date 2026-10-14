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

from pydantic import Field

from app.core.responses import WireModel
from app.core.types import QuestionKind, SkillKind
from app.modules.quiz.domain.entities import SessionMode

__all__ = [
    "AnswerPayload",
    "CompletedSessionResponse",
    "GradedAnswerResponse",
    "SessionDetailResponse",
    "SessionQuestionResponse",
    "SessionResponse",
    "StartSessionRequest",
    "SubmitAnswerRequest",
]


class StartSessionRequest(WireModel):
    """Open a sitting. ``lessonId`` is required for the only mode that exists today."""

    mode: SessionMode = SessionMode.LESSON
    lesson_id: uuid.UUID | None = None


class AnswerPayload(WireModel):
    """What a learner chose or typed. The service checks that it matches the question."""

    choice: str | None = None
    text: str | None = None


class SubmitAnswerRequest(WireModel):
    question_id: uuid.UUID
    answer: AnswerPayload


class SessionQuestionResponse(WireModel):
    """A question as it appears inside a live session — with no answer key."""

    id: uuid.UUID
    position: int
    kind: QuestionKind
    skill: SkillKind
    prompt: str
    choices: list[str]
    points: int = Field(ge=1)
    image_url: str | None = None
    audio_url: str | None = None


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
    session: SessionResponse
    score_percent: int = Field(ge=0, le=100)
