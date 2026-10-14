"""The SM-2 schedule, exercised on its own.

``ReviewCard.schedule`` and the three pure helpers around it decide *when a learner sees a
question again*, and they do it with plain integers and floats — no database, no clock, no
HTTP. That is the point of keeping the rule in the domain: the one function that shapes the
review burden over months is the one piece that must be provably correct, and it is testable
without standing up anything.

Four contracts are pinned here:

* a card is born due in a day, in the default ease and the ``new`` state;
* a successful review walks the fixed steps (one day, then six) and only then multiplies by
  the ease factor, while a failure throws the run away and counts a lapse;
* the interval is computed from the ease *as it stood before* the review — the canonical
  SM-2 ordering, which is easy to get backwards and impossible to see from a single example;
* the ease factor never falls through its floor, however badly recall keeps going.
"""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from app.core.types import UserId
from app.modules.quiz.domain.review import (
    DEFAULT_EASE_FACTOR,
    MIN_EASE_FACTOR,
    REVIEW_INITIAL_INTERVAL_DAYS,
    ReviewCard,
    ReviewLog,
    ReviewLogStats,
    ReviewState,
    new_card,
    next_ease,
    next_interval,
    quality_for,
)

pytestmark = pytest.mark.unit

_MOMENT = datetime(2026, 3, 1, 9, 0, 0, tzinfo=UTC)


def _card(**overrides: object) -> ReviewCard:
    """A card mid-life, so a test only names the numbers it is actually about."""
    fields: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": UserId(uuid.uuid4()),
        "question_id": uuid.uuid4(),
        "ease_factor": DEFAULT_EASE_FACTOR,
        "interval_days": 6,
        "repetitions": 2,
        "lapses": 0,
        "due_at": _MOMENT,
        "last_reviewed_at": None,
        "state": ReviewState.REVIEW,
    }
    fields.update(overrides)
    return ReviewCard(**fields)  # type: ignore[arg-type]


# ==================================================================== newborn card
class TestNewCard:
    def test_it_is_due_tomorrow_in_the_default_ease(self) -> None:
        card = new_card(UserId(uuid.uuid4()), uuid.uuid4(), now=_MOMENT)

        assert card.ease_factor == DEFAULT_EASE_FACTOR
        assert card.interval_days == 0
        assert card.repetitions == 0
        assert card.lapses == 0
        assert card.state is ReviewState.NEW
        assert card.last_reviewed_at is None
        assert card.due_at == _MOMENT + timedelta(days=REVIEW_INITIAL_INTERVAL_DAYS)

    def test_two_cards_for_one_question_are_distinct_rows(self) -> None:
        user = UserId(uuid.uuid4())
        question = uuid.uuid4()

        first = new_card(user, question, now=_MOMENT)
        second = new_card(user, question, now=_MOMENT)

        assert first.id != second.id


# ======================================================================== grading
class TestQualityFor:
    @pytest.mark.parametrize(("is_correct", "quality"), [(True, 5), (False, 2)])
    def test_a_correct_first_attempt_is_the_strongest_recall(
        self, is_correct: bool, quality: int
    ) -> None:
        """Only two grades are reachable, and both sit where SM-2 expects them."""
        assert quality_for(is_correct) == quality

    def test_a_failure_is_below_the_passing_line(self) -> None:
        assert quality_for(is_correct=False) < 3


# =================================================================== ease factor
class TestNextEase:
    def test_a_perfect_recall_raises_the_ease(self) -> None:
        assert next_ease(DEFAULT_EASE_FACTOR, 5) == pytest.approx(2.6)

    def test_a_failure_lowers_the_ease(self) -> None:
        assert next_ease(DEFAULT_EASE_FACTOR, 2) == pytest.approx(2.18)

    def test_the_ease_never_falls_below_the_floor(self) -> None:
        """However many times recall fails, hard cards stop getting harder."""
        ease = DEFAULT_EASE_FACTOR
        for _ in range(50):
            ease = next_ease(ease, 0)

        assert ease == MIN_EASE_FACTOR

    def test_an_already_floor_ease_stays_at_the_floor(self) -> None:
        assert next_ease(MIN_EASE_FACTOR, 0) == MIN_EASE_FACTOR


# ======================================================================= interval
class TestNextInterval:
    def test_the_first_review_is_one_day_out(self) -> None:
        assert next_interval(repetitions=0, interval_days=0, ease_factor=2.5) == 1

    def test_the_second_review_is_six_days_out(self) -> None:
        assert next_interval(repetitions=1, interval_days=1, ease_factor=2.5) == 6

    def test_the_third_multiplies_the_previous_interval_by_the_ease(self) -> None:
        assert next_interval(repetitions=2, interval_days=6, ease_factor=2.5) == 15

    def test_it_uses_the_ease_it_is_given(self) -> None:
        """The step for the third review is ``interval * ease``, not a fixed table."""
        assert next_interval(repetitions=2, interval_days=6, ease_factor=1.3) == 8

    def test_a_zero_interval_never_collapses_the_step(self) -> None:
        """A multiply by a zeroed interval must still schedule at least a day out."""
        assert next_interval(repetitions=5, interval_days=0, ease_factor=2.5) == 1


