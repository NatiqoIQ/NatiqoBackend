"""The sequential-unlock rule, pinned as a pure function.

No database and no clock: the rule takes the published order and the passed set as
arguments, so every branch — the head of the chain, a shut middle, an unknown unit, the
end of the course — is a one-line case here rather than a fixture somewhere else.

The pair of views that consume the rule (the progress screen and the attempt engine) are
tested against their own HTTP surface; what is pinned here is the rule they both call, so a
change to the traversal is caught once instead of twice.
"""

from __future__ import annotations

import uuid

from app.modules.quiz.domain.unlock import (
    CHECKPOINT_PASSING_SCORE,
    lesson_locked,
    next_unit_id,
    unit_locked,
)

A = uuid.uuid4()
B = uuid.uuid4()
C = uuid.uuid4()
L1 = uuid.uuid4()
L2 = uuid.uuid4()
L3 = uuid.uuid4()
UNKNOWN = uuid.uuid4()


def test_the_first_unit_is_open_to_a_brand_new_learner() -> None:
    """Nothing passed, three units published: the head of the chain still opens."""
    assert unit_locked(A, [A, B, C], passed_units=set()) is False


def test_a_unit_opens_when_its_immediate_predecessor_is_passed() -> None:
    assert unit_locked(B, [A, B, C], passed_units={A}) is False
    assert unit_locked(C, [A, B, C], passed_units={A, B}) is False


def test_a_unit_stays_shut_while_its_predecessor_is_unpassed() -> None:
    assert unit_locked(B, [A, B, C], passed_units=set()) is True
    assert unit_locked(C, [A, B, C], passed_units={A}) is True


def test_passing_a_more_distant_unit_does_not_open_the_next_one() -> None:
    """Only the *immediate* predecessor counts — the chain is walked in order, not by set."""
    assert unit_locked(C, [A, B, C], passed_units={A}) is True


def test_a_unit_that_is_not_in_the_published_order_reads_as_locked() -> None:
    """An unpublished unit is nobody's to start, so the rule says shut rather than open."""
    assert unit_locked(UNKNOWN, [A, B, C], passed_units={A, B, C}) is True


def test_an_empty_curriculum_locks_everything() -> None:
    assert unit_locked(A, [], passed_units=set()) is True


def test_next_unit_names_the_immediate_successor() -> None:
    assert next_unit_id(A, [A, B, C]) == B
    assert next_unit_id(B, [A, B, C]) == C


def test_the_last_unit_unlocks_nothing() -> None:
    """No wrap-around: the last unit has nowhere to go and says so instead of repeating."""
    assert next_unit_id(C, [A, B, C]) is None


def test_next_unit_of_an_unknown_unit_is_none() -> None:
    assert next_unit_id(UNKNOWN, [A, B, C]) is None


def test_the_passing_score_is_eighty() -> None:
    """Part of the product's contract with the learner; pinned so it cannot drift quietly."""
    assert CHECKPOINT_PASSING_SCORE == 80


def test_a_lesson_in_a_locked_unit_is_always_locked() -> None:
    assert lesson_locked(L1, [L1, L2, L3], completed_lessons=set(), is_unit_locked=True) is True
    assert lesson_locked(L1, [L1, L2, L3], completed_lessons={L1}, is_unit_locked=True) is True


def test_the_first_lesson_in_an_open_unit_is_unlocked() -> None:
    assert lesson_locked(L1, [L1, L2, L3], completed_lessons=set(), is_unit_locked=False) is False


def test_a_subsequent_lesson_is_locked_until_predecessor_is_completed() -> None:
    assert lesson_locked(L2, [L1, L2, L3], completed_lessons=set(), is_unit_locked=False) is True
    assert lesson_locked(L2, [L1, L2, L3], completed_lessons={L1}, is_unit_locked=False) is False
    assert lesson_locked(L3, [L1, L2, L3], completed_lessons={L1}, is_unit_locked=False) is True
    assert (
        lesson_locked(L3, [L1, L2, L3], completed_lessons={L1, L2}, is_unit_locked=False) is False
    )


def test_an_already_completed_lesson_is_always_unlocked_in_an_open_unit() -> None:
    assert lesson_locked(L1, [L1, L2, L3], completed_lessons={L1}, is_unit_locked=False) is False
    assert (
        lesson_locked(L2, [L1, L2, L3], completed_lessons={L1, L2}, is_unit_locked=False) is False
    )


def test_an_unknown_lesson_reads_as_locked() -> None:
    assert (
        lesson_locked(UNKNOWN, [L1, L2, L3], completed_lessons={L1, L2, L3}, is_unit_locked=False)
        is True
    )


def test_an_empty_lesson_list_locks_everything() -> None:
    assert lesson_locked(L1, [], completed_lessons=set(), is_unit_locked=False) is True
