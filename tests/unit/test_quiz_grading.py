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


def _multi_select(
    *,
    choices: tuple[str, ...] = ("Hello", "Good morning", "Goodbye", "Thank you"),
    accepted: tuple[str, ...] = ("hello", "good morning"),
    points: int = 2,
) -> GradeableQuestion:
    return GradeableQuestion(
        kind=QuestionKind.MULTI_SELECT,
        choices=choices,
        accepted_answers=accepted,
        points=points,
        explanation="'Hello' and 'good morning' greet; the others do not.",
    )


def _word_bank(
    *,
    choices: tuple[str, ...] = ("morning", "night", "afternoon", "teacher"),
    accepted: tuple[str, ...] = ("morning",),
    points: int = 2,
) -> GradeableQuestion:
    return GradeableQuestion(
        kind=QuestionKind.WORD_BANK,
        choices=choices,
        accepted_answers=accepted,
        points=points,
        explanation="'Good morning' is the greeting that fits 'how are you?'.",
    )


class TestWordBank:
    """The word bank is free text wearing a tile bank, so it grades like free text.

    ``choices`` here are tiles the learner assembles from, never options to pick — the
    answer arrives as ``text`` and is compared against ``accepted_answers`` exactly as
    ``fill_blank`` is. The bank's only job is to be offered and shuffled; it never
    becomes the key.
    """

    def test_the_assembled_word_is_graded_by_the_accepted_key(self) -> None:
        graded = grade_answer(_word_bank(), SubmittedAnswer(text="morning"))

        assert graded.is_correct is True
        assert graded.points_awarded == 2

    def test_the_comparison_ignores_case_and_surrounding_space(self) -> None:
        for typed in ("Morning", "MORNING", "  morning  "):
            assert grade_answer(_word_bank(), SubmittedAnswer(text=typed)).is_correct is True

    def test_a_distractor_tile_is_wrong(self) -> None:
        graded = grade_answer(_word_bank(), SubmittedAnswer(text="night"))

        assert graded.is_correct is False
        assert graded.points_awarded == 0
        assert graded.correct_answer == {"text": "morning"}

    def test_a_missing_answer_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(_word_bank(), SubmittedAnswer())

    def test_the_reported_key_comes_from_the_accepted_answers(self) -> None:
        """A key with several accepted spellings reports the first, like fill_blank."""
        question = _word_bank(accepted=("color", "colour"))

        graded = grade_answer(question, SubmittedAnswer(text="colour"))

        assert graded.correct_answer == {"text": "color"}


def _ordering(
    *,
    items: tuple[str, ...] = ("Hello!", "How are you?", "I am fine.", "Goodbye!"),
    points: int = 2,
) -> GradeableQuestion:
    return GradeableQuestion(
        kind=QuestionKind.ORDERING,
        choices=items,
        points=points,
        explanation="A greeting opens, asks, answers, then closes.",
    )


class TestOrdering:
    """Ordering is graded element-wise against the authored sequence — all or nothing.

    The items submitted are texts, not indices, and ``question.choices`` is the key in its
    authored order. A single transposition is wrong; there is no partial credit.
    """

    def test_the_authored_order_is_correct(self) -> None:
        graded = grade_answer(
            _ordering(),
            SubmittedAnswer(order=("Hello!", "How are you?", "I am fine.", "Goodbye!")),
        )

        assert graded.is_correct is True
        assert graded.points_awarded == 2

    def test_a_single_swap_is_wrong(self) -> None:
        graded = grade_answer(
            _ordering(),
            SubmittedAnswer(order=("Hello!", "I am fine.", "How are you?", "Goodbye!")),
        )

        assert graded.is_correct is False
        assert graded.points_awarded == 0

    def test_the_comparison_ignores_case_and_surrounding_space(self) -> None:
        graded = grade_answer(
            _ordering(),
            SubmittedAnswer(order=("  hello! ", "HOW ARE YOU?", "i am fine.", "Goodbye!")),
        )

        assert graded.is_correct is True

    def test_the_correct_order_is_reported_with_arrows(self) -> None:
        graded = grade_answer(
            _ordering(),
            SubmittedAnswer(order=("Hello!", "I am fine.", "How are you?", "Goodbye!")),
        )

        assert graded.correct_answer == {"order": "Hello! → How are you? → I am fine. → Goodbye!"}

    def test_a_missing_order_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(_ordering(), SubmittedAnswer())

    def test_an_unknown_item_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(
                _ordering(),
                SubmittedAnswer(order=("Hello!", "How are you?", "I am fine.", "Nonsense!")),
            )

    def test_a_length_mismatch_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(_ordering(), SubmittedAnswer(order=("Hello!", "How are you?")))

    def test_a_correct_retry_is_worth_half(self) -> None:
        graded = grade_answer(
            _ordering(),
            SubmittedAnswer(order=("Hello!", "How are you?", "I am fine.", "Goodbye!")),
            attempt=2,
        )

        assert graded.is_correct is True
        assert graded.points_awarded == 1


