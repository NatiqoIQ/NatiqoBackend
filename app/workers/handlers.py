"""Job handlers — the code that runs once the worker dequeues something.

A handler is a closure over the dependencies it needs rather than a function that
reaches for a global: the registry is built once per process in
:func:`build_handlers`, so a test can construct the same registry over a test unit of
work and exercise the real handler instead of a stand-in.

Handlers take the whole :class:`~app.platform.queue.port.Job` even when they ignore
its payload. The signature is the port's (``JobHandler``), and a handler that asked
only for a payload could not log which job failed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.logging import get_logger
from app.core.time import utc_now
from app.core.uow import UnitOfWorkProtocol
from app.platform.queue.port import Job, JobHandler, JobName

logger = get_logger("workers.handlers")

#: Builds a fresh unit of work. A callable rather than a factory type so the worker
#: process and a test can pass what each of them already has.
UowFactory = Callable[[], UnitOfWorkProtocol]


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """What a handler is allowed to use: persistence, and the clock."""

    uow_factory: UowFactory
    #: Injected so a test can pin "now" instead of writing rows relative to real time.
    now: Callable[[], datetime] = utc_now
    #: How long AI usage rows are kept. Zero disables the purge.
    ai_usage_retention_days: int = 180
    #: How long an unanswered, unfinished quiz session is kept. Zero disables the purge.
    abandoned_session_retention_days: int = 7
    #: How many complete days the dashboard rollup recomputes on each run. Zero disables it.
    metrics_rollup_lookback_days: int = 3


def build_handlers(ctx: WorkerContext) -> dict[JobName, JobHandler]:
    """The whole registry, one entry per job this build knows how to run.

    A job name with no entry is *not* silently dropped: :meth:`Worker.handle` logs it as
    an error, because a queue full of jobs nobody consumes looks exactly like an idle
    queue from the outside.
    """
    return {
        JobName.CLEANUP_EXPIRED_TOKENS: _cleanup_expired_tokens(ctx),
        JobName.PURGE_AI_USAGE: _purge_ai_usage(ctx),
        JobName.PURGE_ABANDONED_SESSIONS: _purge_abandoned_sessions(ctx),
        JobName.ROLLUP_DAILY_METRICS: _rollup_daily_metrics(ctx),
    }


def _cleanup_expired_tokens(ctx: WorkerContext) -> JobHandler:
    """Delete refresh tokens that can no longer authenticate anything.

    Chosen as the first real job deliberately: it needs no product decision (no time
    zone, no translation, no ``quiet_hours``), it is measurable as a row count, and it
    fails harmlessly — the worst case is that expired rows live one more day.

    It deletes only ``expires_at < now``. Revoked-but-unexpired rows stay, because a
    revoked token presented again is a reuse signal and that signal has to outlive the
    revocation.
    """

    async def handle(job: Job) -> None:
        batch_size = 5000
        #: A poisoned loop guard, not a business limit: if a batch keeps coming back
        #: full, something is not deleting what it selected, and running all night
        #: would hide that behind a row count.
        max_batches = 200
        total = 0
        async with ctx.uow_factory() as uow:
            for _ in range(max_batches):
                removed = await uow.refresh_tokens.purge_expired(before=ctx.now(), limit=batch_size)
                await uow.commit()
                total += removed
                if removed < batch_size:
                    break
            else:  # pragma: no cover - only reachable if deletes stop taking effect
                logger.warning(
                    "worker.tokens_purge_truncated",
                    removed=total,
                    job_id=str(job.job_id),
                )
        logger.info(
            "worker.tokens_purged",
            removed=total,
            job_id=str(job.job_id),
            job=str(job.name),
        )

    return handle


def _purge_ai_usage(ctx: WorkerContext) -> JobHandler:
    """Delete AI usage rows older than the retention window, in bounded batches.

    The usage log is append-only and grows with every model call. Its only live reader
    is the budget check, which looks at today's rows; older rows exist for cost
    attribution over a rolling window. Keeping them forever turns a small table into
    one of the largest in the database, and one unbounded DELETE would hold locks and
    write a single enormous WAL record.

    The window is configuration, and a non-positive value disables the job rather than
    deleting everything — the safe reading of "0 days" here is "keep", not "purge".
    """

    async def handle(job: Job) -> None:
        if ctx.ai_usage_retention_days <= 0:
            logger.info("worker.ai_usage_purge_disabled", job_id=str(job.job_id))
            return

        cutoff = ctx.now() - timedelta(days=ctx.ai_usage_retention_days)
        batch_size = 5000
        total = 0
        async with ctx.uow_factory() as uow:
            for _ in range(200):  # poison-loop guard, not a business limit
                removed = await uow.ai_usage.purge_before(before=cutoff, limit=batch_size)
                await uow.commit()
                total += removed
                if removed < batch_size:
                    break
            else:  # pragma: no cover - only if deletes stop taking effect
                logger.warning(
                    "worker.ai_usage_purge_truncated",
                    removed=total,
                    job_id=str(job.job_id),
                )
        logger.info(
            "worker.ai_usage_purged",
            removed=total,
            older_than_days=ctx.ai_usage_retention_days,
            job_id=str(job.job_id),
        )

    return handle


def _purge_abandoned_sessions(ctx: WorkerContext) -> JobHandler:
    """Delete quiz sessions nobody answered and nobody finished.

    An abandoned session is the row a learner leaves behind by tapping a lesson and
    closing the app. It earns nothing and appears in no feed, but at a hundred thousand
    learners it is a large share of ``quiz_sessions``. Sessions with answers are never
    touched: their rows are the source of the activity and accuracy figures, and deleting
    them later would rewrite history rather than reclaim space.
    """

    async def handle(job: Job) -> None:
        days = ctx.abandoned_session_retention_days
        if days <= 0:
            logger.info("worker.abandoned_session_purge_disabled", job_id=str(job.job_id))
            return
        cutoff = ctx.now() - timedelta(days=days)
        batch_size = 5000
        max_batches = 200
        total = 0
        async with ctx.uow_factory() as uow:
            for _ in range(max_batches):
                removed = await uow.quiz_sessions.purge_abandoned(before=cutoff, limit=batch_size)
                await uow.commit()
                total += removed
                if removed < batch_size:
                    break
            else:
                logger.warning(
                    "worker.abandoned_session_purge_truncated",
                    job_id=str(job.job_id),
                    removed=total,
                )
        logger.info(
            "worker.abandoned_sessions_purged",
            job_id=str(job.job_id),
            removed=total,
            retention_days=days,
        )

    return handle


def _rollup_daily_metrics(ctx: WorkerContext) -> JobHandler:
    """Rebuild the dashboard's daily rollup for the last few complete days.

    The rollup is what lets the overview stop scanning ``quiz_answers`` and
    ``ai_usage_records`` end to end. Recomputing instead of incrementing is deliberate: a
    late answer or a retried job can only make a day more correct, never double it.

    Today is not rolled up; the dashboard reads it live, so a row that is one day behind is
    never shown as the current day's total.
    """

    async def handle(job: Job) -> None:
        lookback = max(ctx.metrics_rollup_lookback_days, 0)
        if lookback == 0:
            logger.info("metrics.rollup_disabled", job_id=str(job.job_id))
            return
        end = ctx.now().date() - timedelta(days=1)
        start = end - timedelta(days=lookback - 1)
        async with ctx.uow_factory() as uow:
            # If the backfill has run, its marker also tells this job where the last
            # complete day ends. A worker that was down for a week therefore recomputes
            # the gap rather than the usual three days; without this, a long outage would
            # leave a hole the marker then claims is covered.
            coverage = await uow.daily_metrics.coverage()
            backfilled_through = coverage[1]
            if backfilled_through is not None:
                start = min(start, backfilled_through + timedelta(days=1))
            written = await uow.daily_metrics.recompute(start, end)
            if backfilled_through is not None:
                await uow.daily_metrics.advance(end)
            await uow.commit()
        logger.info(
            "metrics.rollup_written",
            start=start.isoformat(),
            end=end.isoformat(),
            days=written,
            job_id=str(job.job_id),
        )

    return handle


__all__ = ["UowFactory", "WorkerContext", "build_handlers"]
