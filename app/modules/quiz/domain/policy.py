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
correct learner marked down. A multi-select answer resolves each selection the same way and
then compares the whole selection as a set, so the order the options were ticked in never
changes the verdict.

**A matching answer is a set of pairs, and the set is the answer.** The order a learner built
it in cannot change the verdict, and neither can a pair that names a real prompt and a real
option but the wrong ones — that is a wrong answer, not a malformed one. Only a submission
that could not have been built from the payload the client was sent (a pair with no arrow, a
prompt the question does not have, an option its bank does not offer, a prompt answered twice
or not at all) is refused.

**A correct answer is worth full marks only on the first attempt.** The caller says which
attempt this is; a correct answer to a question that was missed once earns half. The rule
is about rewarding knowing the answer rather than eventually guessing it, so it is stated
once here and applied by :func:`points_awarded` rather than being re-derived wherever a
score is displayed. The module counts nothing itself — the attempt is an input, so the same
function serves a retry, a review pass, and a test that wants to prove the arithmetic.
"""

from __future__ import annotations

from collections.abc import Sequence
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

#: The arrow a matching pair is written with, as in ``"dog => kelb"``. It mirrors the content
#: module's ``MATCH_ARROW``, and it is restated rather than imported because this module takes
#: a narrow ``GradeableQuestion`` precisely so that grading depends on no other module's
#: domain. The two spellings are compared by the grading tests, which post a pair the seeder
#: wrote and assert the verdict.
_MATCH_ARROW = "=>"


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
    """What the learner sent. One field is meaningful per question kind.

    ``choice`` and ``text`` answer a single-option and a free-text question; ``choices``
    answers a multi-select one; ``order`` answers an ordering one; ``matching`` answers a
    matching one, as the ``"left => right"`` pairs the learner built. They are separate fields
    rather than one polymorphic value so that a mis-shaped submission (a list sent where a
    single option was expected) is a validation error the service can name, not a silent
    wrong.
    """

    choice: str | None = None
    text: str | None = None
    choices: tuple[str, ...] = ()
    order: tuple[str, ...] = ()
    matching: tuple[str, ...] = ()


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


def _grade_multi_select(
    question: GradeableQuestion, answer: SubmittedAnswer, *, attempt: int
) -> GradedAnswer:
    """Mark a multi-select answer against the set of correct options.

    The verdict is set equality, so the order the learner ticked the options in — and a
    duplicate of an option already given — cannot change it. Each submitted entry is
    resolved the same generous way a single choice is (text, index or letter), so a client
    that reuses its single-choice encoding still works; an entry that resolves to nothing is
    a 422 rather than a silently incorrect answer.
    """
    if not answer.choices:
        raise ValidationFailed(
            "This question is answered by choosing one or more options.",
            code=ErrorCode.VALIDATION_ERROR,
            details={"field": "answer.choices"},
        )
    submitted: set[str] = set()
    for raw in answer.choices:
        index = _resolve_choice_index(question, raw)
        if index is None:
            raise ValidationFailed(
                "One or more selections are not choices of this question.",
                code=ErrorCode.VALIDATION_ERROR,
                details={"field": "answer.choices", "value": raw},
            )
        submitted.add(question.choices[index].strip().lower())

    key = set(question.accepted_answers)
    is_correct = submitted == key
    # Report the key in the authors' casing and in the order the options are given to
    # grading (the served order), so the field reads the way the options were offered rather
    # than the way they were normalised.
    correct_answer: dict[str, str] = (
        {"choices": ", ".join(c for c in question.choices if c.strip().lower() in key)}
        if key
        else {}
    )
    return GradedAnswer(
        is_correct=is_correct,
        points_awarded=points_awarded(question.points, is_correct=is_correct, attempt=attempt),
        correct_answer=correct_answer,
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


def _grade_ordering(
    question: GradeableQuestion, answer: SubmittedAnswer, *, attempt: int
) -> GradedAnswer:
    """Mark an ordering answer against the authored sequence.

    The key is the order the items were authored in — ``question.choices`` as grading sees
    it — and the learner submits the same items as text in the order they arranged them.
    Grading is element-wise after the same case/whitespace fold free text uses, and
    **all-or-nothing**: a single item out of place is wrong. Partial credit for a nearly
    right sequence is deliberately out of scope, because "half in order" is not a verdict a
    language exercise can defend.

    Texts, not indices, are submitted on purpose: an index would refer to the shuffled array
    the learner was shown, which changes on a retry, so a text is the only stable thing to
    compare.
    """
    if not answer.order:
        raise ValidationFailed(
            "This question is answered by putting the items in order.",
            code=ErrorCode.VALIDATION_ERROR,
            details={"field": "answer.order"},
        )
    key = question.choices
    submitted = tuple(item.strip().lower() for item in answer.order)
    known = {item.strip().lower() for item in key}
    for item in submitted:
        if not item or item not in known:
            raise ValidationFailed(
                "One or more items are not part of this question.",
                code=ErrorCode.VALIDATION_ERROR,
                details={"field": "answer.order"},
            )
    if len(submitted) != len(key):
        raise ValidationFailed(
            "Send every item exactly once, in the order you choose.",
            code=ErrorCode.VALIDATION_ERROR,
            details={"field": "answer.order"},
        )
    is_correct = submitted == tuple(item.strip().lower() for item in key)
    return GradedAnswer(
        is_correct=is_correct,
        points_awarded=points_awarded(question.points, is_correct=is_correct, attempt=attempt),
        correct_answer={"order": " → ".join(key)} if key else {},
        explanation=question.explanation,
    )


def _split_pair(raw: str) -> tuple[str, str] | None:
    """One matching pair, split into its folded halves, or ``None`` if it is not a pair.

    Only the first arrow splits, so a right-hand value that itself contains ``=>`` survives.
    Both halves are folded *here* rather than at storage, and that is not an optimisation left
    undone: storage keeps a matching key in the spelling it was authored in, because those
    halves are also the prompts and options a learner reads, so grading has to fold as it
    compares. Folding here is also what tolerates the whitespace a client put around the
    arrow — ``"dog=>kelb"`` and ``" Dog => Kelb "`` are the same answer.
    """
    left, arrow, right = raw.partition(_MATCH_ARROW)
    if not arrow:
        return None
    left, right = left.strip().lower(), right.strip().lower()
    if not left or not right:
        return None
    return left, right


def _offered_text(choices: Sequence[str], folded: str) -> str:
    """The authored spelling of an option, so feedback is not shown in lower case."""
    for choice in choices:
        if choice.strip().lower() == folded:
            return choice
    return folded  # pragma: no cover - the content rules make every answer an option


def _grade_matching(
    question: GradeableQuestion, answer: SubmittedAnswer, *, attempt: int
) -> GradedAnswer:
    """Mark a matching answer against the authored pairs.

    The verdict is set equality over pairs, so the order the learner built them in cannot
    change it — which is the whole reason this kind exists as its own type rather than as a
    list of separate questions.

    What is a 422 and what is simply wrong is the one line worth stating. A submission that
    is not pairs at all, names a prompt this question does not have, names an option its bank
    does not offer, or leaves a prompt unanswered is a **422**: those are shapes a correct
    learner could be marked down by, or answers the client could not have built from the payload
    it was sent. A complete, well-formed set of pairs that simply disagrees with the key —
    including one pairing two real values the wrong way round — is **wrong**, and earns nothing.
    """
    if not answer.matching:
        raise ValidationFailed(
            "This question is answered by matching each item to an option.",
            code=ErrorCode.VALIDATION_ERROR,
            details={"field": "answer.matching"},
        )

    key: set[tuple[str, str]] = set()
    #: The key as it should be *shown*, keyed by the folded pair it compares as. The stored
    #: spelling and the compared one differ on purpose (see ``_split_pair``), so the feedback
    #: is rebuilt from what was stored rather than from what was folded.
    displayed: dict[tuple[str, str], str] = {}
    for stored in question.accepted_answers:
        pair = _split_pair(stored)
        if pair is None:  # pragma: no cover - the aggregate rejects an unparseable pair
            raise ValidationFailed(
                "This question has no answer key and cannot be graded.",
                code=ErrorCode.QUESTION_TYPE_UNSUPPORTED,
            )
        key.add(pair)
        left_text, _, right_text = stored.partition(_MATCH_ARROW)
        displayed[pair] = (
            f"{left_text.strip()} \u21d2 "
            f"{_offered_text(question.choices, right_text.strip().lower())}"
        )
    if not key or not question.choices:
        raise ValidationFailed(
            "This question has no answer key and cannot be graded.",
            code=ErrorCode.QUESTION_TYPE_UNSUPPORTED,
        )
    prompts = {left for left, _ in key}
    offered = {choice.strip().lower() for choice in question.choices}

    submitted: set[tuple[str, str]] = set()
    for raw in answer.matching:
        pair = _split_pair(raw)
        if pair is None:
            raise ValidationFailed(
                "Send every match as 'left => right'.",
                code=ErrorCode.VALIDATION_ERROR,
                details={"field": "answer.matching", "value": raw},
            )
        left, right = pair
        if left not in prompts:
            raise ValidationFailed(
                "One or more items are not part of this question.",
                code=ErrorCode.VALIDATION_ERROR,
                details={"field": "answer.matching", "value": raw},
            )
        if right not in offered:
            raise ValidationFailed(
                "One or more matches are not among this question's choices.",
                code=ErrorCode.VALIDATION_ERROR,
                details={"field": "answer.matching", "value": raw},
            )
        submitted.add(pair)

    # Every prompt exactly once. Fewer is an unfinished screen; two pairs for one prompt leave
    # another prompt unanswered, because an option can only be placed once — so both are the
    # same rule, stated over the prompts rather than over the number of pairs. A pair repeated
    # character for character is not an error at all: it collapses into the set, which is the
    # same ruling a repeated tick gets in a multi-select.
    if {left for left, _ in submitted} != prompts:
        raise ValidationFailed(
            "Match every item exactly once.",
            code=ErrorCode.VALIDATION_ERROR,
            details={"field": "answer.matching"},
        )

    is_correct = submitted == key
    # Reported in a stable order rather than the order the pairs happen to be stored in: the
    # verdict is order-free, so a client rebuilding the answer from this field should not see
    # it rearrange itself between two identical requests.
    reported = [displayed[pair] for pair in sorted(key)]
    return GradedAnswer(
        is_correct=is_correct,
        points_awarded=points_awarded(question.points, is_correct=is_correct, attempt=attempt),
        correct_answer={"pairs": "; ".join(reported)} if reported else {},
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
    if question.kind is QuestionKind.MULTI_SELECT:
        return _grade_multi_select(question, answer, attempt=attempt)
    if question.kind in (QuestionKind.FILL_BLANK, QuestionKind.WORD_BANK):
        return _grade_free_text(question, answer, attempt=attempt)
    if question.kind is QuestionKind.ORDERING:
        return _grade_ordering(question, answer, attempt=attempt)
    if question.kind is QuestionKind.MATCHING:
        return _grade_matching(question, answer, attempt=attempt)
    raise ValidationFailed(
        f"Questions of kind {question.kind!r} cannot be graded yet.",
        code=ErrorCode.QUESTION_TYPE_UNSUPPORTED,
    )
