"""The grading rule, exercised on its own.

``grade_answer`` is a pure function over a question and a submitted answer, so every branch
is reachable without a database or an HTTP request. That is the point of keeping the rule
in the domain: the thing that decides right from wrong is the one piece of the engine that
must be provably correct, and it is testable in isolation.

Three contracts are pinned here. The verdict is computed against the stored index however the
client phrased its answer (text, index or letter); the correct answer plus the explanation
come back only from a grading call — never from the question itself; and a correct answer is
worth full marks only on the first attempt, half afterwards.
"""

from __future__ import annotations

import pytest

from app.core.errors import ErrorCode, ValidationFailed
from app.core.types import QuestionKind
from app.modules.quiz.domain.policy import (
    FIRST_ATTEMPT,
    GradeableQuestion,
    SubmittedAnswer,
    grade_answer,
    points_awarded,
)

pytestmark = pytest.mark.unit


def _multiple_choice(
    *,
    choices: tuple[str, ...] = ("Hello", "Goodbye", "Thank you"),
    correct_index: int = 1,
    points: int = 2,
    explanation: str = "Goodbye is the farewell.",
) -> GradeableQuestion:
    return GradeableQuestion(
        kind=QuestionKind.MULTIPLE_CHOICE,
        choices=choices,
        correct_index=correct_index,
        points=points,
        explanation=explanation,
    )


def _true_false(*, correct_index: int = 0) -> GradeableQuestion:
    return GradeableQuestion(
        kind=QuestionKind.TRUE_FALSE,
        choices=("True", "False"),
        correct_index=correct_index,
        points=1,
        explanation="It is true.",
    )


def _fill_blank(*, accepted: tuple[str, ...] = ("goes",)) -> GradeableQuestion:
    return GradeableQuestion(
        kind=QuestionKind.FILL_BLANK,
        accepted_answers=accepted,
        points=3,
        explanation="Third person singular takes -s.",
    )


class TestMultipleChoice:
    def test_the_choice_text_is_accepted(self) -> None:
        graded = grade_answer(_multiple_choice(), SubmittedAnswer(choice="Goodbye"))

        assert graded.is_correct is True
        assert graded.points_awarded == 2

    def test_the_zero_based_index_is_accepted(self) -> None:
        graded = grade_answer(_multiple_choice(), SubmittedAnswer(choice="1"))

        assert graded.is_correct is True

    def test_the_letter_is_accepted(self) -> None:
        graded = grade_answer(_multiple_choice(), SubmittedAnswer(choice="b"))

        assert graded.is_correct is True

    def test_a_different_choice_is_wrong_and_scores_nothing(self) -> None:
        graded = grade_answer(_multiple_choice(), SubmittedAnswer(choice="Hello"))

        assert graded.is_correct is False
        assert graded.points_awarded == 0

    def test_the_correct_answer_is_reported_as_text(self) -> None:
        """Whatever shape the client sent, the response is canonical: the choice's text."""
        graded = grade_answer(_multiple_choice(), SubmittedAnswer(choice="b"))

        assert graded.correct_answer == {"choice": "Goodbye"}

    def test_the_explanation_comes_back_with_the_verdict(self) -> None:
        graded = grade_answer(_multiple_choice(), SubmittedAnswer(choice="Hello"))

        assert graded.explanation == "Goodbye is the farewell."

    def test_a_choice_that_matches_nothing_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed) as raised:
            grade_answer(_multiple_choice(), SubmittedAnswer(choice="orange"))

        assert raised.value.code is ErrorCode.VALIDATION_ERROR

    def test_a_missing_choice_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(_multiple_choice(), SubmittedAnswer())

    def test_a_blank_choice_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(_multiple_choice(), SubmittedAnswer(choice="   "))

    def test_choice_matching_is_case_insensitive(self) -> None:
        graded = grade_answer(_multiple_choice(), SubmittedAnswer(choice="goodbye"))

        assert graded.is_correct is True

    def test_a_letter_beyond_the_choices_is_rejected(self) -> None:
        question = _multiple_choice(choices=("a", "b"))

        with pytest.raises(ValidationFailed):
            grade_answer(question, SubmittedAnswer(choice="e"))


class TestTrueFalse:
    def test_the_true_choice_is_the_first_index(self) -> None:
        graded = grade_answer(_true_false(correct_index=0), SubmittedAnswer(choice="True"))

        assert graded.is_correct is True

    def test_the_false_choice(self) -> None:
        graded = grade_answer(_true_false(correct_index=1), SubmittedAnswer(choice="False"))

        assert graded.is_correct is True
        assert graded.correct_answer == {"choice": "False"}

    def test_the_letter_form_works_for_true_false(self) -> None:
        graded = grade_answer(_true_false(correct_index=0), SubmittedAnswer(choice="a"))

        assert graded.is_correct is True


