"""The worker process: dequeue jobs and run them, forever, until asked to stop.

Run it with ``python -m app.workers.main`` (the ``worker`` service in
``docker-compose.yml`` does exactly this). It shares every adapter with the API —
the same ``REDIS_URL``, the same database — and differs only in what it does after
boot: instead of listening on a socket, it listens on the queue.

Two loops run side by side:

* the **consumer**, which takes one job at a time and runs its handler;
* the **scheduler**, which is a producer: it enqueues the daily token purge. It exists
  so the queue has a real producer as well as a consumer — a worker with nothing to
  dequeue proves nothing, and neither does a producer with nothing listening.

Both stop on the same signal, so ``docker compose stop worker`` finishes the job in
flight rather than abandoning it half-applied.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping
from datetime import datetime

from app.core.database import dispose_engine, make_uow
from app.core.logging import get_logger
from app.core.time import utc_now
from app.platform.queue.port import Job, JobHandler, JobName, QueuePort
from app.workers.handlers import WorkerContext, build_handlers

logger = get_logger("workers.main")

#: How long a single ``dequeue`` waits before the loop checks the stop flag again. Short
#: enough that shutdown is prompt, long enough that an idle worker is not a busy loop.
POLL_TIMEOUT_MS = 1000

#: How often an idle worker sweeps abandoned claims. Frequent enough that a crashed
#: job is retried within minutes, infrequent enough that the sweep is not itself load.
RECOVERY_INTERVAL_SECONDS = 30

#: First retry waits a second, then two, four… capped here. Capped rather than unbounded
#: because ``max_attempts`` is 5 by default, and 16 seconds of total backoff is a better
#: use of a worker process than fifteen minutes of sleeping on one bad job.
MAX_BACKOFF_SECONDS = 30


class Worker:
    """One job at a time, with the port's retry contract honoured."""

    def __init__(
        self,
        queue: QueuePort,
        handlers: Mapping[JobName, JobHandler],
        *,
        poll_timeout_ms: int = POLL_TIMEOUT_MS,
        max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        self._queue = queue
        self._handlers = handlers
        self._poll_timeout_ms = poll_timeout_ms
        self._max_backoff_seconds = max_backoff_seconds

    async def handle(self, job: Job) -> bool:
        """Run the job's handler, retrying a transient failure up to ``max_attempts``.

        Returns ``True`` only when a handler completed. A missing handler is not
        retried: the same process will not learn it any sooner, and a retry loop around
        a name nobody registered would spend every attempt on the same log line.

        A job whose attempts are exhausted is logged as ``worker.job_dead`` and dropped.
        That is a deliberate, limited choice rather than a complete one: there is no
        durable dead-letter store yet, so the log *is* the record. It is the seam where
        one goes when a second job needs it.
        """
        handler = self._handlers.get(job.name)
        if handler is None:
            logger.error("worker.no_handler", job=str(job.name), job_id=str(job.job_id))
            return False

        attempts = max(1, job.max_attempts)
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                await handler(job)
            except Exception as exc:  # noqa: BLE001 — one bad job must not stop the loop
                last_error = str(exc)
                if attempt >= attempts:
                    logger.error(
                        "worker.job_dead",
                        job=str(job.name),
                        job_id=str(job.job_id),
                        attempt=attempt,
                        error=last_error[:300],
                    )
                    # Before the ack in ``run_once``: a failure to record must not
                    # keep a poison job cycling, so this is best-effort.
                    try:
                        await self._queue.dead_letter(job, error=last_error)
                    except Exception as record_error:  # noqa: BLE001
                        logger.error(
                            "worker.dead_letter_failed",
                            job=str(job.name),
                            job_id=str(job.job_id),
                            error=str(record_error)[:200],
                        )
                    return False
                delay = min(2 ** (attempt - 1), self._max_backoff_seconds)
                logger.warning(
                    "worker.job_retry",
                    job=str(job.name),
                    job_id=str(job.job_id),
                    attempt=attempt,
                    delay_seconds=delay,
                    error=str(exc)[:300],
                )
                await asyncio.sleep(delay)
            else:
                logger.debug("worker.job_done", job=str(job.name), job_id=str(job.job_id))
                return True
        return False

    async def run_once(self, *, timeout_ms: int | None = None) -> Job | None:
        """Drain at most one job. ``None`` means the queue was empty.

        The job is acknowledged whether the handler succeeded or exhausted its attempts.
        A success obviously leaves the in-flight set; a permanent failure has to leave
        it too, or recovery would requeue the same poison payload forever. The log line
        is the current dead-letter record.
        """
        job = await self._queue.dequeue(
            timeout_ms=self._poll_timeout_ms if timeout_ms is None else timeout_ms
        )
        if job is None:
            return None
        try:
            await self.handle(job)
        finally:
            await self._queue.ack(job)
        return job

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Consume until ``stop`` is set, surviving a broken queue connection.

        A connection error is not fatal: Redis restarts, and a worker that exited on the
        first dropped socket would need a supervisor to bring it back. It is logged and
        retried after a second, which is also the fastest a restarted Redis can accept a
        connection again.
        """
        logger.info("worker.started", jobs=sorted(str(name) for name in self._handlers))
        last_recovery = 0.0
        while not stop.is_set():
            try:
                # Recover before consuming: a job abandoned by a previous process must
                # not wait behind the jobs that arrived after it.
                now = asyncio.get_running_loop().time()
                if now - last_recovery >= RECOVERY_INTERVAL_SECONDS:
                    last_recovery = now
                    try:
                        await self._queue.recover()
                    except Exception as exc:  # noqa: BLE001 - recovery is best-effort
                        logger.warning("worker.recovery_failed", error=str(exc)[:200])
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the loop outlives its dependencies
                logger.error("worker.queue_error", error=str(exc)[:300])
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
        logger.info("worker.stopped")


async def schedule_daily_cleanup(
    queue: QueuePort,
    *,
    stop: asyncio.Event,
    interval_seconds: float = 24 * 60 * 60,
    clock: Callable[[], datetime] = utc_now,
) -> None:
    """Enqueue the token purge once a day, starting immediately.

    The dedupe key carries the date, so restarting the worker three times in one day
    enqueues one job rather than three: the key is what makes "daily" mean daily
    independently of how often the process happens to restart.

    Sleeping through ``stop.wait()`` rather than ``asyncio.sleep`` is what lets a
    shutdown interrupt the wait instead of holding the container open for a day.
    """
    while not stop.is_set():
        today = clock()
        #: Two independent daily jobs. Both carry the date in their dedupe key, so a
        #: restart re-presents the same key instead of enqueueing the work twice.
        for name in (
            JobName.CLEANUP_EXPIRED_TOKENS,
            JobName.PURGE_AI_USAGE,
            JobName.PURGE_ABANDONED_SESSIONS,
            JobName.ROLLUP_DAILY_METRICS,
        ):
            if stop.is_set():
                break
            job = Job(name=name, dedupe_key=f"{name}:{today.date().isoformat()}")
            queued = await queue.enqueue(job)
            logger.info(
                "worker.cleanup_scheduled",
                job=str(name),
                queued=queued,
                job_id=str(job.job_id),
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)


def _install_stop_handlers(stop: asyncio.Event) -> None:
    """Turn SIGTERM/SIGINT into the same cooperative stop.

    ``add_signal_handler`` is not available on every platform (the Proactor loop on
    Windows raises ``NotImplementedError``), and the worker still has to be runnable
    there — ``Ctrl-C`` then arrives as ``KeyboardInterrupt`` instead, which
    :func:`main` already handles.
    """
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        with contextlib.suppress(NotImplementedError, AttributeError, RuntimeError, ValueError):
            import signal

            loop.add_signal_handler(getattr(signal, signal_name), stop.set)


async def main() -> None:
    """Boot, run both loops, and shut down cleanly.

    Configuration is validated *before* the first dequeue: a worker that starts on a
    production placeholder secret can write to the wrong database for a whole day
    before anyone reads the log.
    """
    from app.core.config import get_settings

    settings = get_settings()
    problems = settings.validate_environment()
    if problems:
        for problem in problems:
            logger.error("worker.config_invalid", problem=problem)
        raise SystemExit("refusing to start: " + "; ".join(problems))

    from app.api.deps import get_queue

    queue = get_queue()
    stop = asyncio.Event()
    _install_stop_handlers(stop)
    worker = Worker(
        queue,
        build_handlers(
            WorkerContext(
                uow_factory=make_uow,
                ai_usage_retention_days=settings.ai_usage_retention_days,
                abandoned_session_retention_days=settings.abandoned_session_retention_days,
                metrics_rollup_lookback_days=settings.metrics_rollup_lookback_days,
            )
        ),
    )

    try:
        await asyncio.gather(
            worker.run_forever(stop),
            schedule_daily_cleanup(queue, stop=stop),
        )
    except KeyboardInterrupt:  # pragma: no cover — operator pressed Ctrl-C
        stop.set()
    finally:
        await dispose_engine()
        logger.info("worker.shutdown_complete")


if __name__ == "__main__":  # pragma: no cover — process entry point
    asyncio.run(main())


__all__ = ["Worker", "main", "schedule_daily_cleanup"]
