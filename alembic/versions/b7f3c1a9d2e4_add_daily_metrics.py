"""add daily_metrics rollup and its day indexes

The dashboard's lifetime figures are sums over append-only event tables. Recomputing one
UTC day from those tables needs a day-range index on ``quiz_answers`` and ``review_logs``;
the AI and session tables already have one. The rollup tables themselves start empty and
``metrics_meta`` starts with no row, so the API keeps using the live queries until an
operator runs ``scripts/backfill_daily_metrics.py``.

Revision ID: b7f3c1a9d2e4
Revises: a1b2c3d4e5f6
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7f3c1a9d2e4"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_metrics",
        sa.Column("day", sa.Date(), primary_key=True, nullable=False),
        sa.Column("sessions_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("xp_awarded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("answers_graded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("correct_answers", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reviews_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reviews_passed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ai_cost_usd",
            sa.Numeric(precision=12, scale=6),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "metrics_meta",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("backfilled_from", sa.Date(), nullable=True),
        sa.Column("backfilled_through", sa.Date(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_quiz_answers_answered_at", "quiz_answers", ["answered_at"])
    op.create_index("ix_review_logs_reviewed_at", "review_logs", ["reviewed_at"])


def downgrade() -> None:
    op.drop_index("ix_review_logs_reviewed_at", table_name="review_logs")
    op.drop_index("ix_quiz_answers_answered_at", table_name="quiz_answers")
    op.drop_table("metrics_meta")
    op.drop_table("daily_metrics")
