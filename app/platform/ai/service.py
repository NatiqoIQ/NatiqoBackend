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

import asyncio
import time
import weakref
from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.types import CEFRLevel, ContentLanguage
from app.modules.ai.domain.budget import BudgetPort
from app.platform.ai.port import (
    AIFeature,
    AIProvider,
    AIProviderError,
    AIQuotaExceeded,
    AIRequest,
    AIResponse,
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

#: One semaphore per event loop, shared by every request-scoped ``AIService``.
#:
#: The service cannot own this: it is rebuilt for every request (its budget reads the
#: request's transaction), so an instance attribute would give each request its own cap
#: and ``AI_MAX_CONCURRENT_REQUESTS`` would never bind. Keyed weakly by loop so a test
#: loop that ends does not leak its semaphore into the next one.
_SHARED_BULKHEADS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


class _CircuitBreaker:
    """Stop calling a provider that is failing, for a bounded cooldown.

    The bulkhead bounds *concurrency*; this bounds *waste*. During a provider outage every
    request would otherwise spend the whole timeout before falling back, which is exactly
    when latency matters most and when the fallback text is already known to be good.

    One breaker per event loop, like the bulkhead, because the service is request-scoped:
    a per-instance breaker would reset on every request and never open.
    """

    def __init__(self) -> None:
        self._failures = 0
        self._opened_at: float | None = None

    def allow(self, *, threshold: int, reset_seconds: float, now: float) -> bool:
        if self._opened_at is None:
            return True
        if now - self._opened_at >= reset_seconds:
            # Half-open: let the next call through as the probe. A success closes the
            # breaker; a failure re-opens it immediately (see ``record_failure``).
            self._opened_at = now
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self, *, threshold: int, now: float) -> None:
        self._failures += 1
        if self._opened_at is not None or self._failures >= threshold:
            # Re-open on a failed half-open probe, or open on the threshold-th failure.
            self._opened_at = now

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None


#: One breaker per event loop, for the same reason as the bulkhead above.
_SHARED_BREAKERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _CircuitBreaker] = (
    weakref.WeakKeyDictionary()
)


def _shared_breaker() -> _CircuitBreaker:
    loop = asyncio.get_running_loop()
    breaker = _SHARED_BREAKERS.get(loop)
    if breaker is None:
        breaker = _CircuitBreaker()
        _SHARED_BREAKERS[loop] = breaker
    return breaker


def circuit_open() -> bool:
    """Whether the provider breaker for the running loop is currently open.

    Read by ``/metrics``. It is synchronous on purpose: the exposition renderer is a
    normal function called from a request handler, so the loop is already running.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - called from a request, so a loop exists
        return False
    breaker = _SHARED_BREAKERS.get(loop)
    return breaker.is_open if breaker is not None else False


def reset_circuit_breakers() -> None:
    """Drop every breaker. Tests need a known state; a live process never calls this."""
    _SHARED_BREAKERS.clear()


def _shared_bulkhead() -> asyncio.Semaphore:
    """The process-wide slot pool for the running loop.

    Called from async code only, so ``get_running_loop`` is always available. Creating
    the semaphore is synchronous, and the event loop is single-threaded, so two coroutines
    cannot both install one for the same loop.
    """
    loop = asyncio.get_running_loop()
    semaphore = _SHARED_BULKHEADS.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(max(1, get_settings().ai_max_concurrent_requests))
        _SHARED_BULKHEADS[loop] = semaphore
    return semaphore


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
        #: Test seam. Production always resolves the loop-wide semaphore below; a test
        #: that needs a known width assigns here instead of mutating global state.
        self._bulkhead: asyncio.Semaphore | None = None

    def _slots(self) -> asyncio.Semaphore:
        """A bulkhead, not a queue.

        Waiting behind an unlimited number of slow provider calls is how an AI outage
        becomes an API outage: each wait holds a request slot and the fallback (which is
        good) is never reached. The cap is shared by every request because the service
        is request-scoped — see :data:`_SHARED_BULKHEADS`.
        """
        if self._bulkhead is not None:
            return self._bulkhead
        return _shared_bulkhead()

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
            response = await self._call_provider(request, context)
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
    async def _call_provider(self, request: AIRequest, context: TutorContext) -> AIResponse:
        """One bounded call: a concurrency slot, a wall-clock deadline, then the provider.

        Both bounds raise :class:`AIProviderError`, which every caller already maps to
        its deterministic fallback. ``asyncio.wait_for`` cancels the HTTP request when
        the deadline passes, so the provider connection is closed rather than left to
        finish invisibly.
        """
        breaker = _shared_breaker()
        now = time.monotonic()
        if not breaker.allow(
            threshold=self._settings.ai_circuit_failure_threshold,
            reset_seconds=self._settings.ai_circuit_reset_seconds,
            now=now,
        ):
            # No slot is taken and no HTTP call is made: the fallback is already known.
            raise AIProviderError("ai circuit open after repeated provider failures")

        slots = self._slots()
        try:
            await asyncio.wait_for(slots.acquire(), timeout=0.05)
        except TimeoutError as exc:
            # Saturation is our own bulkhead, not evidence that the provider is down.
            raise AIProviderError("ai provider saturated: too many concurrent requests") from exc
        try:
            try:
                response = await asyncio.wait_for(
                    self._provider.complete(request, context=context),
                    timeout=self._settings.ai_timeout_seconds,
                )
            except AIQuotaExceeded:
                # A quota answer is a working provider telling us "no", not an outage.
                breaker.record_success()
                raise
            except TimeoutError as exc:
                breaker.record_failure(
                    threshold=self._settings.ai_circuit_failure_threshold,
                    now=time.monotonic(),
                )
                raise AIProviderError("ai provider timed out") from exc
            except AIProviderError:
                breaker.record_failure(
                    threshold=self._settings.ai_circuit_failure_threshold,
                    now=time.monotonic(),
                )
                raise
            breaker.record_success()
            return response
        finally:
            slots.release()

    async def _complete(self, request: AIRequest, context: TutorContext) -> str | None:
        try:
            response = await self._call_provider(request, context)
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
