"""Selection of a checkpoint's sample: the weights, the coverage guarantee, the order.

Pure-function tests with no fixtures or database — the module under test takes facts and
returns ids, so every case here is a table of facts and an expected outcome. The cases are
grouped by the three decisions the module docstring stakes out: what each weight means,
what coverage guarantees regardless of size, and how the sitting is ordered.
"""

from __future__ import annotations

import random
import uuid

import pytest

from app.modules.quiz.domain.selection import (
    CHECKPOINT_TARGET,
    QuestionStat,
    select_checkpoint_questions,
    weight,
)

SEED = 20261019


def stat(
    lesson: str,
    position: int,
    *,
    question: str | None = None,
    answered: bool = True,
    was_wrong: bool = False,
    card_lapses: int = 0,
    card_due: bool = False,
    card_easy: bool = False,
) -> QuestionStat:
    return QuestionStat(
        question_id=uuid.uuid5(uuid.NAMESPACE_URL, question or f"{lesson}-{position}"),
        lesson_id=uuid.uuid5(uuid.NAMESPACE_URL, lesson),
        position=position,
        answered=answered,
        was_wrong=was_wrong,
        card_lapses=card_lapses,
        card_due=card_due,
        card_easy=card_easy,
    )


class TestWeight:
    def test_a_never_answered_question_carries_the_unseen_bonus(self) -> None:
        assert weight(stat("l1", 1, answered=False)) == 2.0
        assert weight(stat("l1", 1, answered=True)) == 1.0

    def test_each_weakness_signal_adds_its_bonus_to_the_base(self) -> None:
        # The brief's numbers: +3 missed, +2 lapsed, +2 due, +1 unseen.
        assert weight(stat("l1", 1, was_wrong=True)) == 4.0
        assert weight(stat("l1", 1, card_lapses=1)) == 3.0
        assert weight(stat("l1", 1, card_due=True)) == 3.0
        assert weight(stat("l1", 1, answered=False)) == 2.0

    def test_signals_stack_rather_than_replace_each_other(self) -> None:
        worst = stat(
            "l1",
            1,
            answered=False,
            was_wrong=True,
            card_lapses=2,
            card_due=True,
        )
        assert weight(worst) == 1.0 + 1.0 + 3.0 + 2.0 + 2.0

    def test_an_easy_card_halves_but_never_erases_the_question(self) -> None:
        easy = weight(stat("l1", 1, card_easy=True))
        assert easy == 0.5, "the easy penalty is multiplicative, on the weighted total"
        assert easy > 0, "a down-weighted question must stay eligible for the sample"

    def test_every_weight_is_strictly_positive(self) -> None:
        # The floor is what lets the weighted draw divide by the pool total at all, and
        # what keeps an already-mastered question in the sample's reach.
        comfortable = stat("l1", 1, answered=True, card_easy=True)
        assert weight(comfortable) > 0


class TestCoverage:
    def test_every_lesson_gets_a_question_even_below_the_target(self) -> None:
        # Three lessons, target of two: coverage wins over the cap, by design.
        stats = [stat("l1", 1), stat("l2", 1), stat("l3", 1)]
        picked = select_checkpoint_questions(stats, target=2, rng=random.Random(SEED))
        chosen = set(picked)
        assert {s.question_id for s in stats} <= chosen

    def test_coverage_alone_can_exceed_the_target(self) -> None:
        # A unit with more lessons than the target asks one question per lesson anyway —
        # a sample that skips a lesson cannot certify the unit.
        stats = [stat(f"l{i}", 1) for i in range(20)]
        picked = select_checkpoint_questions(stats, target=CHECKPOINT_TARGET, rng=random.Random(SEED))
        assert len(picked) == 20

    def test_the_coverage_slot_goes_to_the_highest_weight_question(self) -> None:
        easy = stat("l1", 1, answered=True, card_easy=True)
        missed = stat("l1", 2, was_wrong=True)
        picked = select_checkpoint_questions(
            [easy, missed], target=1, rng=random.Random(SEED)
        )
        assert picked == [missed.question_id]

    def test_a_weight_tie_falls_to_catalogue_position(self) -> None:
        first = stat("l1", 1)
        second = stat("l1", 2)
        picked = select_checkpoint_questions(
            [first, second], target=1, rng=random.Random(SEED)
        )
        assert picked == [first.question_id]


