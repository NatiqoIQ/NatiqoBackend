"""Small cache for the progress summary (F10/P2-2 cache step).

The summary is five aggregates, two of which scan a learner's whole answer and session
history. The screen is opened repeatedly during a session, and every open recomputed the
same numbers. A shared 60-second cache collapses those repeats without making a *mutation*
wait: every command that can change the summary invalidates the key after its commit.

Caching is fail-open at both ends. A cache outage costs one extra query; it never fails the
progress screen. That is the same rule the catalogue and admin caches follow.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.core.types import UserId
from app.modules.quiz.application.progress_dto import ProgressSummaryView
from app.platform.cache.port import CachePort

logger = get_logger("quiz.progress_cache")

__all__ = [
    "decode_summary",
    "encode_summary",
    "invalidate_progress_summary",
    "progress_summary_key",
]


def progress_summary_key(cache: CachePort, user_id: UserId) -> str:
    return cache.build_key("progress", "summary", str(user_id))


def encode_summary(view: ProgressSummaryView) -> str:
    payload = asdict(view)
    if payload["last_activity_at"] is not None:
        payload["last_activity_at"] = payload["last_activity_at"].isoformat()
    return json.dumps(payload, separators=(",", ":"))


def decode_summary(raw: str) -> ProgressSummaryView:
    payload: dict[str, Any] = json.loads(raw)
    last = payload.get("last_activity_at")
    if isinstance(last, str):
        payload["last_activity_at"] = datetime.fromisoformat(last)
    return ProgressSummaryView(**payload)


async def invalidate_progress_summary(cache: CachePort, user_id: UserId) -> None:
    """Drop the cached summary after a command commits.

    Called after the commit on purpose: deleting before it would let a concurrent read
    repopulate the key from the pre-commit state, and the stale value would then live for
    the full TTL.
    """
    try:
        await cache.delete(progress_summary_key(cache, user_id))
    except Exception as exc:  # noqa: BLE001 - a cache outage must not fail the command
        logger.warning("quiz.progress_cache_invalidate_failed", error=str(exc)[:200])
