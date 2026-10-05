"""Redis-backed queue adapter (production).

Jobs live in a Redis list; dedupe uses a short-lived marker key. Attempts, backoff
and the dead-letter state are tracked by the worker, not here — the queue's only
responsibility is durable delivery plus duplicate suppression.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.logging import get_logger
from app.platform.queue.port import Job

logger = get_logger("queue.redis")

_QUEUE_KEY = "natiqo:jobs"
_DEDUPE_KEY = "natiqo:jobs:dedupe"
_DEDUPE_TTL_SECONDS = 3600


class RedisQueue:
    def __init__(self, redis: Any, *, queue_key: str = _QUEUE_KEY) -> None:
        self._redis = redis
        self._queue_key = queue_key
        self._dedupe_key = _DEDUPE_KEY

    @classmethod
    def from_url(cls, url: str) -> RedisQueue:
        from redis.asyncio import Redis

        client = Redis.from_url(url, encoding="utf-8", decode_responses=True)
        logger.info("queue.redis_initialised")
        return cls(client)

    async def enqueue(self, job: Job) -> bool:
        if job.dedupe_key:
            # SET NX gives us an atomic "claim this dedupe key" primitive.
            claimed = await self._redis.set(
                f"{self._dedupe_key}:{job.dedupe_key}",
                "1",
                ex=_DEDUPE_TTL_SECONDS,
                nx=True,
            )
            if not claimed:
                logger.debug("queue.job_deduplicated", job=str(job.name), key=job.dedupe_key)
                return False
        await self._redis.rpush(self._queue_key, json.dumps(job.to_dict()))
        return True

    async def dequeue(self, timeout_ms: int = 1000) -> Job | None:
        popped = await self._redis.blpop([self._queue_key], timeout=max(1, timeout_ms / 1000))
        if not popped:
            return None
        _key, payload = popped
        try:
            return Job.from_dict(json.loads(payload))
        except (ValueError, KeyError) as exc:
            logger.error("queue.job_malformed", error=str(exc))
            return None

    async def size(self) -> int:
        return int(await self._redis.llen(self._queue_key))

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception as exc:  # noqa: BLE001
            logger.warning("queue.ping_failed", error=str(exc))
            return False

    async def close(self) -> None:
        await self._redis.aclose()
