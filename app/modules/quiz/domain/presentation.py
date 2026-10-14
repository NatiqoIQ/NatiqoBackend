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

__all__ = ["choice_order", "order_choices", "order_correct_index", "question_seed"]


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


def order_correct_index(order: Sequence[int], correct_index: int | None) -> int:
    """The correct choice's position in the displayed order.

    The inverse of :func:`order_choices`: grading works in display space, so the stored
    index is translated once here rather than teaching the grader about ordering.
    """
    if correct_index is None:  # pragma: no cover - only choice questions call this
        return 0
    return order.index(correct_index)
