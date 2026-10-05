"""AI usage metering.

The AI service is synchronous-ish and must never block a request on bookkeeping, so
this recorder:

- writes through its own short-lived session, never the request's unit of work;
- swallows its own failures (a dropped usage row must not fail a lesson);
- keeps a bounded in-process buffer so metering still works if the database is down.

In-flight writes are tracked so the application lifespan can drain them on shutdown.
Without that, the last few usage rows of every deploy would be lost.
"""

from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.platform.ai.port import AIUsage

logger = get_logger("ai.usage")

#: Recent usage kept in memory. Only used for emergency logging when the DB is down.
_BUFFER: deque[dict[str, Any]] = deque(maxlen=200)

#: Strong references to in-flight persistence tasks. Without holding a reference the
#: event loop is free to garbage-collect a running task mid-write.
_PENDING: set[asyncio.Task[None]] = set()


def record_ai_usage(usage: AIUsage) -> None:
    """Persist one usage record. Synchronous and non-blocking by contract."""
    payload = {
        "user_id": str(usage.user_id),
        "feature": str(usage.feature),
        "provider": str(usage.provider),
        "model": usage.model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "latency_ms": usage.latency_ms,
        "cost_usd": str(usage.cost_usd),
        "status": usage.status,
        "error": usage.error,
    }
    _BUFFER.append(payload)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called outside an event loop (e.g. a sync script). Log and move on.
        logger.info("ai.usage_recorded_no_loop", **payload)
        return

    _track(loop.create_task(_persist(payload)))


def _track(task: asyncio.Task[None]) -> None:
    """Hold a reference until the write finishes so it cannot be collected early."""
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def drain(timeout_seconds: float = 5.0) -> None:
    """Wait for outstanding usage writes. Called from the application lifespan."""
    if not _PENDING:
        return
    pending = list(_PENDING)
    done, still_running = await asyncio.wait(pending, timeout=timeout_seconds)
    if still_running:
        logger.warning("ai.usage_drain_timeout", dropped=len(still_running))
    else:
        logger.info("ai.usage_drained", written=len(done))


async def _persist(payload: dict[str, Any]) -> None:
    try:
        from app.core.database import session_scope
        from app.modules.ai.infrastructure.models import AiUsageRecord

        async with session_scope() as session:
            session.add(
                AiUsageRecord(
                    user_id=payload["user_id"],
                    feature=payload["feature"],
                    provider=payload["provider"],
                    model=payload["model"],
                    input_tokens=payload["input_tokens"],
                    output_tokens=payload["output_tokens"],
                    latency_ms=payload["latency_ms"],
                    cost_usd=Decimal(payload["cost_usd"]),
                    status=payload["status"],
                    error=payload["error"],
                )
            )
    except Exception as exc:  # noqa: BLE001 - metering must never break a request
        logger.warning("ai.usage_persist_failed", error=str(exc)[:200], **payload)


def buffered_usage() -> list[dict[str, Any]]:
    """Test/ops helper: recent usage records held in memory."""
    return list(_BUFFER)


def clear_buffer() -> None:
    _BUFFER.clear()


__all__ = [
    "buffered_usage",
    "clear_buffer",
    "drain",
    "record_ai_usage",
]
