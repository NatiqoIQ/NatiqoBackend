"""Which questions a unit's checkpoint asks — and in what order.

A checkpoint is a *diagnostic sample*, not an exhaustive exam: a unit can hold forty
questions across its lessons, and sitting all forty would turn a progression gate into a
chore. This module picks the sample. It is a pure function of the per-question facts it is
handed — no database, no clock, no module imports beyond the stdlib — so the rule can be
tested exhaustively and the service only has to gather facts.

Three decisions worth stating, because each has a plausible alternative that is wrong:

**Every lesson in the unit is represented, always.** The sample's job is to say "do you
understand *this unit*", and a unit question that never appears means a lesson that can go
uncounted — so one question per lesson is a guarantee, and it wins over the size target: a
unit with more lessons than the target gets one question each and comes out *larger* than
requested. The alternative — sampling blindly from the whole pool — would let a random
draw skip a lesson entirely, which is exactly the false pass the checkpoint exists to
prevent.

**The pool is weighted towards what the learner has actually found hard.** A question
missed before, a question that has lapsed in the review schedule, a question whose review
is due now — those are the questions a checkpoint is *for*. A question sitting on an easy,
well-practised review card is down-weighted rather than excluded: the sample still has to
represent the unit, not just its trouble spots. Weights are additive signals with one
multiplicative penalty, chosen so that no single signal can drown out coverage.

**Hard questions are spread through the sample, not front-loaded.** A checkpoint that
opens with five questions in a row the learner has failed reads as "you are failing" and
invites a rage-quit; the same questions alternating with familiar ones read as a test.
Ordering therefore alternates previously-missed questions with the rest, which changes
nothing about the score — every question counts once — but changes what the sitting feels
like.

The caller supplies the randomness (:class:`random.Random`), so tests are deterministic
and production can vary the draw between attempts. Nothing here knows what a session, a
card or a lesson *is* beyond the flat facts it is given; see the service for how those
facts are gathered.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "CHECKPOINT_TARGET",
    "QuestionStat",
    "card_is_easy",
    "select_checkpoint_questions",
    "weight",
]

#: Questions a checkpoint aims for when the unit has at least this many. The brief's
#: range is 10–15 with coverage of every lesson taking priority over the cap, so this is
#: the midpoint of that range rather than a hard limit: fewer available questions means
#: fewer are asked, more lessons means more.
CHECKPOINT_TARGET = 12

#: Signals that add to a question's weight, in the units the brief specified.
_WRONG_BEFORE_BONUS = 3.0
_LAPSED_BONUS = 2.0
_DUE_BONUS = 2.0
_NEVER_ANSWERED_BONUS = 1.0

#: Every question carries this floor before bonuses. It keeps the sample *representative*:
#: a question the learner once got right is still part of the unit and can still be
#: forgotten, so it is deprioritised, never eliminated — and it keeps every weight
#: strictly positive, so the weighted draw has no all-zero pool to divide by.
_BASE_WEIGHT = 1.0

#: A card that is comfortably known — high ease, never lapsed — halves its question's
#: weight. Only the multiplicative signal, because "this is easy" should shrink a
#: question's share of the sample without ever erasing it.
_EASY_CARD_EASE_FLOOR = 2.3


@dataclass(frozen=True, slots=True)
class QuestionStat:
    """Everything selection knows about one question, flattened.

    The facts are gathered elsewhere — answer history from the attempt engine, card state
    from the review schedule — and handed over already reduced, so this module can stay
    ignorant of both. Fields describe *history*, never content: selection does not read a
    question's text, kind or difficulty, because a question the learner has never met is
    equally unknown whatever it looks like.
    """

    question_id: uuid.UUID
    #: Which lesson of the unit the question belongs to — the coverage group.
    lesson_id: uuid.UUID
    #: Catalogue position within its lesson, for deterministic tie-breaks.
    position: int
    #: Ever answered by this learner, in any session.
    answered: bool = False
    #: Missed at least once, even if a later retry got it right (the engine stores the
    #: latest verdict, so "wrong then corrected" shows as ``attempt > 1`` upstream).
    was_wrong: bool = False
    #: SM-2 lapses recorded on the review card, 0 when the question has no card.
    card_lapses: int = 0
    #: The review card is due now or overdue.
    card_due: bool = False
    #: The card is well known: ease at or above the floor and no lapses. The reduction to
    #: a single boolean happens upstream so the threshold lives in one place, but it is
    #: restated here as the field name that actually reads.
    card_easy: bool = False


def card_is_easy(ease_factor: float, lapses: int) -> bool:
    """Whether a review card counts as comfortably known for weighting.

    The public home of the threshold: selection's docstring promises the reduction to one
    boolean happens upstream, but the *number* stays here so the service gathering card
    facts and the weights this module applies can never drift apart. Takes plain numbers
    rather than a card, so this module keeps its promise of knowing nothing about the
    review schedule's shape.
    """
    return ease_factor >= _EASY_CARD_EASE_FLOOR and lapses < 1


def weight(stat: QuestionStat) -> float:
    """One question's share of the sample.

    Additive bonuses for the signals that say "this is where the gaps are", then the easy
    card's multiplicative penalty. Stated as a module-level function rather than inlined
    in the draw because it is the rule itself: the tests that pin the brief's numbers
    (``+3 wrong``, ``+2 lapsed``, ``+2 due``, ``+1 unseen``, ``×0.5 easy``) call this
    directly, and a caller debugging a surprising sample can too.
    """
    value = _BASE_WEIGHT
    if not stat.answered:
        value += _NEVER_ANSWERED_BONUS
    if stat.was_wrong:
        value += _WRONG_BEFORE_BONUS
    if stat.card_lapses >= 1:
        value += _LAPSED_BONUS
    if stat.card_due:
        value += _DUE_BONUS
    if stat.card_easy:
        value *= 0.5
    return value


def _pick_weighted(rng: random.Random, stats: Sequence[QuestionStat]) -> QuestionStat:
    """One draw without replacement, proportional to weight.

    A roulette wheel over the remaining pool: uniform in ``[0, total)`` walked against the
    cumulative weights. Every weight is positive (see ``_BASE_WEIGHT``), so the draw can
    never stall, and no question can be picked twice because the caller removes it.
    """
    total = sum(weight(stat) for stat in stats)
    threshold = rng.uniform(0.0, total)
    running = 0.0
    for stat in stats:
        running += weight(stat)
        if threshold < running:
            return stat
    return stats[-1]


def _coverage_pass(pool: Sequence[QuestionStat]) -> list[QuestionStat]:
    """The guaranteed representative of each lesson: its highest-weight question.

    Deterministic on purpose — same facts, same sample, no matter who asked — so a
    learner's checkpoint cannot change shape between a warm-up read and the real start,
    and ties fall to catalogue position rather than to chance.
    """
    best_by_lesson: dict[uuid.UUID, QuestionStat] = {}
    for stat in pool:
        current = best_by_lesson.get(stat.lesson_id)
        if current is None or weight(stat) > weight(current):
            best_by_lesson[stat.lesson_id] = stat
        elif weight(stat) == weight(current) and stat.position < current.position:
            best_by_lesson[stat.lesson_id] = stat
    return list(best_by_lesson.values())


def _interleave(previously_missed: Sequence[QuestionStat], rest: Sequence[QuestionStat]) -> list[QuestionStat]:
    """Alternate the two groups so misses are spread through the sitting.

    Starts with a missed question: the sample's whole point is the gaps, so they should be
    met early — just not five in a row. The shorter group runs out first and the remainder
    of the longer one appends, which keeps the order deterministic given the inputs.
    """
    ordered: list[QuestionStat] = []
    missed_index = 0
    rest_index = 0
    take_missed = True
    while missed_index < len(previously_missed) or rest_index < len(rest):
        if take_missed and missed_index < len(previously_missed):
            ordered.append(previously_missed[missed_index])
            missed_index += 1
        elif not take_missed and rest_index < len(rest):
            ordered.append(rest[rest_index])
            rest_index += 1
        elif missed_index < len(previously_missed):
            ordered.append(previously_missed[missed_index])
            missed_index += 1
        else:
            ordered.append(rest[rest_index])
            rest_index += 1
        take_missed = not take_missed
    return ordered


def select_checkpoint_questions(
    stats: Sequence[QuestionStat],
    *,
    target: int = CHECKPOINT_TARGET,
    rng: random.Random | None = None,
) -> list[uuid.UUID]:
    """The checkpoint's question list, in sitting order.

    Three stages: guarantee every lesson a question (``_coverage_pass``), fill the rest of
    the sample by weighted draw until ``target`` or the pool runs dry, then order the
    result by ``_interleave``. Ids come back in a list because order is the session's
    frozen contract; callers must not reshuffle it.

    ``target`` below 1 is a programming error rather than an empty exam — an exam with no
    questions would score 100% by definition, and the pass rule must never be satisfiable
    that way.
    """
    if target < 1:
        raise ValueError(f"checkpoint target must be at least 1 question, got {target}")
    if not stats:
        return []

    # Entropy only for the draw's variety — never a secret, so the non-cryptographic
    # generator is exactly right; tests inject a seeded one and get this line skipped.
    rng = rng if rng is not None else random.Random()  # noqa: S311

    coverage = _coverage_pass(pool=stats)
    selected_ids = {stat.question_id for stat in coverage}
    selected = list(coverage)

    if len(selected) < target:
        remaining = [stat for stat in stats if stat.question_id not in selected_ids]
        while remaining and len(selected) < target:
            drawn = _pick_weighted(rng, remaining)
            selected.append(drawn)
            remaining = [stat for stat in remaining if stat.question_id != drawn.question_id]

    previously_missed = [stat for stat in selected if stat.was_wrong]
    rest = [stat for stat in selected if not stat.was_wrong]
    ordered = _interleave(previously_missed, rest)
    return [stat.question_id for stat in ordered]
