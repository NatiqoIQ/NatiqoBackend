"""Read models for the AI tutor surface.

Kept apart from the wire schemas the same way the attempt engine's are: a view is what the
service returns, and ``presentation/schemas.py`` repeats the field list by hand so a field
added here cannot silently start crossing the wire.

:class:`ExplanationView` deliberately has no ``authored_explanation`` field. The authored
rationale is handed to the model as grounding and may come back reworded; the learner sees
one explanation, and whether it came from the model or the CMS is reported as ``source``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "AiQuotaView",
    "ConversationView",
    "ExplanationView",
    "MessageView",
    "TutorReplyView",
]


@dataclass(frozen=True, slots=True)
class ExplanationView:
    """The verdict on one question, plus the rationale the learner reads now."""

    is_correct: bool
    correct_answer: dict[str, str]
    explanation: str
    #: ``"ai"`` when a model wrote it, ``"authored"`` when the CMS text was used.
    source: str


@dataclass(frozen=True, slots=True)
class MessageView:
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationView:
    """A whole thread, oldest turn first — what a learner reopening the chat sees."""

    id: uuid.UUID
    lesson_id: uuid.UUID | None
    messages: tuple[MessageView, ...]


@dataclass(frozen=True, slots=True)
class TutorReplyView:
    conversation_id: uuid.UUID
    reply: str
    source: str


@dataclass(frozen=True, slots=True)
class AiQuotaView:
    """How much of the learner's daily AI allowance is left, for the UI to show."""

    enabled: bool
    allowed: bool
    limit_usd: float
    spent_usd: float
    remaining_usd: float
    reason: str | None