class TestFill:
    def test_the_sample_stops_at_the_target_when_the_pool_is_larger(self) -> None:
        stats = [stat("l1", i) for i in range(1, 21)]
        picked = select_checkpoint_questions(stats, target=12, rng=random.Random(SEED))
        assert len(picked) == 12

    def test_a_pool_smaller_than_the_target_is_asked_in_full_exactly_once(self) -> None:
        stats = [stat("l1", i) for i in range(1, 6)]
        picked = select_checkpoint_questions(stats, target=12, rng=random.Random(SEED))
        assert len(picked) == 5
        assert len(set(picked)) == 5, "a question must never appear twice in one sitting"

    def test_the_weighted_fill_reaches_repeatedly_for_a_known_gap(self) -> None:
        # Structure so the weak question *loses* its lesson's coverage slot to a
        # stronger sibling — then the only way into the sample is the weighted fill.
        # Three lessons of plain questions plus the l3 pair give a four-question
        # coverage guarantee and a fill pool of "weak among plains"; drawing the same
        # sample for 100 seeds shows whether the fill favours the gap or ignores it.
        stats = [stat("l1", 1), stat("l2", 1)]
        stats += [stat("l3", position) for position in range(3, 13)]  # ten plains
        stats.append(stat("l3", 1, question="sibling", answered=False, was_wrong=True, card_due=True))
        weak = stat("l3", 2, question="the-gap", was_wrong=True, card_due=True)
        stats.append(weak)

        hits = sum(
            weak.question_id
            in select_checkpoint_questions(stats, target=4, rng=random.Random(seed))
            for seed in range(100)
        )

        # A uniform draw would land the gap about 10 of 100 times (1 in the pool of ~11);
        # at weight 6 against 1s the expected count is roughly 40. The threshold sits
        # between the two — far enough above chance that the seed cannot fluke it, far
        # enough below the expected value that the test does not hinge on one draw.
        assert hits >= 25, f"the weighted fill ignored a known gap ({hits}/100 draws)"

    def test_the_same_seed_draws_the_same_sample(self) -> None:
        stats = [stat(f"l{i%3}", i) for i in range(1, 30)]
        first = select_checkpoint_questions(stats, target=12, rng=random.Random(SEED))
        second = select_checkpoint_questions(stats, target=12, rng=random.Random(SEED))
        assert first == second

    def test_a_target_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            select_checkpoint_questions([stat("l1", 1)], target=0)

    def test_an_empty_unit_asks_nothing(self) -> None:
        assert select_checkpoint_questions([], rng=random.Random(SEED)) == []


class TestOrdering:
    def test_previously_missed_questions_alternate_with_the_rest(self) -> None:
        # Four lessons so four questions are guaranteed; two missed, two not.
        stats = [
            stat("l1", 1, was_wrong=True),
            stat("l2", 1),
            stat("l3", 1, was_wrong=True),
            stat("l4", 1),
        ]
        picked = select_checkpoint_questions(stats, target=4, rng=random.Random(SEED))
        by_id = {s.question_id: s for s in stats}
        flags = [by_id[question_id].was_wrong for question_id in picked]

        assert flags == [True, False, True, False], (
            "misses must open but never clump: five in a row reads as failure"
        )

    def test_ordering_preserves_the_sample_regardless_of_group_sizes(self) -> None:
        stats = [stat("l1", 1, was_wrong=True)] + [stat(f"l{i}", 1) for i in range(2, 7)]
        picked = select_checkpoint_questions(stats, target=6, rng=random.Random(SEED))
        assert len(picked) == 6
        assert len(set(picked)) == 6
