"""Job queue port.

Long-running work (notification delivery, challenge generation, analytics rollups,
token cleanup) is enqueued and executed by a separate worker process, so an API
request is never blocked by it (plan §18).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class JobName(StrEnum):
    """Canonical job names. Kept as an enum so typos fail at import time."""

    SEND_NOTIFICATION = "send_notification"
    GENERATE_DAILY_CHALLENGE = "generate_daily_challenge"
    REFRESH_RECOMMENDATIONS = "refresh_recommendations"
    PROCESS_ANALYTICS = "process_analytics"
    CLEANUP_EXPIRED_TOKENS = "cleanup_expired_tokens"
    PURGE_AI_USAGE = "purge_ai_usage"
    PROCESS_AUDIO = "process_audio"
    AI_TASK = "ai_task"
    EVALUATE_ACHIEVEMENTS = "evaluate_achievements"


@dataclass(frozen=True, slots=True)
class Job:
    name: JobName
    payload: dict[str, Any] = field(default_factory=dict)
    #: Stable key used for idempotency. Re-enqueuing the same key is a no-op.
    dedupe_key: str | None = None
    max_attempts: int = 5
    #: Seconds to wait before the first attempt; supports "run at midnight" style jobs.
    run_at: object | None = None
    job_id: uuid.UUID = field(default_factory=uuid.uuid4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": str(self.job_id),
            "name": str(self.name),
            "payload": self.payload,
            "dedupe_key": self.dedupe_key,
            "max_attempts": self.max_attempts,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Job:
        return cls(
            name=JobName(raw["name"]),
            payload=raw.get("payload") or {},
            dedupe_key=raw.get("dedupe_key"),
            max_attempts=int(raw.get("max_attempts", 5)),
            job_id=uuid.UUID(raw["job_id"]) if raw.get("job_id") else uuid.uuid4(),
        )


@runtime_checkable
class QueuePort(Protocol):
    """Publish/subscribe work queue."""

    async def enqueue(self, job: Job) -> bool:
        """Return ``False`` when the job was dropped as a duplicate."""
        ...

    async def dequeue(self, timeout_ms: int = 1000) -> Job | None: ...

    async def ack(self, job: Job) -> None:
        """Confirm that ``job`` finished and may leave the in-flight set.

        Without this the queue cannot distinguish "still running" from "the worker died
        holding it", which is the difference between at-most-once and at-least-once.
        """
        ...

    async def recover(self) -> int:
        """Requeue jobs whose claim outlived its visibility timeout.

        Returns the number of jobs moved back. Called periodically by the worker, not by
        the API: recovery is the consumer's responsibility.
        """
        ...

    async def dead_letter(self, job: Job, *, error: str) -> None:
        """Keep a job whose attempts are exhausted, with the last error.

        The worker acknowledges it after this call, so it can no longer poison the
        queue. The record is for an operator: without it the only trace of a job that
        failed every attempt is a log line that may have rotated away.
        """
        ...

    async def size(self) -> int: ...

    async def ping(self) -> bool: ...


JobHandler = Callable[[Job], Awaitable[None]]
