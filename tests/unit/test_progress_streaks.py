"""Streak and accuracy arithmetic.

Both figures are derived rather than stored, so the arithmetic *is* the spec. They are
pure functions over dates and counts, which is why they are tested here rather than
through HTTP: the interesting cases are a gap, a duplicate day, an empty set and a
timezone that puts an instant on a different calendar day than the UTC clock says.

The timezone cases matter because that is the whole reason the streak is not computed in
SQL. ``app/core/time.py`` resolves an IANA zone per learner, and a query that grouped by
UTC date would silently move a late-evening session onto the wrong day for anyone east of
GMT.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.time import utc_now
from app.modules.quiz.application.progress_service import _streaks
from app.modules.quiz.domain.entities import AnswerTally

#: Two instants chosen so that their UTC calendar days are two apart while their days in
#: Asia/Baghdad (UTC+3) are adjacent. ``_streaks`` must see a 2-day run under Baghdad and
#: a 1-day run under UTC, which is only true if the zone is actually applied.
_CROSSING = (
    datetime(2026, 1, 1, 23, 0, 0, tzinfo=UTC),
    datetime(2026, 1, 3, 1, 0, 0, tzinfo=UTC),
)


class TestStreaks:
    @staticmethod
    def _today() -> datetime:
        """A pinned instant on the current UTC day, at an hour that never rolls over."""
        return utc_now().replace(hour=12)

    def test_a_learner_with_no_sessions_has_no_streak(self) -> None:
        assert _streaks([], None) == (0, 0)

    def test_a_single_session_today_is_a_one_day_streak(self) -> None:
        assert _streaks([self._today()], "UTC") == (1, 1)

    def test_a_session_yesterday_still_counts(self) -> None:
        yesterday = self._today() - timedelta(days=1)

        assert _streaks([yesterday], "UTC") == (1, 1)

    def test_a_gap_ends_the_current_streak_but_keeps_the_longest(self) -> None:
        today = self._today()
        times = [
            today,
            today - timedelta(days=4),
            today - timedelta(days=5),
        ]

        current, longest = _streaks(times, "UTC")

        assert current == 1
        assert longest == 2

    def test_a_streak_that_never_reaches_today_is_zero(self) -> None:
        today = self._today()
        times = [today - timedelta(days=6), today - timedelta(days=7)]

        assert _streaks(times, "UTC") == (0, 2)

    def test_two_sessions_on_one_day_count_once(self) -> None:
        """A set, not a list: practising twice in a day must not inflate the run."""
        today = self._today()

        assert _streaks([today, today - timedelta(hours=1)], "UTC") == (1, 1)

    def test_the_calendar_follows_the_learner_timezone_not_the_utc_clock(self) -> None:
        """The regression guard for computing the streak in the wrong zone."""
        assert _streaks(list(_CROSSING), "Asia/Baghdad") == (0, 2)
        assert _streaks(list(_CROSSING), "UTC") == (0, 1)


class TestAnswerTallyAccuracy:
    def test_no_answers_is_not_a_division_error(self) -> None:
        assert AnswerTally(answered=0, correct=0).accuracy_percent == 0

    def test_a_half_correct_tally_is_fifty_percent(self) -> None:
        assert AnswerTally(answered=4, correct=2).accuracy_percent == 50

    def test_accuracy_rounds_to_whole_percent(self) -> None:
        assert AnswerTally(answered=3, correct=2).accuracy_percent == 67

    def test_a_perfect_tally_is_one_hundred(self) -> None:
        assert AnswerTally(answered=5, correct=5).accuracy_percent == 100

    def test_the_default_is_an_empty_tally(self) -> None:
        tally = AnswerTally()

        assert (tally.answered, tally.correct, tally.accuracy_percent) == (0, 0, 0)
