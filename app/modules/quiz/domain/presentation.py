"""Deterministic choice ordering for quiz sessions.

Each question's options are served in an order derived from a stable seed — the session,
the question and the attempt number — so that three things hold at once:

* **A reload is stable.** Resuming a sitting mid-attempt recomputes the same order, because
  the seed is the same, so a client never sees the options jump under it when it re-fetches.
* **A retry is a fresh order.** After a wrong answer the learner is shown a different
  permutation, so the *position* of the right answer cannot be memorised from the previous
  look. That is the whole feature: the answer key is already withheld, and this stops the
  cheaper leak of memorising where the correct option sat.
* **Grading and presentation agree without shared state.** The order is a pure function of
  the seed, so the server can rebuild exactly the order it showed the client at submit time
  rather than having to store a permutation per question per attempt.

The permutation is produced by sorting the indices by a SHA-256 of ``seed:index``. Doing it
with a hash rather than :mod:`random` is deliberate: a hash is a fixed, documented function,
so the order is stable across processes, across Python versions and across the two machines
that might grade the same submission. ``random`` makes no cross-version guarantee, and an
order that drifted between a client's look and the server's grade would mis-resolve a
letter- or index-based answer.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence

__all__ = [
    "choice_order",
    "deranged_order",
    "order_choices",
    "order_correct_index",
    "ordering_order",
    "question_seed",
]


def question_seed(*, session_id: uuid.UUID, question_id: uuid.UUID, attempt: int) -> str:
    """The seed an order is derived from. One question, one session, one attempt."""
    return f"{session_id}:{question_id}:{attempt}"


def choice_order(size: int, *, seed: str) -> tuple[int, ...]:
    """A permutation of ``range(size)``, deterministic for a given ``seed``.

    The result maps a **display position** to the **original choice index**: position ``p``
    shows ``choices[order[p]]``. Sizes below two are returned as the identity, because there
    is nothing to reorder and a free-text question carries no choices at all.
    """
    if size <= 1:
        return tuple(range(size))
    keyed = [(hashlib.sha256(f"{seed}:{index}".encode()).digest(), index) for index in range(size)]
    keyed.sort(key=lambda item: item[0])
    return tuple(index for _, index in keyed)


def order_choices(choices: Sequence[str], order: Sequence[int]) -> tuple[str, ...]:
    """Apply a display order to a question's choices."""
    if not choices:
        return ()
    return tuple(choices[index] for index in order)


def ordering_order(size: int, *, seed: str) -> tuple[int, ...]:
    """A permutation of ``range(size)`` that is never the identity, for ordering questions.

    An ordering question's key *is* its authored sequence, so a served order equal to that
    sequence would hand the answer over inside the session payload. :func:`choice_order` can
    return the identity for any size, so an ordering question adds one rule on top: when the
    derived order is the identity, rotate it by one. The result is still a deterministic
    permutation — stable across processes — but never the key itself.
    """
    order = choice_order(size, seed=seed)
    if size >= 2 and order == tuple(range(size)):
        return order[1:] + order[:1]
    return order


def deranged_order(size: int, *, forbidden: Sequence[int | None], seed: str) -> tuple[int, ...]:
    """A permutation of ``range(size)`` that never places ``forbidden[j]`` at position ``j``.

    This is the rule a matching question needs and a multiple choice does not. A matching item
    serves two columns — the prompts and the bank of options to pair them with — and a learner
    reading down them pairs whatever sits side by side. So the one arrangement that must never
    be served is the alignment where each option faces the prompt it answers, which is exactly
    the order the question was authored in and therefore the most likely coincidence of
    :func:`choice_order`: a shuffle is not enough here, because a shuffle is *allowed* to come
    out as the identity.

    ``forbidden`` is indexed by display position and may be shorter than ``size`` — a bank
    carries distractors, so it is longer than the list of prompts — and a ``None`` entry marks
    a position with nothing to avoid (no prompt, or a prompt whose option is unknown).

    Repairing a collision swaps the offending entry with another position's. That cannot
    re-align the position it swaps into, because the entries ``forbidden`` names are distinct
    (the content rules forbid a repeated option) and the swap only ever moves the offending
    entry into a position that either has no constraint or is repaired later in the same pass.
    A question whose data broke the distinctness rule keeps the derived order instead of
    raising: presentation must not be the thing that fails a request.
    """
    if size <= 1:
        return tuple(range(size))
    order = list(choice_order(size, seed=seed))
    for position, banned in enumerate(forbidden):
        if position >= size or banned is None or order[position] != banned:
            continue
        # Any position after this one holds something other than ``banned`` — a permutation
        # has no duplicate — so this scan succeeds unless it is the last position.
        for candidate in range(position + 1, size):
            if order[candidate] != banned:
                order[position], order[candidate] = order[candidate], order[position]
                break
        else:
            for candidate in range(position - 1, -1, -1):
                if order[candidate] != banned and forbidden[candidate] != banned:
                    order[position], order[candidate] = order[candidate], order[position]
                    break
    return tuple(order)


def order_correct_index(order: Sequence[int], correct_index: int | None) -> int:
    """The correct choice's position in the displayed order.

    The inverse of :func:`order_choices`: grading works in display space, so the stored
    index is translated once here rather than teaching the grader about ordering.
    """
    if correct_index is None:  # pragma: no cover - only choice questions call this
        return 0
    return order.index(correct_index)
