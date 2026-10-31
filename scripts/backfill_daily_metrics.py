"""Backfill the admin dashboard's daily rollup (F19).

    uv run python -m scripts.backfill_daily_metrics --dry-run
    uv run python -m scripts.backfill_daily_metrics
    uv run python -m scripts.backfill_daily_metrics --from 2024-01-01 --to 2024-06-30

Why this is a script and not a migration: the first backfill groups every answer, review
and AI call the product has ever written. On a real database that is minutes of read work,
and putting it inside ``alembic upgrade`` would make a deploy depend on it. The migration
only creates the empty tables; until this script finishes and writes the marker, the API
keeps using its original live queries, so a slow or abandoned backfill is invisible rather
than wrong.

Idempotent: every chunk deletes and rewrites its days, so rerunning after an interruption
is safe. The marker is written last, and only when the requested range reaches yesterday.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core.database import dispose_engine, get_session_factory
from app.core.logging import get_logger
from app.core.time import utc_now
from app.modules.admin.infrastructure.daily_metrics import DailyMetricsRepository
from app.modules.admin.infrastructure.metrics_models import MetricsMetaModel
from app.modules.ai.infrastructure.models import AiUsageRecord
from app.modules.quiz.infrastructure.models import QuizAnswerModel, QuizSessionModel
from app.modules.quiz.infrastructure.review_models import ReviewLogModel

logger = get_logger("scripts.backfill_daily_metrics")

_CHUNK_DAYS = 31


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


async def _earliest_day(session: Any) -> date | None:
    """The first day any of the four rolled-up tables has a row for."""
    candidates: list[date | None] = []
    for statement in (
        select(func.min(QuizSessionModel.completed_at)).where(
            QuizSessionModel.status == "completed"
        ),
        select(func.min(QuizAnswerModel.answered_at)),
        select(func.min(ReviewLogModel.reviewed_at)),
        select(func.min(AiUsageRecord.created_at)),
    ):
        candidates.append(_as_date(await session.scalar(statement)))
    present = [day for day in candidates if day is not None]
    return min(present) if present else None


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


async def backfill(*, start: date | None, end: date | None, mark: bool, dry_run: bool) -> int:
    yesterday = utc_now().date() - timedelta(days=1)
    session_factory = get_session_factory()
    written = 0
    async with session_factory() as session:
        first = start or await _earliest_day(session)
        if first is None:
            print("no events to roll up; nothing to backfill")
            return 0
        last = min(end or yesterday, yesterday)
        if last < first:
            print(f"range is empty: {first} to {last}")
            return 0

        print(f"backfilling {first} .. {last} ({(last - first).days + 1} days)")
        if dry_run:
            return 0

        repository = DailyMetricsRepository(session)
        cursor = first
        while cursor <= last:
            chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS - 1), last)
            count = await repository.recompute(cursor, chunk_end)
            await session.commit()
            written += count
            print(f"  {cursor} .. {chunk_end}: {count} day rows")
            cursor = chunk_end + timedelta(days=1)

        if mark and last >= yesterday:
            await repository.mark_backfilled(start=first, through=last)
            await session.commit()
            meta = await session.get(MetricsMetaModel, 1)
            print(f"rollout marker written: {meta.backfilled_from} .. {meta.backfilled_through}")
        elif mark:
            print(
                f"marker not written: backfill ends {last}, before yesterday {yesterday}. "
                "The dashboard keeps using live queries."
            )

    logger.info("metrics.backfill_complete", days=written, start=str(first), end=str(last))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill the admin daily rollup.")
    parser.add_argument("--from", dest="start", type=_parse_day, default=None)
    parser.add_argument("--to", dest="end", type=_parse_day, default=None)
    parser.add_argument(
        "--no-mark",
        action="store_true",
        help="write the days but do not switch the dashboard over to the rollup",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the range, write nothing")
    args = parser.parse_args(argv)

    async def run() -> int:
        try:
            return await backfill(
                start=args.start, end=args.end, mark=not args.no_mark, dry_run=args.dry_run
            )
        finally:
            await dispose_engine()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:  # pragma: no cover - operator pressed Ctrl-C
        print("interrupted; rerun the same command to finish after the last committed chunk")
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
