"""Redis-backed queue adapter (production).

Delivery is **at-least-once**: a job is moved atomically from the ready list to a
processing list, and only leaves that list when the worker acknowledges it. A worker
that dies mid-job leaves the payload behind with a deadline; once the deadline passes,
:meth:`recover` puts it back on the ready list and another (or the same) worker runs it
again. That is the trade-off versus the previous ``BLPOP``: a crash used to delete the
job outright, while now it may execute twice. For this application's jobs — token
cleanup, notifications, analytics — a duplicate is recoverable and a loss is not.

The queue still does not track attempts or backoff; that is the worker's contract, and
the same ``max_attempts`` ceiling applies whether a job was retried in-process or after
a crash. An exhausted job is pushed to a bounded dead-letter list with its last error
before it is acknowledged, so it cannot poison the queue and an operator can still see
what failed. The list is a Redis list, not a Stream: it is an inspection aid, not a
replay protocol.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from app.core.logging import get_logger
from app.platform.queue.port import Job

logger = get_logger("queue.redis")

_QUEUE_KEY = "natiqo:jobs"
_DEDUPE_KEY = "natiqo:jobs:dedupe"
_DEDUPE_TTL_SECONDS = 3600

#: Bounded dead-letter list. Ten thousand entries is days of failures at a healthy
#: rate and small enough that inspecting the whole list is practical.
DEAD_LETTER_LIMIT = 10_000

#: How long a worker may hold a job before it is considered abandoned. Five minutes is
#: comfortably longer than any current handler and short enough that a crash is noticed
#: within one deploy window. Raise it if a handler legitimately runs longer.
DEFAULT_VISIBILITY_SECONDS = 300


class RedisQueue:
    def __init__(
        self,
        redis: Any,
        *,
        queue_key: str = _QUEUE_KEY,
        visibility_seconds: int = DEFAULT_VISIBILITY_SECONDS,
    ) -> None:
        self._redis = redis
        self._queue_key = queue_key
        self._processing_key = f"{queue_key}:processing"
        self._deadlines_key = f"{queue_key}:deadlines"
        self._dead_letter_key = f"{queue_key}:dead"
        self._dedupe_key = _DEDUPE_KEY
        self._visibility_seconds = max(1, visibility_seconds)
        #: job id -> exact serialized payload. Holding the payload rather than
        #: re-serializing on ack means ack removes *the bytes that were queued*, even if
        #: the Job object has since gained or lost an optional field.
        self._claims: dict[str, str] = {}

    @classmethod
    def from_url(
        cls, url: str, *, visibility_seconds: int = DEFAULT_VISIBILITY_SECONDS
    ) -> RedisQueue:
        from redis.asyncio import Redis

        client = Redis.from_url(url, encoding="utf-8", decode_responses=True)
        logger.info("queue.redis_initialised")
        return cls(client, visibility_seconds=visibility_seconds)

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
        """Take the next job, moving it to the processing list in one atomic step."""
        # Redis' blocking commands take whole seconds. Rounding up keeps a caller's
        # 100 ms intent from becoming a busy loop of immediate returns.
        timeout = max(1, math.ceil(timeout_ms / 1000))
        payload = await self._redis.blmove(
            self._queue_key, self._processing_key, timeout, "LEFT", "RIGHT"
        )
        if payload is None:
            return None
        try:
            job = Job.from_dict(json.loads(payload))
        except (ValueError, KeyError) as exc:
            logger.error("queue.job_malformed", error=str(exc))
            # Do not leave a poison payload in the processing list forever: it cannot
            # be retried into a valid job, and recovery would requeue it on every pass.
            await self._redis.lrem(self._processing_key, 1, payload)
            return None

        self._claims[str(job.job_id)] = payload
        await self._redis.zadd(
            self._deadlines_key, {payload: time.time() + self._visibility_seconds}
        )
        return job

    async def ack(self, job: Job) -> None:
        payload = self._claims.pop(str(job.job_id), None) or json.dumps(job.to_dict())
        await self._redis.lrem(self._processing_key, 1, payload)
        await self._redis.zrem(self._deadlines_key, payload)

    async def recover(self) -> int:
        """Move expired claims back to the ready list, in bounded batches.

        Claims are removed from the deadline set whether or not the corresponding
        processing entry still exists: an entry that disappeared was acknowledged
        between the read and the removal, so requeueing it would duplicate completed
        work for no reason.
        """
        now = time.time()
        expired = await self._redis.zrangebyscore(
            self._deadlines_key, "-inf", now, start=0, num=100
        )
        moved = 0
        for payload in expired:
            if await self._redis.lrem(self._processing_key, 1, payload):
                await self._redis.rpush(self._queue_key, payload)
                moved += 1
            await self._redis.zrem(self._deadlines_key, payload)
        if moved:
            logger.warning("queue.jobs_recovered", count=moved)
        return moved

    async def dead_letter(self, job: Job, *, error: str) -> None:
        record = {
            "job": job.to_dict(),
            "error": error[:500],
            "failed_at": time.time(),
        }
        pipe = self._redis.pipeline()
        pipe.lpush(self._dead_letter_key, json.dumps(record))
        pipe.ltrim(self._dead_letter_key, 0, DEAD_LETTER_LIMIT - 1)
        await pipe.execute()

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
