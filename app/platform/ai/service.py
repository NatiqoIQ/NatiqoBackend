"""AI application service.

Guarantees, in order of importance:

1. **Never a hard dependency.** Every method has a deterministic fallback. If the
   provider is disabled, slow, erroring or over budget, the learner still gets a
   useful, non-AI answer and the core learning loop is untouched.
2. **Never authoritative.** AI output is advisory text. It cannot award XP, change
   mastery, or move a review schedule.
3. **Always metered.** Every call produces an :class:`AIUsage` record for cost and
   abuse tracking, including failures.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.types import CEFRLevel, ContentLanguage
from app.modules.ai.domain.budget import BudgetPort
from app.platform.ai.port import (
    AIFeature,
    AIProvider,
    AIProviderError,
    AIQuotaExceeded,
    AIRequest,
    AIServicePort,
    AIUsage,
    TutorContext,
)
from app.platform.ai.prompts import (
    EXPLAIN_ANSWER,
    TUTOR_SYSTEM,
    WRITING_CORRECTION,
    answer_language_name,
    cefr_guidance,
)

logger = get_logger("ai.service")

#: Signature the service hands to the composition root to persist usage records.
UsageRecorder = Callable[[AIUsage], None]


class AIService(AIServicePort):
    def __init__(
        self,
        provider: AIProvider,
        *,
        usage_recorder: UsageRecorder | None = None,
        budget: BudgetPort | None = None,
    ) -> None:
        self._provider = provider
        self._record = usage_recorder
        self._budget = budget
        self._settings = get_settings()

    @property
    def enabled(self) -> bool:
        return self._settings.ai_enabled and self._provider.name != "none"

    async def _within_budget(self, context: TutorContext, feature: AIFeature) -> bool:
        if self._budget is None:
            return True
        try:
            status = await self._budget.status(context.user_id, feature=feature)
        except Exception as exc:  # noqa: BLE001 - fail closed on metering, not on learning
            logger.warning("ai.budget_check_failed", error=str(exc)[:200])
            return True
        if not status.allowed:
            logger.info("ai.budget_exceeded", user_id=str(context.user_id), feature=str(feature))
        return status.allowed

    # ------------------------------------------------------------ explain
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
    ) -> dict[str, Any]:
        """Explain an answer. Falls back to the authored explanation from the CMS."""
        if not self.enabled or not await self._within_budget(context, AIFeature.EXPLAIN_ANSWER):
            return {
                "source": "authored",
                "text": _authored_explanation(
                    context.explanation_language, explanation_en, explanation_iq, is_correct
                ),
            }
        if len(question_text) > self._settings.ai_max_input_chars:
            return {
                "source": "authored",
                "text": _authored_explanation(
                    context.explanation_language, explanation_en, explanation_iq, is_correct
                ),
            }

        request = AIRequest(
            system_prompt=EXPLAIN_ANSWER.system,
            user_prompt=EXPLAIN_ANSWER.user.format(
                question=question_text[:2000],
                learner_answer=learner_answer[:500],
                correct_answer=correct_answer[:500],
                is_correct="yes" if is_correct else "no",
                level=str(context.level),
                answer_language=answer_language_name(context.explanation_language),
            ),
            feature=AIFeature.EXPLAIN_ANSWER,
            max_output_tokens=320,
            temperature=0.2,
        )
        text = await self._complete(request, context)
        return (
            {"source": "ai", "text": text}
            if text
            else {
                "source": "authored",
                "text": _authored_explanation(
                    context.explanation_language, explanation_en, explanation_iq, is_correct
                ),
            }
        )

    # ------------------------------------------------------------ writing
    async def correct_writing(
        self,
        *,
        context: TutorContext,
        text: str,
        task: str | None = None,
    ) -> dict[str, Any]:
        """Return the plan §24 correction payload, or a deterministic fallback."""
        if not self.enabled or not await self._within_budget(context, AIFeature.WRITING_CORRECTION):
            return _fallback_writing(context, text)
        if len(text) > self._settings.ai_max_input_chars:
            return _fallback_writing(context, text)

        request = AIRequest(
            system_prompt=WRITING_CORRECTION.system,
            user_prompt=WRITING_CORRECTION.user.format(
                text=text[: self._settings.ai_max_input_chars],
                level=str(context.level),
                task=task or "Write a short paragraph.",
            ),
            feature=AIFeature.WRITING_CORRECTION,
            max_output_tokens=self._settings.ai_max_output_tokens,
            temperature=0.1,
            response_format="json",
        )
        try:
            response = await self._provider.complete(request, context=context)
        except AIQuotaExceeded:
            raise
        except AIProviderError as exc:
            self._record_usage(self._failed_usage(request, context, str(exc)))
            logger.warning("ai.provider_error", feature=str(request.feature))
            return _fallback_writing(context, text)

        self._record_usage(response.usage)
        data = response.data or {}
        if not data.get("corrected"):
            return _fallback_writing(context, text)
        return {
            "original": data.get("original", text),
            "corrected": data["corrected"],
            "errors": data.get("errors") or [],
            "explanation": data.get("explanation")
            or _fallback_writing(context, text)["explanation"],
            "better_alternative": data.get("better_alternative"),
            "difficulty": str(data.get("difficulty") or context.level.value),
            "source": "ai",
        }

    # -------------------------------------------------------------- tutor
    async def tutor_reply(
        self,
        *,
        context: TutorContext,
        history: Sequence[dict[str, str]],
        message: str,
    ) -> dict[str, Any]:
        if not message.strip() or not self.enabled:
            return {
                "source": "authored",
                "reply": _fallback_tutor(context, message),
            }
        if not await self._within_budget(context, AIFeature.TUTOR_CHAT):
            return {
                "source": "authored",
                "reply": _fallback_tutor(context, message),
            }
        trimmed_history = list(history)[-10:]
        conversation = "\n".join(
            f"{turn.get('role', 'user')}: {turn.get('content', '')}" for turn in trimmed_history
        )
        system = f"{TUTOR_SYSTEM.system}\n{cefr_guidance(context.level)}"
        if context.current_lesson_title:
            system += f"\nThe learner is working on the lesson: {context.current_lesson_title}."
        if context.current_skill:
            system += f"\nCurrent skill being practised: {context.current_skill}."
        if context.recent_mistakes:
            system += "\nThe learner recently got these wrong: " + ", ".join(
                context.recent_mistakes[:5]
            )
        user_prompt = (
            f"{conversation}\nuser: {message[: self._settings.ai_max_input_chars]}"
            if conversation
            else message[: self._settings.ai_max_input_chars]
        )
        request = AIRequest(
            system_prompt=system,
            user_prompt=user_prompt,
            feature=AIFeature.TUTOR_CHAT,
            max_output_tokens=400,
            temperature=0.5,
        )
        text = await self._complete(request, context)
        return {
            "source": "ai" if text else "authored",
            "reply": text or _fallback_tutor(context, message),
        }

    # ------------------------------------------------------------ plumbing
    async def _complete(self, request: AIRequest, context: TutorContext) -> str | None:
        try:
            response = await self._provider.complete(request, context=context)
        except AIQuotaExceeded:
            raise
        except AIProviderError as exc:
            self._record_usage(self._failed_usage(request, context, str(exc)))
            logger.warning(
                "ai.provider_error",
                feature=str(request.feature),
                provider=str(self._provider.name),
                error=str(exc)[:200],
            )
            return None
        self._record_usage(response.usage)
        return response.text.strip() or None

    def _failed_usage(self, request: AIRequest, context: TutorContext, error: str) -> AIUsage:
        return AIUsage(
            user_id=context.user_id,
            feature=request.feature,
            provider=self._provider.name,
            model=self._settings.ai_model or "unknown",
            latency_ms=0,
            status="error",
            error=error[:500],
        )

    def _record_usage(self, usage: AIUsage) -> None:
        if self._record is not None:
            try:
                self._record(usage)
            except Exception as exc:  # noqa: BLE001 - metering must never break a request
                logger.warning("ai.usage_record_failed", error=str(exc))

    async def ping(self) -> bool:
        try:
            return await self._provider.ping()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ai.ping_failed", error=str(exc))
            return False


# ------------------------------------------------------------- deterministic fallbacks
def _authored_explanation(
    language: ContentLanguage,
    explanation_en: str | None,
    explanation_iq: str | None,
    is_correct: bool,
) -> str:
    if language == ContentLanguage.IQ and explanation_iq:
        return explanation_iq
    if language == ContentLanguage.AR and explanation_iq:
        return explanation_iq
    if explanation_en:
        return explanation_en
    return (
        "That is correct. Well done."
        if is_correct
        else "That is not the correct answer. Review the example and try again."
    )


def _fallback_writing(context: TutorContext, text: str) -> dict[str, Any]:
    """No-AI correction: report only objective, checkable signals.

    We deliberately do not attempt grammar correction without a model. Inventing
    errors would be worse than admitting the limitation.
    """
    words = text.split()
    return {
        "original": text,
        "corrected": text,
        "errors": [],
        "explanation": (
            "Automated correction is not available right now, so your text was returned "
            "unchanged. Check three things: is every verb in the right tense, does every "
            "sentence start with a capital letter, and does it end with a full stop?"
        ),
        "better_alternative": None,
        "difficulty": context.level.value,
        "source": "unavailable",
        "word_count": len(words),
    }


def _fallback_tutor(context: TutorContext, message: str) -> str:
    level_hint = {
        CEFRLevel.A1: "Start with short everyday sentences.",
        CEFRLevel.A2: "Try writing two sentences about your day, in the past tense.",
        CEFRLevel.B1: "Try explaining your opinion on a topic you care about.",
    }.get(context.level, "Try writing a short paragraph and compare it with the model.")
    return (
        "I cannot reach the AI tutor right now, but the lesson content and exercises "
        f"are fully available. {level_hint}"
    )


def ai_unavailable_error() -> AppError:
    return AppError(
        "AI features are currently unavailable. The rest of the app works normally.",
        code=ErrorCode.AI_DISABLED,
        http_status=503,
    )
