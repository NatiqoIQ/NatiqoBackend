"""AI port and shared value objects.

AI is an **enhancement, never a dependency** (plan §59). Everything below can be
disabled, and every call site must have a deterministic fallback. Provider SDKs stay
behind this port so a provider swap never touches application code (plan §22).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.core.types import CEFRLevel, ContentLanguage, UserId


class AIProviderName(StrEnum):
    NONE = "none"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class AIFeature(StrEnum):
    """Cost-attribution buckets. One user-facing feature per bucket."""

    EXPLAIN_ANSWER = "explain_answer"
    WRITING_CORRECTION = "writing_correction"
    TUTOR_CHAT = "tutor_chat"
    SPEAKING_FEEDBACK = "speaking_feedback"
    CONTENT_HELPER = "content_helper"


@dataclass(frozen=True, slots=True)
class AIUsage:
    """Accounting record persisted for every AI call."""

    user_id: UserId
    feature: AIFeature
    provider: AIProviderName
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: Decimal = Decimal("0")
    status: str = "ok"
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class AIRequest:
    system_prompt: str
    user_prompt: str
    #: Cost-attribution bucket. Set by the caller, not inferred from prompt text.
    feature: AIFeature = AIFeature.CONTENT_HELPER
    max_output_tokens: int = 800
    temperature: float = 0.3
    #: JSON schema hint. When set, the adapter asks the provider for JSON output.
    response_format: str | None = None


@dataclass(frozen=True, slots=True)
class AIResponse:
    text: str
    usage: AIUsage
    model: str
    provider: AIProviderName
    #: Parsed payload when the caller requested JSON.
    data: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TutorContext:
    """Everything the tutor is allowed to know about the learner."""

    user_id: UserId
    level: CEFRLevel
    explanation_language: ContentLanguage
    current_lesson_title: str | None = None
    current_skill: str | None = None
    recent_mistakes: Sequence[str] = ()
    vocabulary_hints: Sequence[str] = ()


@runtime_checkable
class AIProvider(Protocol):
    """A single text-generation backend."""

    name: AIProviderName

    async def complete(self, request: AIRequest, *, context: TutorContext) -> AIResponse: ...

    async def ping(self) -> bool: ...


@runtime_checkable
class AIServicePort(Protocol):
    """What application code is allowed to ask for.

    Every method returns a dict with a ``source`` key so callers can tell whether the
    learner saw AI output or the deterministic fallback, and render provenance.
    """

    @property
    def enabled(self) -> bool: ...

    async def explain_answer(
        self,
        *,
        context: TutorContext,
        question_text: str,
        learner_answer: str,
        correct_answer: str,
        is_correct: bool,
        explanation_en: str | None = None,
        explanation_iq: str | None = None,
    ) -> dict[str, Any]: ...

    async def correct_writing(
        self,
        *,
        context: TutorContext,
        text: str,
        task: str | None = None,
    ) -> dict[str, Any]: ...

    async def tutor_reply(
        self,
        *,
        context: TutorContext,
        history: Sequence[dict[str, str]],
        message: str,
    ) -> dict[str, Any]: ...


class AIQuotaExceeded(RuntimeError):
    """Raised when a user or the platform exhausted its AI budget."""


class AIProviderError(RuntimeError):
    """Raised when the upstream provider fails. Never propagated to the learner."""
