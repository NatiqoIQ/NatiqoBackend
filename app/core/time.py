"""Time helpers.

Two rules are enforced across the whole backend:

1. **Storage is always UTC.** Every ``TIMESTAMP WITH TIME ZONE`` value is written
   in UTC; no local time ever reaches the database.
2. **Calendar semantics follow the user.** Streaks, daily goals, daily challenges
   and notification quiet hours are computed against the user's IANA timezone
   (``Asia/Baghdad`` by default) — the server decides the calendar date, the client
   never does.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Baghdad"
FALLBACK_TIMEZONE = "UTC"


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Normalise any datetime to timezone-aware UTC.

    Naive datetimes are assumed to already be UTC (never local machine time).
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def ensure_aware(value: datetime) -> datetime:
    """Attach UTC to a naive datetime read back from a driver that dropped tzinfo."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def get_timezone(name: str | None) -> ZoneInfo:
    """Resolve an IANA timezone name, falling back to UTC when unknown."""
    if not name:
        return ZoneInfo(FALLBACK_TIMEZONE)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(FALLBACK_TIMEZONE)


def is_valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def local_date(moment: datetime, timezone: str | None) -> date:
    """The calendar date a learner sees for ``moment`` in their own timezone."""
    return to_utc(moment).astimezone(get_timezone(timezone)).date()


def local_datetime(moment: datetime, timezone: str | None) -> datetime:
    return to_utc(moment).astimezone(get_timezone(timezone))


def start_of_local_day(day: date, timezone: str | None) -> datetime:
    """UTC instant at which ``day`` begins in the learner's timezone."""
    tz = get_timezone(timezone)
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)


def end_of_local_day(day: date, timezone: str | None) -> datetime:
    """UTC instant at which ``day`` ends (exclusive) in the learner's timezone."""
    return start_of_local_day(day + timedelta(days=1), timezone)


def local_day_bounds(
    timezone: str | None = None, *, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Half-open ``[start, end)`` UTC range covering the learner's current day.

    Used by every "today" query — daily goal, streak freeze, daily challenge, AI
    budget — so they all agree on where a day starts. Asia/Baghdad has no DST, but
    the helper is timezone-generic so it stays correct for users elsewhere.
    """
    moment = to_utc(now) if now is not None else utc_now()
    return (
        start_of_local_day(moment.astimezone(get_timezone(timezone)).date(), timezone),
        end_of_local_day(moment.astimezone(get_timezone(timezone)).date(), timezone),
    )


def days_between(earlier: date, later: date) -> int:
    """Whole calendar days from ``earlier`` to ``later`` (may be negative)."""
    return (later - earlier).days


def add_days(moment: datetime, days: float) -> datetime:
    return moment + timedelta(days=days)


def humanize_seconds(seconds: float) -> str:
    """Compact duration used in notification and API copy."""
    total = int(max(0, seconds))
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
