"""The admin overview's read model.

Counts, not lists. Every field is what a ``SELECT count(*)``/``sum(...)`` returns, so the
overview screen can be drawn from a handful of aggregates instead of loading the tables it
summarises — which is the difference between a dashboard that stays fast as the catalogue
grows and one that does not.

Nothing here is a projection of a single row: each figure answers a question the screen
asks directly ("how many accounts?", "how much of the content is still a draft?"), and the
definitions that are not obvious — what counts as *active*, what counts as a passed review —
are stated where they are computed rather than left to a reader of the screen to guess.

The screen grew beyond the original three blocks, and the shape follows it: stock
(``questions``), learner quality (``performance``), reward state (``gamification``),
operational spend (``ai``) and a per-day ``activity`` series for the chart. The series is
the one part that is not a single row: it carries at most one grouped row per day in the
window, so it grows with the window the caller asked for and with nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

__all__ = [
    "AdminOverview",
    "AiTotals",
    "DailyActivity",
    "GamificationTotals",
    "PerformanceTotals",
    "QuestionCounts",
]


@dataclass(frozen=True, slots=True)
class QuestionCounts:
    """The question stock, by where it sits and how it is shaped.

    ``published`` counts questions whose lesson is published — a question has no editorial
    state of its own, and its visibility is its lesson's. The two maps are zero-filled over
    the enum values in the adapter so a chart can render every kind and skill even when
    nobody has authored one yet, instead of treating a missing key as "unknown category".
    """

    total: int
    published: int
    by_kind: dict[str, int]
    by_skill: dict[str, int]


@dataclass(frozen=True, slots=True)
class PerformanceTotals:
    """Learner grades as sums and counts — the averages are derived in the application.

    ``score_total``/``rated_lessons`` are over *completed* lesson progress only: a lesson
    the learner walked away from mid-way would drag an average down for not being finished,
    which is not the same thing as being badly done.

    ``reviews_passed`` counts SM-2 grades at or above the passing quality, so
    ``reviews_passed / reviews_total`` is the retention figure the screen shows.
    """

    rated_lessons: int
    score_total: int
    reviews_total: int
    reviews_passed: int
    quality_total: int


@dataclass(frozen=True, slots=True)
class GamificationTotals:
    """Reward state across the learner base.

    ``active_streaks`` counts learners whose current streak is above zero *today*, not
    learners who ever had one — the streak is a live figure and a lapsed streak has stopped
    being one.
    """

    xp_total: int
    active_streaks: int
    longest_streak: int
    achievements_unlocked: int


@dataclass(frozen=True, slots=True)
class AiTotals:
    """Lifetime metering spend, so the home page can show cost without a second request.

    ``errors`` is every record not marked ``ok``: the metering recorder writes one of two
    statuses, so "not ok" is the honest count of calls that failed a learner.
    """

    requests: int
    errors: int
    tokens: int
    cost_usd: float
    conversations: int


@dataclass(frozen=True, slots=True)
class DailyActivity:
    """One calendar day's activity, in UTC — the day the row's timestamp falls on.

    Returned sparse by the adapter (days nobody did anything are absent); the use case
    fills the gaps so the chart's x-axis is continuous and a quiet day reads as zero
    rather than as missing data.
    """

    day: date
    new_users: int
    sessions_completed: int
    xp_awarded: int
    ai_requests: int


@dataclass(frozen=True, slots=True)
class AdminOverview:
    """Every number on the overview screen, already aggregated.

    ``users_active`` is scoped to the caller's window (days), while the rest are lifetime
    totals. That asymmetry is deliberate: "active" only means something relative to a
    period, and a lifetime "active" would just equal the account count. The activity
    series is windowed too, and its days sum back to the lifetime-scoped counters that
    are windowed as well (``users_new``, completed sessions and their XP inside the
    window) — a chart whose columns did not add up to the card beside it would be worse
    than no chart.
    """

    # ------------------------------------------------------------------ accounts
    users_total: int
    users_new: int
    users_active: int
    users_deleted: int

    # ------------------------------------------------------------------ learning
    lessons_completed: int
    sessions_completed: int
    sessions_active: int
    answers_graded: int
    correct_answers: int
    xp_awarded: int

    # ------------------------------------------------------------------- content
    levels: int
    units: int
    lessons: int
    published_lessons: int
    drafts: int
    in_review: int

    # ----------------------------------------------------------------- questions
    questions: QuestionCounts

    # -------------------------------------------------------- learner performance
    performance: PerformanceTotals

    # -------------------------------------------------------------- gamification
    gamification: GamificationTotals

    # ------------------------------------------------------------------- ai spend
    ai: AiTotals

    # ------------------------------------------------------------- chart series
    activity: tuple[DailyActivity, ...]
