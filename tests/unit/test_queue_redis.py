"""At-least-once delivery, against a fake that models the Redis commands we use.

The property that matters is not "Redis works" — it does — but the adapter's own
protocol: a job leaves the ready list into a processing list atomically, survives a
crashed worker as an expired claim, and is returned to the ready list exactly once.
The fake implements only those commands, so a test failure points at the adapter rather
than at a container.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import pytest

from app.platform.queue.port import Job, JobName
from app.platform.queue.redis import RedisQueue


class FakeRedis:
    """The subset of ``redis.asyncio.Redis`` the queue uses, with real list semantics."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.zsets: dict[str, dict[str, float]] = defaultdict(dict)
        self.kv: dict[str, str] = {}

    async def rpush(self, key: str, value: str) -> int:
        self.lists[key].append(value)
        return len(self.lists[key])

    async def llen(self, key: str) -> int:
        return len(self.lists[key])

    async def lrem(self, key: str, count: int, value: str) -> int:
        removed = 0
        while value in self.lists[key] and (count == 0 or removed < count):
            self.lists[key].remove(value)
            removed += 1
        return removed

    async def blmove(
        self,
        source: str,
        destination: str,
        timeout: int,  # noqa: ASYNC109 - mirrors the Redis client signature
        src: str,
        dest: str,
    ) -> str | None:
        if not self.lists[source]:
            return None
        value = self.lists[source].pop(0)
        if dest == "RIGHT":
            self.lists[destination].append(value)
        else:
            self.lists[destination].insert(0, value)
        return value

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.zsets[key].update(mapping)
        return len(mapping)

    async def zrem(self, key: str, member: str) -> int:
        return 1 if self.zsets[key].pop(member, None) is not None else 0

    async def zrangebyscore(
        self, key: str, minimum: Any, maximum: Any, start: int = 0, num: int | None = None
    ) -> list[str]:
        upper = float(maximum)
        rows = sorted(
            ((member, score) for member, score in self.zsets[key].items() if score <= upper),
            key=lambda row: row[1],
        )
        members = [member for member, _ in rows][start:]
        return members[:num] if num is not None else members

    async def set(
        self, key: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _job() -> Job:
    return Job(name=JobName.CLEANUP_EXPIRED_TOKENS, payload={"reason": "test"})


class TestDeliveryProtocol:
    @pytest.mark.asyncio
    async def test_dequeue_moves_the_job_out_of_the_ready_list(self) -> None:
        redis = FakeRedis()
        queue = RedisQueue(redis)
        await queue.enqueue(_job())

        job = await queue.dequeue(timeout_ms=10)

        assert job is not None
        assert redis.lists["natiqo:jobs"] == []
        assert len(redis.lists["natiqo:jobs:processing"]) == 1

    @pytest.mark.asyncio
    async def test_ack_clears_the_processing_entry_and_its_deadline(self) -> None:
        redis = FakeRedis()
        queue = RedisQueue(redis)
        await queue.enqueue(_job())
        job = await queue.dequeue(timeout_ms=10)
        assert job is not None

        await queue.ack(job)

        assert redis.lists["natiqo:jobs:processing"] == []
        assert redis.zsets["natiqo:jobs:deadlines"] == {}
        assert await queue.size() == 0

    @pytest.mark.asyncio
    async def test_an_unacknowledged_job_is_requeued_after_its_claim_expires(self) -> None:
        """This is the crash case: the worker died after taking the job."""
        redis = FakeRedis()
        queue = RedisQueue(redis, visibility_seconds=1)
        await queue.enqueue(_job())
        first = await queue.dequeue(timeout_ms=10)
        assert first is not None

        # Nothing acknowledges it. Make the deadline look old, then sweep.
        for payload in redis.zsets["natiqo:jobs:deadlines"]:
            redis.zsets["natiqo:jobs:deadlines"][payload] = 0
        moved = await queue.recover()

        assert moved == 1
        assert len(redis.lists["natiqo:jobs"]) == 1

        second = await queue.dequeue(timeout_ms=10)
        assert second is not None
        assert second.job_id == first.job_id

    @pytest.mark.asyncio
    async def test_recover_does_not_duplicate_a_job_that_was_acked_in_the_meantime(self) -> None:
        redis = FakeRedis()
        queue = RedisQueue(redis, visibility_seconds=1)
        await queue.enqueue(_job())
        job = await queue.dequeue(timeout_ms=10)
        assert job is not None
        await queue.ack(job)

        # The deadline was removed by ack, so the sweep must find nothing.
        assert await queue.recover() == 0
        assert redis.lists["natiqo:jobs"] == []

    @pytest.mark.asyncio
    async def test_a_malformed_payload_is_dropped_instead_of_poisoning_the_queue(self) -> None:
        redis = FakeRedis()
        queue = RedisQueue(redis)
        await redis.rpush("natiqo:jobs", json.dumps({"nope": True}))

        assert await queue.dequeue(timeout_ms=10) is None
        assert redis.lists["natiqo:jobs:processing"] == []

    @pytest.mark.asyncio
    async def test_dedupe_still_suppresses_a_second_enqueue(self) -> None:
        redis = FakeRedis()
        queue = RedisQueue(redis)
        first = Job(name=JobName.CLEANUP_EXPIRED_TOKENS, dedupe_key="same")
        second = Job(name=JobName.CLEANUP_EXPIRED_TOKENS, dedupe_key="same")

        assert await queue.enqueue(first) is True
        assert await queue.enqueue(second) is False
        assert await queue.size() == 1