class TestMultiSelect:
    def test_the_exact_set_of_correct_options_is_right(self) -> None:
        graded = grade_answer(_multi_select(), SubmittedAnswer(choices=("Hello", "Good morning")))

        assert graded.is_correct is True
        assert graded.points_awarded == 2

    def test_the_order_of_the_selection_does_not_matter(self) -> None:
        graded = grade_answer(_multi_select(), SubmittedAnswer(choices=("Good morning", "Hello")))

        assert graded.is_correct is True

    def test_a_duplicate_selection_still_scores(self) -> None:
        """Set equality, not list equality: ticking the same option twice is the same
        selection, so it cannot turn a right answer wrong."""
        graded = grade_answer(
            _multi_select(),
            SubmittedAnswer(choices=("Hello", "Hello", "Good morning")),
        )

        assert graded.is_correct is True

    def test_a_subset_is_wrong(self) -> None:
        graded = grade_answer(_multi_select(), SubmittedAnswer(choices=("Hello",)))

        assert graded.is_correct is False
        assert graded.points_awarded == 0

    def test_a_superset_is_wrong(self) -> None:
        graded = grade_answer(
            _multi_select(),
            SubmittedAnswer(choices=("Hello", "Good morning", "Goodbye")),
        )

        assert graded.is_correct is False

    def test_selections_resolve_by_index_and_letter_too(self) -> None:
        by_index = grade_answer(_multi_select(), SubmittedAnswer(choices=("0", "1")))
        by_letter = grade_answer(_multi_select(), SubmittedAnswer(choices=("a", "b")))

        assert by_index.is_correct is True
        assert by_letter.is_correct is True

    def test_a_missing_selection_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(_multi_select(), SubmittedAnswer())

    def test_a_selection_that_is_not_a_choice_is_rejected(self) -> None:
        """A client that mis-reads the options is told, rather than the learner marked wrong."""
        with pytest.raises(ValidationFailed):
            grade_answer(_multi_select(), SubmittedAnswer(choices=("Hello", "Nonsense")))

    def test_the_correct_answers_are_reported_in_authored_casing(self) -> None:
        graded = grade_answer(_multi_select(), SubmittedAnswer(choices=("Hello", "Good morning")))

        assert graded.correct_answer == {"choices": "Hello, Good morning"}


def _matching(
    *,
    pairs: tuple[tuple[str, str], ...] = (("1", "one"), ("2", "two"), ("3", "three")),
    bank: tuple[str, ...] = ("one", "two", "three", "four"),
    points: int = 2,
) -> GradeableQuestion:
    """A matching question as grading sees it.

    The key arrives as stored ``"left => right"`` strings and ``choices`` is the bank of
    options to pair against — one more of them than there are prompts, because a distractor is
    the normal case rather than an edge one. The pairs are spelled with the author's capitals
    wherever they have any, because the domain does not fold a matching key at rest.
    """
    return GradeableQuestion(
        kind=QuestionKind.MATCHING,
        choices=bank,
        accepted_answers=tuple(f"{left} => {right}" for left, right in pairs),
        points=points,
        explanation="1 is 'one', 2 is 'two' and 3 is 'three'.",
    )


