"""add image aid to questions

Revision ID: 9c1f4a7b2e60
Revises: 7a4d2f9c1b58
Create Date: 2026-10-09 12:00:00.000000+00:00

A question can now carry an optional ``image_url`` — a visual aid that illustrates the
word or sentence being taught. It is nullable because every question authored before this
revision has no image, and the aid is optional by product rule; questions are still
required to carry their textual explanation (see ``domain/policy.py``).

Batch mode rather than a plain ``ALTER``: the test suite runs the real migration path
against SQLite, whose ``ADD COLUMN`` support is limited, while batch mode still emits the
native statement on PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c1f4a7b2e60"
down_revision: str | None = "7a4d2f9c1b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("image_url", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.drop_column("image_url")
