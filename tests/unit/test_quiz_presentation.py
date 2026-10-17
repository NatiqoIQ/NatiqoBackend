"""Choice ordering, exercised on its own.

``presentation`` is a pure function of a seed, so the whole contract is reachable without a
database or an HTTP request — which is the reason the permutation lives in the domain rather
than inside the service. Three things have to hold for the reshuffle to be trustworthy, and
each is pinned here:

* the order is a **deterministic permutation** — same seed, same order, no options lost or
  duplicated, so grading can rebuild exactly what the client was shown;
* the seed is fixed by the **session, the question and the attempt**, so a reload is stable
  and a retry is a fresh layout;
* the correct index is **remapped into display space**, so a letter or index the client
  derived from what it saw resolves back to the right choice.
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.quiz.domain.presentation import (
    choice_order,
    deranged_order,
    order_choices,
    order_correct_index,
    ordering_order,
    question_seed,
)

pytestmark = pytest.mark.unit


def _seed(*, session: int = 1, question: int = 1, attempt: int = 1) -> str:
    return question_seed(
        session_id=uuid.UUID(int=session),
        question_id=uuid.UUID(int=question),
        attempt=attempt,
    )


# --------------------------------------------------------------------- the seed
class TestQuestionSeed:
    def test_it_is_stable_for_the_same_session_question_and_attempt(self) -> None:
        assert _seed(session=7, question=9, attempt=3) == _seed(session=7, question=9, attempt=3)

    def test_each_input_moves_the_seed(self) -> None:
        base = _seed(session=1, question=1, attempt=1)

        assert base != _seed(session=2, question=1, attempt=1)
        assert base != _seed(session=1, question=2, attempt=1)
        assert base != _seed(session=1, question=1, attempt=2)


# --------------------------------------------------------------------- the order
class TestChoiceOrder:
    def test_it_is_a_permutation_of_the_choices(self) -> None:
        order = choice_order(5, seed=_seed())

        assert sorted(order) == [0, 1, 2, 3, 4]

    def test_the_same_seed_yields_the_same_order(self) -> None:
        assert choice_order(6, seed=_seed(attempt=2)) == choice_order(6, seed=_seed(attempt=2))

    def test_a_retry_generally_reorders_the_choices(self) -> None:
        """Not a guarantee of *difference* — six permutations collide — but the mechanism
        must not collapse to the stored order for every question, which is the failure that
        would make the feature quietly do nothing."""
        first = [choice_order(4, seed=_seed(session=n, attempt=1)) for n in range(12)]
        second = [choice_order(4, seed=_seed(session=n, attempt=2)) for n in range(12)]

        assert all(sorted(order) == [0, 1, 2, 3] for order in first + second)
        assert any(a != b for a, b in zip(first, second, strict=True))

    def test_a_single_choice_has_no_order_to_apply(self) -> None:
        assert choice_order(1, seed=_seed()) == (0,)

    def test_no_choices_has_no_order_to_apply(self) -> None:
        assert choice_order(0, seed=_seed()) == ()


# ------------------------------------------------------------------ the remapping
class TestOrderChoices:
    def test_it_permutes_the_choices_without_loss(self) -> None:
        choices = ("Hello", "Goodbye", "Thank you")
        order = choice_order(len(choices), seed=_seed(attempt=3))

        served = order_choices(choices, order)

        assert sorted(served) == sorted(choices)
        assert served == tuple(choices[index] for index in order)

    def test_an_empty_question_stays_empty(self) -> None:
        assert order_choices((), ()) == ()


class TestOrderCorrectIndex:
    def test_it_maps_the_key_into_display_space(self) -> None:
        choices = ("Hello", "Goodbye", "Thank you")
        order = choice_order(len(choices), seed=_seed(attempt=2))
        served = order_choices(choices, order)

        display_index = order_correct_index(order, choices.index("Goodbye"))

        assert served[display_index] == "Goodbye"

    def test_every_choice_survives_the_round_trip(self) -> None:
        choices = ("a", "b", "c", "d", "e", "f")
        order = choice_order(len(choices), seed=_seed(question=4))
        served = order_choices(choices, order)

        for original_index, text in enumerate(choices):
            assert served[order_correct_index(order, original_index)] == text


class TestOrderingOrder:
    """The ordering reshuffle must never equal the authored order.

    For an ordering question the served sequence *is* a candidate answer, so an identity
    permutation — which :func:`choice_order` is allowed to produce — would leak the key in
    the payload. :func:`ordering_order` guarantees a non-identity permutation instead.
    """

    def test_it_is_never_the_identity(self) -> None:
        for size in range(2, 7):
            for n in range(50):
                order = ordering_order(size, seed=_seed(session=n, question=n, attempt=1))

                assert order != tuple(range(size))

    def test_it_is_still_a_permutation(self) -> None:
        for size in range(2, 7):
            order = ordering_order(size, seed=_seed(question=size))

            assert sorted(order) == list(range(size))

    def test_it_is_deterministic(self) -> None:
        assert ordering_order(4, seed=_seed(attempt=2)) == ordering_order(4, seed=_seed(attempt=2))

    def test_a_single_item_has_nothing_to_shuffle(self) -> None:
        assert ordering_order(1, seed=_seed()) == (0,)

    def test_it_still_reorders_across_attempts(self) -> None:
        first = [ordering_order(4, seed=_seed(session=n, attempt=1)) for n in range(12)]
        second = [ordering_order(4, seed=_seed(session=n, attempt=2)) for n in range(12)]

        assert all(sorted(order) == [0, 1, 2, 3] for order in first + second)
        assert any(a != b for a, b in zip(first, second, strict=True))


class TestDerangedOrder:
    """The matching bank's shuffle: never seat an option opposite the prompt it answers.

    A matching question is served as two columns read side by side, so the arrangement that
    must not come out is the one where every option faces the prompt it answers — which is the
    order the pairs were authored in, and therefore the most likely coincidence of a plain
    shuffle. Where :func:`ordering_order` avoids a single arrangement, this has to avoid one
    per position; ``forbidden[j]`` says which option may not stand at position ``j``.
    """

    def test_it_is_a_permutation_with_nothing_lost(self) -> None:
        for size in range(2, 7):
            order = deranged_order(size, forbidden=tuple(range(size)), seed=_seed(question=size))

            assert sorted(order) == list(range(size))

    def test_no_option_is_left_facing_the_prompt_it_answers(self) -> None:
        """The strictest case there is: every position constrained, in the authored order."""
        for size in range(2, 7):
            for n in range(50):
                forbidden = tuple(range(size))
                order = deranged_order(
                    size, forbidden=forbidden, seed=_seed(session=n, question=n, attempt=1)
                )

                assert all(order[position] != forbidden[position] for position in range(size))

    def test_a_bank_with_distractors_only_constrains_the_prompts(self) -> None:
        """Three prompts against a five-option bank: the last two positions are free."""
        order = deranged_order(5, forbidden=(0, 1, 2), seed=_seed())

        assert sorted(order) == [0, 1, 2, 3, 4]
        assert all(order[position] != position for position in range(3))

    def test_an_unanswered_position_is_left_free(self) -> None:
        order = deranged_order(4, forbidden=(None, 1, None, 3), seed=_seed())

        assert sorted(order) == [0, 1, 2, 3]
        assert order[1] != 1
        assert order[3] != 3

    def test_it_is_deterministic(self) -> None:
        assert deranged_order(4, forbidden=(0, 1, 2, 3), seed=_seed(attempt=2)) == deranged_order(
            4, forbidden=(0, 1, 2, 3), seed=_seed(attempt=2)
        )

    def test_a_single_option_has_nothing_to_avoid(self) -> None:
        assert deranged_order(1, forbidden=(0,), seed=_seed()) == (0,)

    def test_it_still_reorders_across_attempts(self) -> None:
        first = [
            deranged_order(4, forbidden=(0, 1, 2, 3), seed=_seed(session=n, attempt=1))
            for n in range(12)
        ]
        second = [
            deranged_order(4, forbidden=(0, 1, 2, 3), seed=_seed(session=n, attempt=2))
            for n in range(12)
        ]

        assert all(sorted(order) == [0, 1, 2, 3] for order in first + second)
        assert any(a != b for a, b in zip(first, second, strict=True))
