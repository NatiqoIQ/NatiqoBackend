"""The admin overview's read model.

Counts, not lists. Every field is what a ``SELECT count(*)``/``sum(...)`` returns, so the
overview screen can be drawn from a handful of aggregates instead of loading the tables it
summarises — which is the difference between a dashboard that stays fast as the catalogue
grows and one that does not.

Nothing here is a projection of a single row: each figure answers a question the screen
asks directly ("how many accounts?", "how much of the content is still a draft?"), and the
definitions that are not obvious — what counts as *active* — are stated where they are
computed rather than left to a reader of the screen to guess.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["AdminOverview"]


@dataclass(frozen=True, slots=True)
class AdminOverview:
    """Every number on the overview screen, already aggregated.

    ``users_active`` is scoped to the caller's window (days), while the rest are lifetime
    totals. That asymmetry is deliberate: "active" only means something relative to a
    period, and a lifetime "active" would just equal the account count.
    """

    # ------------------------------------------------------------------ accounts
    users_total: int
    users_new: int
    users_active: int
    users_deleted: int

    # ------------------------------------------------------------------ learning
    lessons_completed: int
    sessions_completed: int
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
