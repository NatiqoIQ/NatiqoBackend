"""The AI circuit breaker (P2-4).

The bulkhead caps how many calls run at once; the breaker caps how many *useless* calls
are made while the provider is down. Without it every request during an outage waits out
the whole timeout before the deterministic fallback answers — exactly when latency
matters most. These tests pin when it opens, what it refuses, and how it closes again.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.types import CEFRLevel, ContentLanguage, UserId
from app.platform.ai import service as service_module
from app.platform.ai.port import (
    AIFeature,
    AIProviderError,
    AIQuotaExceeded,
    AIRequest,
    AIResponse,
    AIUsage,
    TutorContext,
)
from app.platform.ai.service import AIService, reset_circuit_breakers

_CONTEXT = TutorContext(
    user_id=UserId("11111111-1111-1111-1111-111111111111"),
    level=CEFRLevel.A1,
    explanation_language=ContentLanguage.EN,
)
_REQUEST = AIRequest(system_prompt="system", user_prompt="hello", feature=AIFeature.TUTOR_CHAT)


class _FlakyProvider:
    name = "openai"

    def __init__(self, *, failures: int = 0, error: type[Exception] = AIProviderError) -> None:
        self.calls = 0
        self.failures = failures
        self.error = error

    async def complete(self, request: AIRequest, *, context: TutorContext) -> AIResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error("provider down")
        return AIResponse(
            text="model reply",
            usage=AIUsage(
                user_id=context.user_id,
                feature=request.feature,
                provider="openai",
                model="test-model",
            ),
            model="test-model",
            provider="openai",
        )

    async def ping(self) -> bool:
        return True


def _service(provider: Any, *, threshold: int = 2, reset_seconds: float = 30.0) -> AIService:
    reset_circuit_breakers()
    service = AIService(provider)
    service._settings.ai_enabled = True
    service._settings.ai_circuit_failure_threshold = threshold
    service._settings.ai_circuit_reset_seconds = reset_seconds
    return service


@pytest.mark.asyncio
async def test_the_breaker_opens_after_the_threshold_and_stops_calling_the_provider() -> None:
    provider = _FlakyProvider(failures=99)
    service = _service(provider, threshold=2)

    for _ in range(2):
        with pytest.raises(AIProviderError):
            await service._call_provider(_REQUEST, _CONTEXT)

    with pytest.raises(AIProviderError) as raised:
        await service._call_provider(_REQUEST, _CONTEXT)

    assert "circuit open" in str(raised.value)
    assert provider.calls == 2, "an open breaker must not spend another provider call"


@pytest.mark.asyncio
async def test_a_success_closes_the_breaker() -> None:
    provider = _FlakyProvider(failures=2)
    service = _service(provider, threshold=2, reset_seconds=0)

    for _ in range(2):
        with pytest.raises(AIProviderError):
            await service._call_provider(_REQUEST, _CONTEXT)

    # reset_seconds=0 makes the next call the half-open probe, which succeeds.
    response = await service._call_provider(_REQUEST, _CONTEXT)
    assert response.text == "model reply"

    # Closed again: the following call is allowed even though it would have been blocked
    # while the breaker was open.
    assert (await service._call_provider(_REQUEST, _CONTEXT)).text == "model reply"
    assert provider.calls == 4


@pytest.mark.asyncio
async def test_the_breaker_waits_for_the_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr(service_module.time, "monotonic", lambda: now["value"])

    provider = _FlakyProvider(failures=2)
    service = _service(provider, threshold=2, reset_seconds=30)

    for _ in range(2):
        with pytest.raises(AIProviderError):
            await service._call_provider(_REQUEST, _CONTEXT)

    now["value"] += 29
    with pytest.raises(AIProviderError) as early:
        await service._call_provider(_REQUEST, _CONTEXT)
    assert "circuit open" in str(early.value)

    now["value"] += 2
    response = await service._call_provider(_REQUEST, _CONTEXT)
    assert response.text == "model reply"


@pytest.mark.asyncio
async def test_a_quota_answer_is_not_an_outage() -> None:
    """``AIQuotaExceeded`` means the provider is healthy and saying no; opening the
    breaker would turn a per-user budget into a platform-wide one."""
    provider = _FlakyProvider(failures=99, error=AIQuotaExceeded)
    service = _service(provider, threshold=1)

    for _ in range(3):
        with pytest.raises(AIQuotaExceeded):
            await service._call_provider(_REQUEST, _CONTEXT)

    assert provider.calls == 3


@pytest.mark.asyncio
async def test_the_tutor_falls_back_once_the_breaker_is_open() -> None:
    """The breaker is invisible to the learner: the deterministic reply still arrives."""
    provider = _FlakyProvider(failures=99)
    service = _service(provider, threshold=1)

    first = await service.tutor_reply(context=_CONTEXT, history=[], message="What is a noun?")
    second = await service.tutor_reply(context=_CONTEXT, history=[], message="What is a noun?")

    assert first["source"] == "authored"
    assert second["source"] == "authored"
    assert provider.calls == 1
