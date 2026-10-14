"""Answer grading: the one rule that decides right from wrong, and what it is worth.

The engine grades on the server, always. The client is handed a question **without** its
answer key and posts back a bare answer; this module compares it and returns a verdict plus
the explanation. Nothing here touches the database or the framework, so the rule can be
tested exhaustively on its own — and it is the ruling the whole slice exists to make
authoritative.

Three decisions worth stating, because each has a plausible alternative that is wrong:

**Free text is compared case- and whitespace-insensitively.** A learner typing `` Go``,
``goes`` or ``GOES`` is demonstrating the same understanding, and marking ``GOES`` wrong
would be grading typing, not knowledge. The accepted-answer keys are folded to the same
form when the question aggregate is built (``content.domain.policy``), so this comparison
is a plain membership test rather than a re-normalisation on every submission.

**A multiple-choice answer is resolved generously but deterministically.** Clients differ
on what they send — the choice's text, its zero-based index, or its letter — so all three
are accepted, tried in that order. The *verdict* is always computed against the stored
index, and the response always reports the answer back as text, so the wire contract has
one canonical shape even though the input has three. An answer that matches nothing is a
422, not a silent "wrong": a client that mis-reads the choices should be told, not have a
correct learner marked down.

**A correct answer is worth full marks only on the first attempt.** The caller says which
attempt this is; a correct answer to a question that was missed once earns half. The rule
is about rewarding knowing the answer rather than eventually guessing it, so it is stated
once here and applied by :func:`points_awarded` rather than being re-derived wherever a
score is displayed. The module counts nothing itself — the attempt is an input, so the same
function serves a retry, a review pass, and a test that wants to prove the arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.errors import ErrorCode, ValidationFailed
from app.core.types import QuestionKind

__all__ = [
    "FIRST_ATTEMPT",
    "GradeableQuestion",
    "GradedAnswer",
    "SubmittedAnswer",
    "grade_answer",
    "points_awarded",
]

#: The attempt number that earns full marks. Every later attempt is a retry.
FIRST_ATTEMPT = 1

#: The letters a client may use instead of a choice's text or index: ``a`` is the first
#: choice. Capped at the six choices a multiple-choice question is allowed to offer.
_CHOICE_LETTERS = "abcdef"


@dataclass(frozen=True, slots=True)
class GradeableQuestion:
    """Everything grading needs from a question, and nothing else.

    A narrow input rather than the content module's ``Question`` aggregate on purpose:
    it keeps this module free of a cross-module domain import and makes it obvious that
    the answer key never has to leave the server to be compared.
    """

    kind: QuestionKind
    choices: tuple[str, ...] = ()
    correct_index: int | None = None
    accepted_answers: tuple[str, ...] = ()
    points: int = 1
    explanation: str = ""


@dataclass(frozen=True, slots=True)
class SubmittedAnswer:
    """What the learner sent. Exactly one of the two is meaningful, per question kind."""

    choice: str | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class GradedAnswer:
    """The verdict, plus what to show the learner now that they have committed."""

    is_correct: bool
    points_awarded: int
    #: The answer key, in the same shape the client sent. Only ever produced *after* an
    #: answer has been submitted; it is the whole point that it is not sent before.
    correct_answer: dict[str, str]
    explanation: str


def points_awarded(question_points: int, *, is_correct: bool, attempt: int = FIRST_ATTEMPT) -> int:
    """The one place the scoring rule is written.

    A wrong answer is worth nothing, whatever the attempt. A correct answer is worth the
    question's full value on the first attempt and half of it afterwards.

    ``//`` rather than a float: points are whole on the wire, and an integer division makes
    the rounding explicit and deterministic instead of leaving it to whichever rounding a
    client happens to implement. Rounding *down* is the deliberate half of that choice. It
    means a one-point question earns nothing on a retry, which is the rule working rather
    than an off-by-one: if a learner's only route to points were to keep trying, the
    "first try" incentive the rule exists to create would be gone. Authors who want a retry
    to still be worth something should price the question above one point.

    An ``attempt`` below :data:`FIRST_ATTEMPT` is treated as the first, so a caller that has
    not thought about attempt numbering yet cannot accidentally halve a fresh answer.
    """
    if not is_correct:
        return 0
    if attempt <= FIRST_ATTEMPT:
        return question_points
    return question_points // 2


def _resolve_choice_index(question: GradeableQuestion, raw: str) -> int | None:
    """Map whatever the client sent onto a choice index, or ``None`` if it matches none.

    Order matters and is documented on the module: exact choice text first (so a question
    whose options are literally ``"a"``/``"b"`` still answers by value), then a zero-based
    index, then a single letter. The first interpretation that lands on a real choice wins;
    the others are never consulted, so the result does not depend on how the client phrased
    it beyond which form it chose.
    """
    value = raw.strip()
    if not value:
        return None
    folded = value.lower()

    for index, choice in enumerate(question.choices):
        if choice.strip().lower() == folded:
            return index

    if value.isdigit():
        index = int(value)
        if 0 <= index < len(question.choices):
            return index

    if len(folded) == 1 and folded in _CHOICE_LETTERS:
        index = _CHOICE_LETTERS.index(folded)
        if 0 <= index < len(question.choices):
            return index

    return None


def _grade_choice(
    question: GradeableQuestion, answer: SubmittedAnswer, *, attempt: int
) -> GradedAnswer:
    if answer.choice is None or not answer.choice.strip():
        raise ValidationFailed(
            "This question is answered by choosing an option.",
            code=ErrorCode.VALIDATION_ERROR,
            details={"field": "answer.choice"},
        )
    index = _resolve_choice_index(question, answer.choice)
    if index is None:
        raise ValidationFailed(
            "That is not one of this question's choices.",
            code=ErrorCode.VALIDATION_ERROR,
            details={"field": "answer.choice"},
        )
    if question.correct_index is None:  # pragma: no cover - the aggregate forbids it
        raise ValidationFailed(
            "This question has no answer key and cannot be graded.",
            code=ErrorCode.QUESTION_TYPE_UNSUPPORTED,
        )
    is_correct = index == question.correct_index
    return GradedAnswer(
        is_correct=is_correct,
        points_awarded=points_awarded(question.points, is_correct=is_correct, attempt=attempt),
        correct_answer={"choice": question.choices[question.correct_index]},
        explanation=question.explanation,
    )


def _grade_free_text(
    question: GradeableQuestion, answer: SubmittedAnswer, *, attempt: int
) -> GradedAnswer:
    if answer.text is None or not answer.text.strip():
        raise ValidationFailed(
            "This question is answered by typing.",
            code=ErrorCode.VALIDATION_ERROR,
            details={"field": "answer.text"},
        )
    is_correct = answer.text.strip().lower() in question.accepted_answers
    correct_answer: dict[str, str] = (
        {"text": question.accepted_answers[0]} if question.accepted_answers else {}
    )
    return GradedAnswer(
        is_correct=is_correct,
        points_awarded=points_awarded(question.points, is_correct=is_correct, attempt=attempt),
        correct_answer=correct_answer,
        explanation=question.explanation,
    )


def grade_answer(
    question: GradeableQuestion,
    answer: SubmittedAnswer,
    *,
    attempt: int = FIRST_ATTEMPT,
) -> GradedAnswer:
    """Mark one answer against one question.

    ``attempt`` is which try this is, counting from :data:`FIRST_ATTEMPT`; it only decides
    whether a correct answer is worth full or half marks. See :func:`points_awarded`.

    Raises :class:`~app.core.errors.ValidationFailed` when the submission does not match
    the question's kind, and carries ``QUESTION_TYPE_UNSUPPORTED`` for a kind with no
    grading rule — which the schema forbids today but a future question type could reach.
    """
    if question.kind in (QuestionKind.MULTIPLE_CHOICE, QuestionKind.TRUE_FALSE):
        return _grade_choice(question, answer, attempt=attempt)
    if question.kind is QuestionKind.FILL_BLANK:
        return _grade_free_text(question, answer, attempt=attempt)
    raise ValidationFailed(
        f"Questions of kind {question.kind!r} cannot be graded yet.",
        code=ErrorCode.QUESTION_TYPE_UNSUPPORTED,
    )
