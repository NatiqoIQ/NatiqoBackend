"""add users.deleted_at for account closure

Revision ID: c4a91f2e7b08
Revises: b7e21c4a9d13
Create Date: 2026-10-03 02:30:00.000000+00:00

A closed account is a status change, not a row removal. Quiz attempts, SRS cards and
streak days all point at the user, and a hard delete would either cascade a learner's
history away or leave it dangling; so the row stays and ``users.status`` becomes
``'deleted'``, which already fails the authenticatable check on every credential path.

``deleted_at`` is added alongside the status rather than inferred from it on purpose.
"when did this account end" is a question the GDPR export and the retention sweep both
need answered, and ``updated_at`` cannot carry it because every later write overwrites it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4a91f2e7b08"
down_revision: str | None = "b7e21c4a9d13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    # Indexed because the retention sweep asks "which closed accounts are older than the
    # retention window" on every run, and that is a range scan without it.
    op.create_index("ix_users_deleted_at", "users", ["deleted_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_users_deleted_at", table_name="users")
    op.drop_column("users", "deleted_at")