class TestMatching:
    """Matching is graded on the set of pairs, and only the set.

    The order the learner built the pairs in cannot change the verdict, so a client is free to
    send them in whatever order its screen has them. What *is* a nameable error is a submission
    the payload could not have produced — a prompt the question does not have, an option its
    bank does not offer, an incomplete set — and those are 422s rather than a wrong answer, on
    the same principle the choice and ordering kinds follow: a mis-read client should be told,
    not have a correct learner marked down.
    """

    def test_the_authored_pairs_are_correct(self) -> None:
        graded = grade_answer(
            _matching(), SubmittedAnswer(matching=("1 => one", "2 => two", "3 => three"))
        )

        assert graded.is_correct is True
        assert graded.points_awarded == 2

    def test_the_order_of_the_pairs_does_not_matter(self) -> None:
        graded = grade_answer(
            _matching(), SubmittedAnswer(matching=("3 => three", "1 => one", "2 => two"))
        )

        assert graded.is_correct is True

    def test_the_comparison_ignores_case_and_space_around_the_arrow(self) -> None:
        graded = grade_answer(
            _matching(),
            SubmittedAnswer(matching=("1=>ONE", " 2  =>  two ", "3 => Three")),
        )

        assert graded.is_correct is True

    def test_a_wrong_but_well_formed_pairing_is_wrong_not_refused(self) -> None:
        """The learner paired two real values the wrong way round. That is an answer."""
        graded = grade_answer(
            _matching(), SubmittedAnswer(matching=("1 => two", "2 => one", "3 => three"))
        )

        assert graded.is_correct is False
        assert graded.points_awarded == 0

    def test_a_distractor_from_the_bank_is_wrong(self) -> None:
        graded = grade_answer(
            _matching(), SubmittedAnswer(matching=("1 => one", "2 => two", "3 => four"))
        )

        assert graded.is_correct is False

    def test_a_partial_set_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(_matching(), SubmittedAnswer(matching=("1 => one", "2 => two")))

    def test_a_prompt_answered_twice_is_rejected(self) -> None:
        """One option per prompt: pairing 1 twice leaves 2 unpaired."""
        with pytest.raises(ValidationFailed):
            grade_answer(
                _matching(),
                SubmittedAnswer(matching=("1 => one", "1 => two", "3 => three")),
            )

    def test_a_pair_sent_twice_still_scores(self) -> None:
        """Set equality, not list equality — the same ruling a repeated tick gets."""
        graded = grade_answer(
            _matching(),
            SubmittedAnswer(matching=("1 => one", "1 => one", "2 => two", "3 => three")),
        )

        assert graded.is_correct is True

    def test_a_missing_answer_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(_matching(), SubmittedAnswer())

    def test_a_prompt_that_is_not_in_the_question_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(
                _matching(), SubmittedAnswer(matching=("1 => one", "2 => two", "4 => four"))
            )

    def test_an_option_the_bank_does_not_offer_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(
                _matching(), SubmittedAnswer(matching=("1 => one", "2 => two", "3 => threeish"))
            )

    def test_an_entry_with_no_arrow_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            grade_answer(_matching(), SubmittedAnswer(matching=("1 => one", "2 => two", "three")))

    def test_the_reported_key_keeps_the_authored_spelling(self) -> None:
        graded = grade_answer(
            _matching(pairs=(("Monday", "1"), ("Tuesday", "2")), bank=("1", "2", "3")),
            SubmittedAnswer(matching=("monday => 3", "tuesday => 2")),
        )

        assert graded.correct_answer == {"pairs": "Monday \u21d2 1; Tuesday \u21d2 2"}

    def test_a_correct_retry_is_worth_half(self) -> None:
        graded = grade_answer(
            _matching(),
            SubmittedAnswer(matching=("1 => one", "2 => two", "3 => three")),
            attempt=2,
        )

        assert graded.is_correct is True
        assert graded.points_awarded == 1

    def test_a_key_with_no_bank_cannot_be_graded(self) -> None:
        """Unreachable through the aggregate, which is exactly why it is pinned here."""
        with pytest.raises(ValidationFailed) as raised:
            grade_answer(
                _matching(bank=()), SubmittedAnswer(matching=("1 => one", "2 => two", "3 => three"))
            )

        assert raised.value.code is ErrorCode.QUESTION_TYPE_UNSUPPORTED


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


class TestEveryKindIsGradeable:
    """No declared kind may reach grading without a rule.

    ``QuestionKind`` is the list of kinds the engine promises to score, so enumerating the
    enum is the one check that keeps that promise honest: a kind added to the list but not to
    :func:`grade_answer` would otherwise silently score every learner wrong. Each entry here
    is a valid question-and-answer pair for its kind.
    """

    def test_every_declared_kind_has_a_grading_rule(self) -> None:
        submissions: dict[QuestionKind, tuple[GradeableQuestion, SubmittedAnswer]] = {
            QuestionKind.MULTIPLE_CHOICE: (
                _multiple_choice(),
                SubmittedAnswer(choice="Goodbye"),
            ),
            QuestionKind.MULTI_SELECT: (
                _multi_select(),
                SubmittedAnswer(choices=("Hello", "Good morning")),
            ),
            QuestionKind.TRUE_FALSE: (_true_false(), SubmittedAnswer(choice="True")),
            QuestionKind.FILL_BLANK: (_fill_blank(), SubmittedAnswer(text="goes")),
            QuestionKind.WORD_BANK: (_word_bank(), SubmittedAnswer(text="morning")),
            QuestionKind.ORDERING: (
                _ordering(),
                SubmittedAnswer(order=("Hello!", "How are you?", "I am fine.", "Goodbye!")),
            ),
            QuestionKind.MATCHING: (
                _matching(),
                SubmittedAnswer(matching=("1 => one", "2 => two", "3 => three")),
            ),
        }

        assert set(submissions) == set(QuestionKind)

        for kind, (question, answer) in submissions.items():
            try:
                grade_answer(question, answer)
            except ValidationFailed as exc:  # pragma: no cover - the failure under test
                assert exc.code is not ErrorCode.QUESTION_TYPE_UNSUPPORTED, (
                    f"{kind.value} is declared but has no grading rule."
                )
