"""Wire schemas for the AI tutor surface.

Fields are listed by hand, the same way the catalogue, quiz and review surfaces do, so a
field added to a read model cannot silently start leaking. ``source`` is the one to notice:
it is present on every AI-flavoured response so a client can tell the learner — honestly —
whether they read model output or a deterministic fallback, rather than mislabelling one as
the other.

``from_attributes`` (via ``_AiWire``) lets the routes hand a read model straight to a
response model, exactly as ``review_schemas`` does.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import ConfigDict, Field

from app.core.responses import WireModel
from app.modules.quiz.presentation.schemas import AnswerPayload

__all__ = [
    "AiQuotaResponse",
    "ConversationResponse",
    "ExplainRequest",
    "ExplanationResponse",
    "MessageResponse",
    "TutorReplyResponse",
    "TutorRequest",
]

#: Upper bound on a tutor message. The platform truncates to ``AI_MAX_INPUT_CHARS`` as a
#: second line of defence; rejecting at the edge gives a clean 422 instead of a silent cut.
MAX_MESSAGE_CHARS = 4000


class _AiWire(WireModel):
    """A wire model that reads attributes off a read model."""

    model_config = ConfigDict(from_attributes=True)


class ExplainRequest(WireModel):
    """Ask the tutor to explain one answer to one question."""

    question_id: uuid.UUID
    answer: AnswerPayload


class ExplanationResponse(_AiWire):
    """The verdict on an answer, plus the rationale the learner reads now.

    The answer key and the rationale appear only here, after the learner has committed an
    answer — the same contract the attempt engine keeps. ``source`` says whether a model or
    the authored CMS text produced the explanation.
    """

    is_correct: bool
    correct_answer: dict[str, str] = Field(default_factory=dict)
    explanation: str
    source: str


class TutorRequest(WireModel):
    """One turn in the tutor chat.

    ``conversationId`` continues an existing thread; omitting it opens a new one. ``lessonId``
    is an optional anchor so the tutor can keep guidance on the lesson the learner is in.
    """

    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    conversation_id: uuid.UUID | None = None
    lesson_id: uuid.UUID | None = None


class TutorReplyResponse(_AiWire):
    conversation_id: uuid.UUID
    reply: str
    source: str


class MessageResponse(_AiWire):
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime


class ConversationResponse(_AiWire):
    """A whole thread, oldest turn first."""

    id: uuid.UUID
    lesson_id: uuid.UUID | None = None
    messages: list[MessageResponse] = Field(default_factory=list)


class AiQuotaResponse(_AiWire):
    """The learner's AI allowance for today, and whether the feature is live at all.

    ``limitUsd`` of ``0`` means unmetered, matching the budget port's convention — the same
    value a deployment that has not set a budget returns.
    """

    enabled: bool
    allowed: bool
    limit_usd: float = Field(ge=0)
    spent_usd: float = Field(ge=0)
    remaining_usd: float = Field(ge=0)
    reason: str | None = None
