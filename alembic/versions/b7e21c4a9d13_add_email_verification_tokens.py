"""add email_verification_tokens for address confirmation

Revision ID: b7e21c4a9d13
Revises: 2483ffa8f254
Create Date: 2026-10-03 01:40:00.000000+00:00

Deliberately *not* folded into the password-reset table. The two look alike and are not
the same thing: a verification link is worth days because the learner may not open their
mail for a while, a reset link is worth an hour because the alternative is being locked
out, and "revoke outstanding resets" must never silently cancel a verification (or the
reverse). ``users.email_verified_at`` already exists from the initial migration, so this
revision adds only the token store.

Login is *not* gated on verification. An unverified learner keeps full access and the
client decides what to nudge about, because gating it would lock out every account that
registered before this feature existed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e21c4a9d13"
down_revision: str | None = "2483ffa8f254"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_verification_tokens",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", sa.String(length=64), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_verification_tokens_users_id_email_verification_tokens"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_verification_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_email_verification_tokens_token_hash")),
    )
    op.create_index(
        "ix_email_verification_expires", "email_verification_tokens", ["expires_at"]
    )
    op.create_index(
        "ix_email_verification_user",
        "email_verification_tokens",
        ["user_id", "used_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_verification_user", table_name="email_verification_tokens")
    op.drop_index("ix_email_verification_expires", table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")