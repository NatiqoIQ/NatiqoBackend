"""AI usage records (domain).

``ai_usage_records`` is append-only, so its repository needs exactly two verbs: put a
row in, and say whether a row is already there. The second is not decoration. A row's
primary key is random by default, so a maintenance command that inserts usage rows
twice would double a learner's spend history on every rerun. Exposing "insert unless
this id exists" lets a caller that controls the id make a rerun a no-op, and lets it
report honestly what the run changed instead of guessing.

The metering recorder in :mod:`app.modules.ai.infrastructure.usage` deliberately does
*not* use this port — it runs on its own session and must never fail a request. This
port exists for callers that are already inside a transaction and want the row to
land with it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.core.types import UserId


@dataclass(frozen=True, slots=True)
class AiUsageEntry:
    """One metered AI call, as the domain sees it."""

    id: uuid.UUID
    user_id: UserId
    feature: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: Decimal = Decimal("0")
    status: str = "ok"
    error: str | None = None
    #: Supplied only when the caller means to backdate the row. ``None`` leaves the
    #: column to the database clock, which is what a live call wants.
    created_at: datetime | None = None


@runtime_checkable
class AiUsageRepositoryPort(Protocol):
    """Read/write access to the append-only usage log.

    Implementations must not commit, for the same reason every other repository in the
    codebase does not: the unit of work owns the transaction so a caller can read,
    decide and write atomically.
    """

    async def get(self, entry_id: uuid.UUID) -> AiUsageEntry | None: ...

    async def add(self, entry: AiUsageEntry) -> None: ...

    async def add_if_absent(self, entry: AiUsageEntry) -> bool:
        """Add ``entry`` unless a row with that id is already there.

        Returns ``True`` when a row was written, so a caller can report what a run
        actually changed.

        Not atomic against a second writer racing the same id: the primary-key
        collision surfaces as :class:`sqlalchemy.exc.IntegrityError` at flush time.
        That is the right trade for a single-writer maintenance command, and it is
        stated here so nobody mistakes this for a safe upsert.
        """
        ...

    async def purge_before(self, *, before: datetime, limit: int = 5000) -> int:
        """Delete at most ``limit`` rows older than ``before`` and return how many.

        Usage is kept for cost attribution and abuse review over a rolling window, not
        forever: a learner generating a few calls a day produces millions of rows a
        year across the platform. Budget checks only read today's rows, so an old row
        has no reader once its window has passed.

        Bounded on purpose — the caller loops and commits between batches, so one
        statement cannot hold a lock or write an unbounded WAL record.
        """
        ...


__all__ = ["AiUsageEntry", "AiUsageRepositoryPort"]
