"""Concrete AI providers.

Implemented over plain HTTP with ``httpx`` rather than vendor SDKs: it keeps the
dependency surface small, makes the timeout/retry policy explicit, and means a
provider change never requires a new package. Both adapters are imported lazily and
are only constructed when ``AI_ENABLED`` is true and a key is present.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.platform.ai.port import (
    AIProviderError,
    AIProviderName,
    AIQuotaExceeded,
    AIRequest,
    AIResponse,
    AIUsage,
    TutorContext,
)

logger = get_logger("ai.provider")

#: Published list prices (USD per 1M tokens) used for cost attribution only. They are
#: configuration, not billing truth, and are easy to correct.
_MODEL_PRICING: dict[tuple[str, str], tuple[Decimal, Decimal]] = {
    ("openai", "gpt-4o-mini"): (Decimal("0.15"), Decimal("0.60")),
    ("openai", "gpt-4o"): (Decimal("2.50"), Decimal("10.00")),
    ("anthropic", "claude-3-5-haiku-latest"): (Decimal("0.80"), Decimal("4.00")),
    ("anthropic", "claude-3-5-sonnet-latest"): (Decimal("3.00"), Decimal("15.00")),
}
_FALLBACK_PRICE = (Decimal("1.00"), Decimal("3.00"))


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> Decimal:
    in_price, out_price = _MODEL_PRICING.get((provider, model), _FALLBACK_PRICE)
    millions = Decimal("1000000")
    return (Decimal(input_tokens) * in_price + Decimal(output_tokens) * out_price) / millions


class _BaseHttpProvider:
    name: AIProviderName

    def __init__(self, client: Any) -> None:
        self._client = client
        self._settings = get_settings()

    async def _timed(self, coroutine_factory: Any) -> tuple[Any, int, int]:
        started = time.perf_counter()
        try:
            result = await coroutine_factory()
        except Exception as exc:
            raise AIProviderError(str(exc)) from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        return result, latency_ms, 0

    def _model(self) -> str:
        return self._settings.ai_model or self.default_model

    default_model: str = ""

    async def ping(self) -> bool:
        return True


class OpenAIProvider(_BaseHttpProvider):
    """OpenAI chat-completions compatible endpoint (also covers local gateways)."""

    name = AIProviderName.OPENAI
    default_model = "gpt-4o-mini"
    endpoint = "https://api.openai.com/v1/chat/completions"

    @classmethod
    def from_settings(cls) -> OpenAIProvider:
        import httpx

        settings = get_settings()
        if not settings.ai_api_key:
            raise AIProviderError("AI_API_KEY is not configured")
        client = httpx.AsyncClient(
            timeout=settings.ai_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.ai_api_key.get_secret_value()}"},
        )
        return cls(client)

    async def complete(self, request: AIRequest, *, context: TutorContext) -> AIResponse:
        model = self._model()
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }
        if request.response_format:
            payload["response_format"] = {"type": "json_object"}

        async def _call() -> dict[str, Any]:
            response = await self._client.post(self.endpoint, json=payload)
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]

        data, latency_ms, _ = await self._timed(_call)
        text = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        return AIResponse(
            text=text,
            model=model,
            provider=self.name,
            data=_parse_json(text) if request.response_format else None,
            usage=AIUsage(
                user_id=context.user_id,
                feature=request.feature,
                provider=self.name,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cost_usd=estimate_cost(str(self.name), model, input_tokens, output_tokens),
            ),
        )


class AnthropicProvider(_BaseHttpProvider):
    """Anthropic Messages API."""

    name = AIProviderName.ANTHROPIC
    default_model = "claude-3-5-haiku-latest"
    endpoint = "https://api.anthropic.com/v1/messages"
    api_version = "2023-06-01"

    @classmethod
    def from_settings(cls) -> AnthropicProvider:
        import httpx

        settings = get_settings()
        if not settings.ai_api_key:
            raise AIProviderError("AI_API_KEY is not configured")
        client = httpx.AsyncClient(
            timeout=settings.ai_timeout_seconds,
            headers={
                "x-api-key": settings.ai_api_key.get_secret_value(),
                "anthropic-version": cls.api_version,
            },
        )
        return cls(client)

    async def complete(self, request: AIRequest, *, context: TutorContext) -> AIResponse:
        model = self._model()
        system = request.system_prompt
        if request.response_format:
            system = f"{system}\n\nRespond with a single valid JSON object and nothing else."
        payload: dict[str, Any] = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": request.user_prompt}],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }

        async def _call() -> dict[str, Any]:
            response = await self._client.post(self.endpoint, json=payload)
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]

        data, latency_ms, _ = await self._timed(_call)
        blocks = data.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("completion_tokens", 0))
        return AIResponse(
            text=text,
            model=model,
            provider=self.name,
            data=_parse_json(text) if request.response_format else None,
            usage=AIUsage(
                user_id=context.user_id,
                feature=request.feature,
                provider=self.name,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                cost_usd=estimate_cost(str(self.name), model, input_tokens, output_tokens),
            ),
        )


class DisabledProvider:
    """Used when AI is switched off. Every call fails fast with a clear reason."""

    name = AIProviderName.NONE

    async def complete(self, request: AIRequest, *, context: TutorContext) -> AIResponse:
        raise AIProviderError("AI is disabled for this deployment")

    async def ping(self) -> bool:
        return False


def _parse_json(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        _, _, stripped = stripped.partition("\n")
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = [
    "AIProviderError",
    "AIQuotaExceeded",
    "AnthropicProvider",
    "DisabledProvider",
    "OpenAIProvider",
    "estimate_cost",
]
