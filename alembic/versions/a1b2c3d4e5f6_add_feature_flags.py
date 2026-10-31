"""add feature_flags

A flag is a row so a launch can be widened without a deploy, and so a kill switch can be
pulled while an incident is in progress. The key is the primary key: operators address a
flag by name, and two rows with the same name would be the only real mistake here.

``rollout_percentage`` carries a check constraint rather than trusting the application:
a value outside 0..100 would silently turn a 10% launch into an all-or-nothing one.

Revision ID: a1b2c3d4e5f6
Revises: c3f7b21e9d84
Create Date: 2026-10-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "c3f7b21e9d84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("key", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "rollout_percentage", sa.SmallInteger(), nullable=False, server_default="100"
        ),
        sa.Column("public", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name="fk_feature_flags_updated_by_users",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "rollout_percentage >= 0 AND rollout_percentage <= 100",
            name="ck_feature_flags_rollout_percentage_range",
        ),
    )


def downgrade() -> None:
    op.drop_table("feature_flags")
