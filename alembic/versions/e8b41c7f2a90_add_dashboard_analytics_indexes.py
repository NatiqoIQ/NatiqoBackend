"""add the read indexes the dashboard's analytics need

The dashboard reads aggregates, not rows: "how many accounts signed up last week", "what is
the accuracy of this question", "what did this lesson's reviews look like over the month".
Each of those is a range scan or a group-by on a column that has no index of its own today,
so every one of them currently falls back to a full table scan — fine on a seeded database
and progressively unusable on a real one.

The indexes are purely additive: no column changes, no constraint changes, no data touched.
A reader can therefore apply this revision on a live database without a maintenance window,
and ``downgrade`` is a clean reversal because dropping an index destroys no information.

Why each one:

* ``users.created_at`` — sign-up growth and the overview's "new accounts" window.
* ``user_identities.last_login_at`` — the "active in the window" count. The existing
  ``(user_id)`` index cannot answer it: it leads with the identity's owner, not the time.
* ``quiz_sessions.completed_at`` and ``(status, completed_at)`` — the completion series and
  the funnel. ``(user_id, status)`` serves a learner's own resume, not a deployment-wide
  date range.
* ``quiz_answers.question_id`` — question-quality reporting groups answers by question;
  ``(session_id, position)`` is how an attempt is replayed, not how a question is judged.
* ``lesson_progress.lesson_id`` — "hardest lessons" groups by lesson, while the existing
  index leads with ``user_id``.
* ``audit_logs.created_at`` — the audit screen's default view is newest-first unfiltered;
  both existing indexes lead with a filter column (entity, actor).
* ``ai_usage_records.created_at`` — a deployment-wide daily cost series is filtered by
  neither user nor feature, so neither composite index can serve it.
* ``media_assets.created_at`` — the library lists newest first across all owners.

Write cost is the honest trade: nine indexes add work to inserts on tables that are written
on every answer (`quiz_answers`, `quiz_sessions`). They were chosen for columns the product
actually filters and sorts by, and the tables that receive them are append-mostly, so the
cost is a small constant per write rather than a per-row penalty on reads.

Revision ID: e8b41c7f2a90
Revises: d4c9a17b6e02
Create Date: 2026-10-18 12:05:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b41c7f2a90"
down_revision: str | None = "d4c9a17b6e02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``(index name, table, columns)`` in creation order. One list rather than nine pairs of
#: statements so ``downgrade`` cannot drift from ``upgrade`` by a typo in a table name.
_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_users_created_at", "users", ("created_at",)),
    ("ix_identity_last_login", "user_identities", ("last_login_at",)),
    ("ix_quiz_sessions_completed_at", "quiz_sessions", ("completed_at",)),
    ("ix_quiz_sessions_status_completed_at", "quiz_sessions", ("status", "completed_at")),
    ("ix_quiz_answers_question", "quiz_answers", ("question_id",)),
    ("ix_lesson_progress_lesson", "lesson_progress", ("lesson_id",)),
    ("ix_audit_logs_created_at", "audit_logs", ("created_at",)),
    ("ix_ai_usage_created_at", "ai_usage_records", ("created_at",)),
    ("ix_media_assets_created_at", "media_assets", ("created_at",)),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, list(columns), unique=False)


def downgrade() -> None:
    # Reversed so a downgrade unwinds in the opposite order to the upgrade; the order is
    # not load-bearing for independent indexes, but keeping it symmetrical means a partial
    # failure leaves the schema in the same half-state either way.
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
