"""The gamification rules, framework-free and side-effect-free.

Everything here is a pure function of the aggregate's current numbers, so a policy can be
exercised without a database or a request:

* :func:`league_for_xp` — the tier a lifetime XP total has earned.
* :data:`LEAGUE_THRESHOLDS` — the single ordered table the tier is read from, so the
  boundary between two tiers is defined in exactly one place.
* :func:`condition_target` / :func:`condition_metric` / :func:`is_condition_met` — how an
  achievement's declarative ``condition`` is read against a learner's stats.

The evaluation is deliberately generic: adding an achievement is adding a catalogue entry
with a condition dict, not adding a branch here.
"""

from __future__ import annotations

from typing import Any

from app.modules.gamification.domain.entities import LeagueTier

__all__ = [
    "LEAGUE_THRESHOLDS",
    "condition_metric",
    "condition_target",
    "is_condition_met",
    "league_for_xp",
]

#: The XP a learner must reach to *hold* each tier. Ordered from the lowest so the tier is
#: a simple walk from the bottom up. The first entry is always ``0`` so every XP total maps
#: to a tier.
LEAGUE_THRESHOLDS: tuple[tuple[LeagueTier, int], ...] = (
    (LeagueTier.BRONZE, 0),
    (LeagueTier.SILVER, 100),
    (LeagueTier.GOLD, 500),
    (LeagueTier.PLATINUM, 1500),
    (LeagueTier.DIAMOND, 4000),
)


def league_for_xp(xp_total: int) -> LeagueTier:
    """The highest tier whose threshold ``xp_total`` has reached.

    A linear walk, not a binary search: the table has five entries and the branch-free
    loop is easier to read than an index computation that has to be right about an exact
    match.
    """
    tier = LEAGUE_THRESHOLDS[0][0]
    for candidate, threshold in LEAGUE_THRESHOLDS:
        if xp_total >= threshold:
            tier = candidate
        else:
            break
    return tier


def condition_target(condition: dict[str, Any]) -> int:
    """The value an achievement's condition asks the learner to reach.

    An unrecognised condition has no reachable target; ``0`` keeps the caller's arithmetic
    total while :func:`is_condition_met` refuses to fire on it.
    """
    kind = condition.get("type")
    if kind == "xp":
        return int(condition.get("total", 0))
    if kind == "streak":
        return int(condition.get("days", 0))
    if kind == "lessons_completed":
        return int(condition.get("count", 0))
    return 0


def condition_metric(
    condition: dict[str, Any],
    *,
    xp_total: int,
    longest_streak: int,
    lessons_completed: int,
) -> int:
    """The learner's current value on the axis the condition measures.

    Streak conditions read ``longest_streak`` rather than the current run: an achievement
    for "a 7-day streak" is earned once and kept, so a later gap must not take the badge
    away. The other axes are already monotonic.
    """
    kind = condition.get("type")
    if kind == "xp":
        return xp_total
    if kind == "streak":
        return longest_streak
    if kind == "lessons_completed":
        return lessons_completed
    return 0


def is_condition_met(
    condition: dict[str, Any],
    *,
    xp_total: int,
    longest_streak: int,
    lessons_completed: int,
) -> bool:
    """Whether the learner has reached an achievement's target.

    An unrecognised condition type is never met — the safe default, since the alternative
    would hand every learner a badge the moment a typo entered the catalogue.
    """
    kind = condition.get("type")
    if kind not in {"xp", "streak", "lessons_completed"}:
        return False
    target = condition_target(condition)
    return (
        condition_metric(
            condition,
            xp_total=xp_total,
            longest_streak=longest_streak,
            lessons_completed=lessons_completed,
        )
        >= target
    )
