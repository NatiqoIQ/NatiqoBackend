"""In-process queue adapter for development and tests.

Jobs are drained by the in-process worker started alongside the API in
development, or executed inline when a test opts in. Never used in production.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.core.logging import get_logger
from app.platform.queue.port import Job

logger = get_logger("queue.memory")


class InMemoryQueue:
    """FIFO queue with a dedupe set and a background-drain helper."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._dedupe: set[str] = set()
        self._inflight: set[str] = set()
        #: Bounded for the same reason the Redis list is trimmed: a development process
        #: must not grow without limit because a handler is broken.
        self.dead_letters: list[dict[str, Any]] = []

    async def enqueue(self, job: Job) -> bool:
        key = job.dedupe_key
        if key:
            if key in self._dedupe or key in self._inflight:
                logger.debug("queue.job_deduplicated", job=str(job.name), key=key)
                return False
            self._dedupe.add(key)
        await self._queue.put(job)
        return True

    async def dequeue(self, timeout_ms: int = 1000) -> Job | None:
        """Take the next job, or ``None`` if none arrives within ``timeout_ms``.

        ``timeout_ms <= 0`` means "do not wait". It has to be handled separately rather than
        passed to ``wait_for``: a zero timeout cancels the pending ``get`` unconditionally,
        even when the queue is not empty, so ``drain()`` would report zero jobs processed
        while the queue stayed full.
        """
        try:
            if timeout_ms <= 0:
                job = self._queue.get_nowait()
            else:
                job = await asyncio.wait_for(self._queue.get(), timeout=timeout_ms / 1000)
        except (TimeoutError, asyncio.QueueEmpty):
            return None
        if job.dedupe_key:
            self._dedupe.discard(job.dedupe_key)
        self._queue.task_done()
        return job

    async def ack(self, job: Job) -> None:
        """In-process queue: a job cannot outlive the process holding it."""
        self._inflight.discard(str(job.job_id))

    async def recover(self) -> int:
        """Nothing can be recovered: the queue dies with the process."""
        return 0

    async def dead_letter(self, job: Job, *, error: str) -> None:
        self.dead_letters.append(
            {"job_id": str(job.job_id), "name": str(job.name), "error": error[:500]}
        )
        del self.dead_letters[:-1000]

    async def size(self) -> int:
        return self._queue.qsize()

    async def ping(self) -> bool:
        return True

    async def drain(self, handler: Any, *, limit: int = 1000) -> int:
        """Process queued jobs with ``handler`` until the queue empties."""
        processed = 0
        while processed < limit:
            job = await self.dequeue(timeout_ms=0)
            if job is None:
                break
            with contextlib.suppress(Exception):
                await handler(job)
            processed += 1
        return processed

    async def close(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
