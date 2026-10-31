"""The worker loop, against a queue that is only a list.

What these tests pin is the *contract* the process relies on, not Redis: one job at a
time, a transient failure retried, a permanent one given up on with a log line rather
than an exception that kills the loop, and a scheduler whose dedupe key makes "daily"
independent of how often the process restarts.

The retry tests pass ``max_backoff_seconds=0`` on purpose: the schedule is the code's
business and the delay is the machine's, so a test that slept would be measuring the
second one and would fail the moment the schedule changed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from app.platform.queue.memory import InMemoryQueue
from app.platform.queue.port import Job, JobName
from app.workers.main import Worker, schedule_daily_cleanup

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 19, 3, 0, 0)


class _CountingHandler:
    """A handler that fails a fixed number of times before it succeeds."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self._fail_times = fail_times

    async def __call__(self, job: Job) -> None:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError(f"attempt {self.calls} failed")


def _worker(handler: Any, **kwargs: Any) -> Worker:
    return Worker(
        InMemoryQueue(),
        {JobName.CLEANUP_EXPIRED_TOKENS: handler},
        max_backoff_seconds=0,
        **kwargs,
    )


# --------------------------------------------------------------------- dispatch
@pytest.mark.asyncio
async def test_a_known_job_reaches_its_handler() -> None:
    handler = _CountingHandler()
    job = Job(name=JobName.CLEANUP_EXPIRED_TOKENS)

    assert await _worker(handler).handle(job) is True
    assert handler.calls == 1


@pytest.mark.asyncio
async def test_a_job_with_no_handler_is_reported_and_not_retried() -> None:
    """A name nobody registered will not become known by trying it again."""
    handler = _CountingHandler()
    job = Job(name=JobName.PROCESS_ANALYTICS)

    assert await _worker(handler).handle(job) is False
    assert handler.calls == 0


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_until_it_succeeds() -> None:
    handler = _CountingHandler(fail_times=2)

    assert await _worker(handler).handle(Job(name=JobName.CLEANUP_EXPIRED_TOKENS)) is True
    assert handler.calls == 3


@pytest.mark.asyncio
async def test_a_job_that_keeps_failing_gives_up_and_is_dead_lettered() -> None:
    """``max_attempts`` is the ceiling; the failed job is kept, not silently dropped.

    Dropping keeps one poison job from spending the process's whole day in a retry
    loop. Keeping a record is what lets an operator see *what* failed after the log has
    rotated. The worker acknowledges the job afterwards, so it cannot cycle.
    """
    queue = InMemoryQueue()
    worker = Worker(queue, {JobName.CLEANUP_EXPIRED_TOKENS: _CountingHandler(fail_times=99)})
    job = Job(name=JobName.CLEANUP_EXPIRED_TOKENS, max_attempts=3)

    assert await worker.handle(job) is False

    assert len(queue.dead_letters) == 1
    assert queue.dead_letters[0]["job_id"] == str(job.job_id)
    assert "attempt 3 failed" in queue.dead_letters[0]["error"]


# ------------------------------------------------------------------ consumer loop
@pytest.mark.asyncio
async def test_the_consumer_takes_one_job_and_then_reports_empty() -> None:
    handler = _CountingHandler()
    queue = InMemoryQueue()
    worker = Worker(queue, {JobName.CLEANUP_EXPIRED_TOKENS: handler})
    await queue.enqueue(Job(name=JobName.CLEANUP_EXPIRED_TOKENS))

    first = await worker.run_once(timeout_ms=0)
    second = await worker.run_once(timeout_ms=0)

    assert isinstance(first, Job)
    assert second is None
    assert handler.calls == 1


@pytest.mark.asyncio
async def test_the_loop_stops_when_asked() -> None:
    """``stop`` interrupts the dequeue wait rather than being read once per timeout."""
    stop = asyncio.Event()
    worker = _worker(_CountingHandler(), poll_timeout_ms=5)

    task = asyncio.create_task(worker.run_forever(stop))
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


# --------------------------------------------------------------------- scheduler
class _StoppingQueue(InMemoryQueue):
    """A real queue that records its jobs and stops the scheduler on the first one."""

    def __init__(self, stop: asyncio.Event) -> None:
        super().__init__()
        self._stop = stop
        self.jobs: list[Job] = []

    async def enqueue(self, job: Job) -> bool:
        self.jobs.append(job)
        accepted = await super().enqueue(job)
        self._stop.set()
        return accepted


@pytest.mark.asyncio
async def test_the_daily_cleanup_carries_the_date_as_its_dedupe_key() -> None:
    """The key is what makes one job per day true across restarts.

    A worker restarted at noon must not enqueue a second purge for a day that already
    had one, and the only thing that can tell the queue it is the same day is the key.
    """
    stop = asyncio.Event()
    queue = _StoppingQueue(stop)

    await asyncio.wait_for(schedule_daily_cleanup(queue, stop=stop, clock=lambda: _NOW), timeout=2)

    assert len(queue.jobs) == 1
    assert queue.jobs[0].name is JobName.CLEANUP_EXPIRED_TOKENS
    assert queue.jobs[0].dedupe_key == "cleanup_expired_tokens:2026-10-19"


@pytest.mark.asyncio
async def test_two_restarts_in_one_day_enqueue_one_job() -> None:
    """The producer's own dedupe, through the real adapter rather than a stub."""
    stop = asyncio.Event()
    queue = _StoppingQueue(stop)

    await asyncio.wait_for(schedule_daily_cleanup(queue, stop=stop, clock=lambda: _NOW), timeout=2)
    # A second boot the same day presents the same key, which the queue drops.
    second = await queue.enqueue(queue.jobs[0])

    assert second is False
    assert await queue.size() == 1


# ------------------------------------------------------------------------ acks
class _AckingQueue(InMemoryQueue):
    """Records every acknowledgement, so dropping a job is visible."""

    def __init__(self) -> None:
        super().__init__()
        self.acked: list[Job] = []

    async def ack(self, job: Job) -> None:
        self.acked.append(job)
        await super().ack(job)


@pytest.mark.asyncio
async def test_a_finished_job_is_acknowledged() -> None:
    """Without an ack the Redis adapter would requeue it after the visibility timeout."""
    queue = _AckingQueue()
    worker = Worker(queue, {JobName.CLEANUP_EXPIRED_TOKENS: _CountingHandler()})
    await queue.enqueue(Job(name=JobName.CLEANUP_EXPIRED_TOKENS))

    job = await worker.run_once(timeout_ms=0)

    assert job is not None
    assert queue.acked == [job]


@pytest.mark.asyncio
async def test_a_poison_job_is_acknowledged_too() -> None:
    """A permanently failing job must leave the in-flight set, or recovery loops forever."""
    queue = _AckingQueue()
    handler = _CountingHandler(fail_times=99)
    worker = Worker(queue, {JobName.CLEANUP_EXPIRED_TOKENS: handler})
    await queue.enqueue(Job(name=JobName.CLEANUP_EXPIRED_TOKENS, max_attempts=2))

    job = await worker.run_once(timeout_ms=0)

    assert job is not None
    assert len(queue.acked) == 1
    assert handler.calls == 2
