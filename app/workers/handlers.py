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
from datetime import datetime

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


def build_handlers(ctx: WorkerContext) -> dict[JobName, JobHandler]:
    """The whole registry, one entry per job this build knows how to run.

    A job name with no entry is *not* silently dropped: :meth:`Worker.handle` logs it as
    an error, because a queue full of jobs nobody consumes looks exactly like an idle
    queue from the outside.
    """
    return {
        JobName.CLEANUP_EXPIRED_TOKENS: _cleanup_expired_tokens(ctx),
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


__all__ = ["UowFactory", "WorkerContext", "build_handlers"]
