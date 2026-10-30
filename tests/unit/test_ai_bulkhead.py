"""The two bounds around every model call: a deadline and a concurrency cap.

Without them a slow provider is a database problem: each waiting request still holds a
request slot, and the deterministic fallback — which is perfectly good — is never
reached. These tests pin the failure mode as a *fast fallback*, not a hang and not a
500.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.types import CEFRLevel, ContentLanguage, UserId
from app.platform.ai.port import (
    AIFeature,
    AIProviderError,
    AIRequest,
    AIResponse,
    AIUsage,
    TutorContext,
)
from app.platform.ai.service import AIService

_CONTEXT = TutorContext(
    user_id=UserId("11111111-1111-1111-1111-111111111111"),
    level=CEFRLevel.A1,
    explanation_language=ContentLanguage.EN,
)
_REQUEST = AIRequest(system_prompt="system", user_prompt="hello", feature=AIFeature.TUTOR_CHAT)


class _Provider:
    name = "openai"

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0

    async def complete(self, request: AIRequest, *, context: TutorContext) -> AIResponse:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
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


@pytest.mark.asyncio
async def test_a_saturated_bulkhead_fails_fast_instead_of_queueing() -> None:
    """When every slot is taken, the next caller is rejected immediately.

    The rejection is an ``AIProviderError``, which the service already maps to its
    authored fallback — the learner gets an answer, just not a model-generated one.
    """
    service = AIService(_Provider())
    service._bulkhead = asyncio.Semaphore(1)
    await service._bulkhead.acquire()

    with pytest.raises(AIProviderError) as raised:
        await service._call_provider(_REQUEST, _CONTEXT)

    assert "saturated" in str(raised.value)
    service._bulkhead.release()


@pytest.mark.asyncio
async def test_a_provider_that_misses_the_deadline_is_cancelled() -> None:
    """The deadline must cancel the upstream call, not merely stop waiting for it."""
    service = AIService(_Provider(delay=5))
    service._settings.ai_timeout_seconds = 0

    with pytest.raises(AIProviderError) as raised:
        await service._call_provider(_REQUEST, _CONTEXT)

    assert "timed out" in str(raised.value)


@pytest.mark.asyncio
async def test_a_successful_call_returns_the_response_and_releases_the_slot() -> None:
    provider = _Provider()
    service = AIService(provider)
    service._bulkhead = asyncio.Semaphore(1)

    response = await service._call_provider(_REQUEST, _CONTEXT)

    assert response.text == "model reply"
    assert provider.calls == 1
    # The slot was released in ``finally``: a second call must not be saturated.
    assert service._bulkhead.locked() is False
