"""AI usage metering.

The AI service is synchronous-ish and must never block a request on bookkeeping, so
this recorder:

- writes through its own short-lived session, never the request's unit of work;
- swallows its own failures (a dropped usage row must not fail a lesson);
- keeps a bounded in-process buffer so metering still works if the database is down.

In-flight writes are tracked so the application lifespan can drain them on shutdown.
Without that, the last few usage rows of every deploy would be lost.

Writes are batched. One session and one transaction per AI call is a connection checkout
that exists only to insert a row; at a hundred thousand learners that is the busiest
consumer of the pool. Records now accumulate for up to a second (or 100 rows) and are
written together. The cost is that a crash can lose the last second of metering — an
acceptable trade for cost attribution, which is not allowed to compete with the learning
path for database connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import deque
from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.platform.ai.port import AIUsage

logger = get_logger("ai.usage")

#: Recent usage kept in memory. Only used for emergency logging when the DB is down.
_BUFFER: deque[dict[str, Any]] = deque(maxlen=200)

#: Rows waiting to be written, and the task that will write them. A single flush task
#: collects every record produced during its window, so a burst of AI calls costs one
#: transaction rather than one per call.
_QUEUE: deque[dict[str, Any]] = deque()
_FLUSH_INTERVAL_SECONDS = 1.0
_MAX_BATCH = 100
_FLUSH_TASK: asyncio.Task[None] | None = None

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

    _QUEUE.append(payload)
    _schedule_flush(loop)


def _schedule_flush(loop: asyncio.AbstractEventLoop) -> None:
    """Start the collector if one is not already running."""
    global _FLUSH_TASK
    if _FLUSH_TASK is not None and not _FLUSH_TASK.done():
        return
    _FLUSH_TASK = loop.create_task(_flush_loop())
    _track(_FLUSH_TASK)


async def _flush_loop() -> None:
    """Coalesce a window of records, then write them in one transaction."""
    try:
        await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        # Shutdown wants the rows now; drain() calls flush() directly.
        pass
    await flush()


async def flush() -> int:
    """Write every queued record, in batches. Returns the number written."""
    total = 0
    while _QUEUE:
        batch = [_QUEUE.popleft() for _ in range(min(_MAX_BATCH, len(_QUEUE)))]
        try:
            await _persist_batch(batch)
        except Exception as exc:  # noqa: BLE001 - metering must never break a request
            logger.warning("ai.usage_batch_failed", error=str(exc)[:200], rows=len(batch))
            break
        total += len(batch)
    return total


def _track(task: asyncio.Task[None]) -> None:
    """Hold a reference until the write finishes so it cannot be collected early."""
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def drain(timeout_seconds: float = 5.0) -> None:
    """Flush and wait for outstanding usage writes. Called from the application lifespan.

    The scheduled collector is cancelled first so the rows it is holding do not wait out
    the one-second window during shutdown; ``flush`` then writes them synchronously.
    """
    global _FLUSH_TASK
    task = _FLUSH_TASK
    _FLUSH_TASK = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    written = await flush()
    if written:
        logger.info("ai.usage_flushed_on_shutdown", written=written)
    if not _PENDING:
        return
    pending = list(_PENDING)
    done, still_running = await asyncio.wait(pending, timeout=timeout_seconds)
    if still_running:
        logger.warning("ai.usage_drain_timeout", dropped=len(still_running))
    else:
        logger.info("ai.usage_drained", written=len(done))


async def _persist_batch(batch: list[dict[str, Any]]) -> None:
    from app.core.database import session_scope
    from app.modules.ai.infrastructure.models import AiUsageRecord

    async with session_scope() as session:
        session.add_all(
            [
                AiUsageRecord(
                    user_id=uuid.UUID(payload["user_id"]),
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
                for payload in batch
            ]
        )


def buffered_usage() -> list[dict[str, Any]]:
    """Test/ops helper: recent usage records held in memory."""
    return list(_BUFFER)


def clear_buffer() -> None:
    """Drop buffered and queued records. Tests use this; production never calls it."""
    global _FLUSH_TASK
    _BUFFER.clear()
    _QUEUE.clear()
    task = _FLUSH_TASK
    _FLUSH_TASK = None
    if task is not None and not task.done():
        task.cancel()


__all__ = [
    "buffered_usage",
    "clear_buffer",
    "drain",
    "record_ai_usage",
]
