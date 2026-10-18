"""add video aid to questions

Revision ID: f5d27c9a3e41
Revises: e8b41c7f2a90
Create Date: 2026-10-18 14:10:00.000000+00:00

A question can now carry an optional ``video_url`` — a short clip that shows the word or
sentence being taught in use. It is the video counterpart of ``image_url`` and
``audio_url``: nullable because every question authored before this revision has no video,
and the aid is optional by product rule; questions are still required to carry their
textual explanation (see ``domain/policy.py``).

Batch mode rather than a plain ``ALTER``: the test suite runs the real migration path
against SQLite, whose ``ADD COLUMN`` support is limited, while batch mode still emits the
native statement on PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5d27c9a3e41"
down_revision: str | None = "e8b41c7f2a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("video_url", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.drop_column("video_url")