# ================================================================== the schedule
class TestScheduleOnSuccess:
    def test_the_first_success_is_a_single_day_in_learning(self) -> None:
        card = _card(interval_days=0, repetitions=0, state=ReviewState.NEW)

        card.schedule(5, at=_MOMENT)

        assert card.interval_days == 1
        assert card.repetitions == 1
        assert card.state is ReviewState.LEARNING
        assert card.ease_factor == pytest.approx(2.6)
        assert card.last_reviewed_at == _MOMENT
        assert card.due_at == _MOMENT + timedelta(days=1)

    def test_the_second_success_is_six_days_and_graduates_the_card(self) -> None:
        card = _card(interval_days=1, repetitions=1, state=ReviewState.LEARNING)

        card.schedule(5, at=_MOMENT)

        assert card.interval_days == 6
        assert card.repetitions == 2
        assert card.state is ReviewState.REVIEW
        assert card.due_at == _MOMENT + timedelta(days=6)

    def test_the_third_success_multiplies_the_previous_interval(self) -> None:
        card = _card(interval_days=6, repetitions=2, ease_factor=2.5)

        card.schedule(5, at=_MOMENT)

        assert card.interval_days == 15
        assert card.repetitions == 3


class TestScheduleUsesTheEaseFromBeforeTheReview:
    def test_the_interval_grows_by_the_old_ease_not_the_new_one(self) -> None:
        """The canonical SM-2 ordering, which a single step cannot distinguish.

        A card at interval 6 with ease 2.5 advances 15 days, then its ease rises to 2.6.
        Computing the interval after the ease update would give 16 instead — an error that
        looks like nothing on any one card and compounds across every review.
        """
        card = _card(interval_days=6, repetitions=2, ease_factor=2.5)

        card.schedule(5, at=_MOMENT)

        assert card.interval_days == 15
        assert card.ease_factor == pytest.approx(2.6)


class TestScheduleOnFailure:
    def test_a_failure_resets_the_run_and_counts_a_lapse(self) -> None:
        card = _card(interval_days=15, repetitions=3, lapses=0, state=ReviewState.REVIEW)

        card.schedule(2, at=_MOMENT)

        assert card.repetitions == 0
        assert card.interval_days == 1
        assert card.lapses == 1
        assert card.state is ReviewState.RELEARNING
        assert card.ease_factor == pytest.approx(2.18)
        assert card.due_at == _MOMENT + timedelta(days=1)

    def test_the_lapse_counter_accumulates_across_failures(self) -> None:
        card = _card(lapses=4)

        card.schedule(2, at=_MOMENT)

        assert card.lapses == 5

    def test_a_relapsed_card_rebuilds_from_the_first_step(self) -> None:
        """After failing, a success is the one-day step again, not a jump to six."""
        card = _card(interval_days=60, repetitions=5, state=ReviewState.REVIEW)
        card.schedule(2, at=_MOMENT)

        card.schedule(5, at=_MOMENT)

        assert card.interval_days == 1
        assert card.repetitions == 1
        assert card.state is ReviewState.LEARNING


class TestScheduleDefaultsToNow:
    def test_a_missing_moment_is_filled_from_the_clock(self) -> None:
        card = _card(interval_days=0, repetitions=0, state=ReviewState.NEW)

        before = datetime.now(UTC)
        card.schedule(5)
        after = datetime.now(UTC)

        assert card.last_reviewed_at is not None
        assert before <= card.last_reviewed_at <= after


# ======================================================================= the log
class TestReviewLog:
    def test_it_is_immutable(self) -> None:
        """An append-only record that can be rewritten is not a record."""
        log = ReviewLog(
            user_id=UserId(uuid.uuid4()),
            card_id=uuid.uuid4(),
            quality=5,
            previous_interval_days=6,
            new_interval_days=15,
        )

        with pytest.raises(FrozenInstanceError):
            log.quality = 2  # type: ignore[misc]

    def test_it_carries_both_intervals_and_a_moment(self) -> None:
        """The card remembers only the latest interval, so the log has to keep both."""
        log = ReviewLog(
            user_id=UserId(uuid.uuid4()),
            card_id=uuid.uuid4(),
            quality=2,
            previous_interval_days=15,
            new_interval_days=1,
            reviewed_at=_MOMENT,
        )

        assert (log.previous_interval_days, log.new_interval_days) == (15, 1)
        assert log.reviewed_at == _MOMENT
        assert log.id is not None


class TestReviewLogStats:
    def test_no_reviews_reports_no_retention(self) -> None:
        """Undefined is reported as zero rather than as a division by nothing."""
        assert ReviewLogStats(total=0, correct=0).retention_percent == 0

    @pytest.mark.parametrize(
        ("total", "correct", "percent"),
        [(10, 10, 100), (10, 0, 0), (4, 3, 75), (3, 2, 67), (6, 5, 83)],
    )
    def test_retention_is_the_share_that_succeeded(
        self, total: int, correct: int, percent: int
    ) -> None:
        assert ReviewLogStats(total=total, correct=correct).retention_percent == percent
