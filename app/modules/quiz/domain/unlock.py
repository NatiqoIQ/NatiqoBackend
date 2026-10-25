"""The sequential-unlock rule: which unit a learner may sit, and when it opens.

One rule, stated once, read by two callers — the progress screen (which colours the map)
and the attempt engine (which refuses to start a checkpoint on a unit that is still shut).
Both hand in the same two facts: the published units in curriculum order, and the units
the learner has already passed. Nothing here reads a database or a clock; the facts are
gathered by the services, so the rule can be pinned by unit tests alone.

The rule itself is deliberately linear: **the first published unit is always open, and
every other unit opens only once its immediate predecessor has been passed.** The chain
is global rather than per-level, so the first unit of B1 waits for the last unit of A2 —
which is what "level" means to a learner: a course that runs on, not a set of parallel
tracks. An order missing the requested unit answers ``True``: an unpublished unit is not
anyone's to start, and the callers have already answered a 404 for it by the time they
ask.

``CHECKPOINT_PASSING_SCORE`` lives here rather than in the grading policy because the
80% bar is not a property of a single answer — it is the threshold at which a *unit* is
considered learned, and this module is where that consequence is defined and enforced.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

__all__ = [
    "CHECKPOINT_PASSING_SCORE",
    "lesson_locked",
    "next_unit_id",
    "unit_locked",
]

#: The share of a checkpoint a learner must answer correctly to pass the unit. Fixed
#: rather than configurable: it is part of the product's contract with the learner
#: ("clear 80% to move on") and a deployment that could lower it could also silently
#: re-grade history's meaning.
CHECKPOINT_PASSING_SCORE = 80


def unit_locked(
    unit_id: uuid.UUID,
    ordered_units: Sequence[uuid.UUID],
    passed_units: set[uuid.UUID],
) -> bool:
    """Whether the unit is shut for this learner.

    The first unit in ``ordered_units`` is open unconditionally — a new learner meets a
    locked course on their first screen otherwise. Every later unit requires its
    predecessor in ``passed_units``. A unit absent from the order reads as locked: the
    order is the published catalogue, so an unknown unit is not a unit anyone may start.
    """
    if not ordered_units:
        return True
    try:
        position = ordered_units.index(unit_id)
    except ValueError:
        return True
    if position == 0:
        return False
    return ordered_units[position - 1] not in passed_units


def next_unit_id(unit_id: uuid.UUID, ordered_units: Sequence[uuid.UUID]) -> uuid.UUID | None:
    """The unit that ``unit_id`` unlocks on a pass, or ``None`` when it is the last one.

    Used to hand a client the navigation target after a passing score; a last unit has
    nowhere to go and says so with ``None`` rather than by repeating itself.
    """
    try:
        position = ordered_units.index(unit_id)
    except ValueError:
        return None
    if position + 1 >= len(ordered_units):
        return None
    return ordered_units[position + 1]


def lesson_locked(
    lesson_id: uuid.UUID,
    ordered_lessons_in_unit: Sequence[uuid.UUID],
    completed_lessons: set[uuid.UUID],
    *,
    is_unit_locked: bool,
) -> bool:
    """Whether a lesson is shut for this learner.

    If the unit itself is locked, every lesson inside it is locked.
    Inside an open unit:
    - If the lesson is already completed, it is open (unlocked) for review and re-taking.
    - If it is the first published lesson of the unit, it is open.
    - Otherwise, it opens only once its immediate predecessor lesson in the unit
      has been completed.
    - An unknown lesson absent from the order reads as locked.
    """
    if is_unit_locked:
        return True
    if lesson_id in completed_lessons:
        return False
    if not ordered_lessons_in_unit:
        return True
    try:
        position = ordered_lessons_in_unit.index(lesson_id)
    except ValueError:
        return True
    if position == 0:
        return False
    return ordered_lessons_in_unit[position - 1] not in completed_lessons
