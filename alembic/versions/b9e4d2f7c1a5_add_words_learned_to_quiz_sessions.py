"""add words_learned to quiz_sessions

Revision ID: b9e4d2f7c1a5
Revises: a3d7f1c9e503
Create Date: 2026-10-26

The per-session home of the "learned word" metric: the count of vocabulary questions
the learner answered correctly for the first time ever in that sitting. It backs
``xp.items[].wordsLearned`` in the XP feed, the lifetime ``wordsLearned`` summary and
the daily-challenge word counter, all three read from the same column so they cannot
disagree. Existing rows default to 0 — sessions completed before this metric existed
are not backfilled, and a 0 there means "no first-time words recorded at the time",
not "unavailable".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9e4d2f7c1a5"
down_revision: str | None = "a3d7f1c9e503"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "quiz_sessions",
        sa.Column(
            "words_learned",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("quiz_sessions", "words_learned")