class TestFillBlank:
    def test_a_matching_answer_is_correct(self) -> None:
        graded = grade_answer(_fill_blank(), SubmittedAnswer(text="goes"))

        assert graded.is_correct is True
        assert graded.points_awarded == 3

    def test_the_comparison_ignores_case_and_surrounding_space(self) -> None:
        for typed in ("Goes", "GOES", "  goes  "):
            assert grade_answer(_fill_blank(), SubmittedAnswer(text=typed)).is_correct is True

    def test_a_different_answer_is_wrong(self) -> None:
        graded = grade_answer(_fill_blank(), SubmittedAnswer(text="go"))

        assert graded.is_correct is False
        assert graded.points_awarded == 0
        assert graded.correct_answer == {"text": "goes"}

    def test_a_missing_answer_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(_fill_blank(), SubmittedAnswer())

    def test_any_accepted_variant_passes(self) -> None:
        question = _fill_blank(accepted=("colour", "color"))

        assert grade_answer(question, SubmittedAnswer(text="color")).is_correct is True
        assert grade_answer(question, SubmittedAnswer(text="colour")).is_correct is True

    def test_the_first_accepted_answer_is_the_reported_one(self) -> None:
        question = _fill_blank(accepted=("colour", "color"))

        graded = grade_answer(question, SubmittedAnswer(text="color"))

        assert graded.correct_answer == {"text": "colour"}


class TestAttemptScoring:
    """The halved-points rule: a correct answer is worth full marks only the first time.

    ``points_awarded`` is a function in its own right, so the arithmetic is pinned without a
    question, and ``grade_answer`` is then checked to route through it rather than re-derive
    the rule somewhere the two could drift apart.
    """

    def test_a_first_attempt_is_worth_full_marks(self) -> None:
        assert points_awarded(4, is_correct=True) == 4
        assert points_awarded(4, is_correct=True, attempt=FIRST_ATTEMPT) == 4

    def test_a_correct_retry_is_worth_half(self) -> None:
        assert points_awarded(4, is_correct=True, attempt=2) == 2

    def test_a_wrong_answer_is_worth_nothing_whenever_it_is_given(self) -> None:
        for attempt in (1, 2, 7):
            assert points_awarded(4, is_correct=False, attempt=attempt) == 0

    def test_halving_rounds_down(self) -> None:
        assert points_awarded(3, is_correct=True, attempt=2) == 1

    def test_a_one_point_question_earns_nothing_on_a_retry(self) -> None:
        """The documented consequence of rounding down, asserted so it stays deliberate."""
        assert points_awarded(1, is_correct=True, attempt=2) == 0

    def test_an_attempt_number_below_the_first_is_treated_as_the_first(self) -> None:
        for attempt in (0, -3):
            assert points_awarded(4, is_correct=True, attempt=attempt) == 4

    def test_grading_routes_the_attempt_into_the_score(self) -> None:
        graded = grade_answer(_multiple_choice(), SubmittedAnswer(choice="Goodbye"), attempt=2)

        assert graded.is_correct is True
        assert graded.points_awarded == 1  # half of the question's 2

    def test_a_free_text_retry_is_halved_too(self) -> None:
        graded = grade_answer(_fill_blank(), SubmittedAnswer(text="goes"), attempt=2)

        assert graded.is_correct is True
        assert graded.points_awarded == 1  # 3 // 2

    def test_the_verdict_does_not_change_on_a_retry(self) -> None:
        """Only the score is attempt-aware; being wrong is not something a retry fixes."""
        graded = grade_answer(_multiple_choice(), SubmittedAnswer(choice="Hello"), attempt=2)

        assert graded.is_correct is False
        assert graded.points_awarded == 0

    def test_the_default_attempt_is_the_first(self) -> None:
        graded = grade_answer(_fill_blank(), SubmittedAnswer(text="goes"))

        assert graded.points_awarded == points_awarded(3, is_correct=True)


class TestNoAnswerKeyOnTheQuestion:
    def test_a_gradeable_question_never_carries_a_verdict(self) -> None:
        """The verdict is produced by grading, not stored on the question the client sees.

        ``GradeableQuestion`` is an input to grading, not a wire shape — this asserts the
        separation the whole slice relies on: there is no field a client could read to
        learn the answer.
        """
        question = _multiple_choice()

        for leaked in ("is_correct", "correct_answer", "given_answer"):
            assert not hasattr(question, leaked)
