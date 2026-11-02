"""Small cache for the review stats screen (F10 confirmation pattern).

The review screen reads the same figures over and over during a session — lifetime
reviews and retention do not change on a read. ``stats()`` runs one card scan plus one
lifetime aggregate per call, so a shared 30-second cache collapses those repeats the
same way the progress summary already does.

Caching is fail-open at both ends, exactly like the progress, flags and catalogue
caches: a cache outage costs two extra queries; it never fails the review screen.
Invalidation is deliberate: only a committed *answer* changes the numbers, so only the
answer path drops the key — and only after the commit, because deleting it earlier
would let a concurrent read repopulate the stale value for a whole TTL.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from app.core.logging import get_logger
from app.core.types import UserId
from app.modules.quiz.application.review_dto import ReviewStatsView
from app.platform.cache.port import CachePort

logger = get_logger("quiz.review_cache")

__all__ = [
    "decode_stats",
    "encode_stats",
    "invalidate_review_stats",
    "review_stats_key",
]


def review_stats_key(cache: CachePort, user_id: UserId) -> str:
    return cache.build_key("review", "stats", str(user_id))


def encode_stats(view: ReviewStatsView) -> str:
    payload: dict[str, Any] = asdict(view)
    return json.dumps(payload, separators=(",", ":"))


def decode_stats(raw: str) -> ReviewStatsView:
    payload: dict[str, Any] = json.loads(raw)
    return ReviewStatsView(
        total_cards=int(payload["total_cards"]),
        due_today=int(payload["due_today"]),
        future=int(payload["future"]),
        reviewed_total=int(payload["reviewed_total"]),
        retention_percent=int(payload["retention_percent"]),
    )


async def invalidate_review_stats(cache: CachePort, user_id: UserId) -> None:
    """Drop the cached stats after an answer commits."""
    try:
        await cache.delete(review_stats_key(cache, user_id))
    except Exception as exc:  # noqa: BLE001 - a cache outage must not fail the command
        logger.warning("quiz.review_cache_invalidate_failed", error=str(exc)[:200])
