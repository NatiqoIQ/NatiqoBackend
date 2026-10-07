"""social sign-in: replace credentials with provider identities

Revision ID: 7a4d2f9c1b58
Revises: c4a91f2e7b08
Create Date: 2026-10-06 10:30:00.000000+00:00

Sign-in is delegated to Google and Apple, so the columns and tables that existed to hold
a credential stop having a reason to exist:

* ``users.password_hash``, ``password_changed_at`` and the lockout pair are dropped. A
  learner's identity is now the ``(provider, subject)`` pair in ``user_identities``.
* ``password_reset_tokens`` and ``email_verification_tokens`` are dropped with the flows
  that minted them. Leaving them behind would keep two tables that no code path reads
  and that a future reader would reasonably assume are live.

``user_identities`` is a table rather than columns on ``users`` because one learner may
legitimately hold two provider accounts — the same person signing in with Google on
Android and Apple on iOS must land on one account, and nothing about that is expressible
as a single pair of columns.

The email column on the new table is recorded for support and is deliberately **not**
unique: it is the same address that identifies the row in ``users``, and a second
``UNIQUE`` here would reject the second provider for a learner who links both.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a4d2f9c1b58"
down_revision: str | None = "c4a91f2e7b08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_identities",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=True),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("picture_url", sa.String(length=512), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint(
            "provider IN ('google', 'apple')",
            name=op.f("ck_user_identities_valid_provider"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_identities_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_identities")),
        # The lookup every sign-in performs. Unique, because two accounts sharing one
        # provider identity is the one state that would make "who is this" ambiguous.
        sa.UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),
    )
    with op.batch_alter_table("user_identities", schema=None) as batch_op:
        batch_op.create_index("ix_identity_user", ["user_id"], unique=False)

    # Dropped with the flows that wrote them. Their rows are single-use tokens that
    # expired within days, so there is nothing worth migrating forward.
    op.drop_table("email_verification_tokens")
    op.drop_table("password_reset_tokens")

    # Batch mode rather than plain ALTERs: the test suite runs the real migration path
    # against SQLite, which cannot drop a constraint in place, and batch mode emits the
    # native statement on PostgreSQL while still working there.
    with op.batch_alter_table("users", schema=None) as batch_op:
        # The logical name, not the final one: the metadata naming convention prefixes
        # it with ``ck_users_``, and passing the finished name would prefix it twice.
        batch_op.drop_constraint("non_negative_attempts", type_="check")
        batch_op.drop_column("password_hash")
        batch_op.drop_column("password_changed_at")
        batch_op.drop_column("failed_login_attempts")
        batch_op.drop_column("locked_until")


def downgrade() -> None:
    # Re-added nullable. The original columns were NOT NULL, but rows created while this
    # revision was applied have no credential to put back, and a downgrade that cannot
    # run on a populated database is not a downgrade.
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("password_hash", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("password_changed_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("failed_login_attempts", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("locked_until", sa.DateTime(timezone=True)))
        batch_op.create_check_constraint("non_negative_attempts", "failed_login_attempts >= 0")

    op.create_table(
        "password_reset_tokens",
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
            ["user_id"], ["users.id"], name=op.f("fk_password_reset_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_reset_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_password_reset_tokens_token_hash")),
    )
    # The indexes have to come back too. A later revision's downgrade drops them by
    # name, so omitting them here converts "revert everything" into an error one step
    # further down the chain — a failure that only appears when someone is already
    # rolling back a problem.
    with op.batch_alter_table("password_reset_tokens", schema=None) as batch_op:
        batch_op.create_index("ix_password_reset_expires", ["expires_at"], unique=False)
        batch_op.create_index("ix_password_reset_user", ["user_id", "used_at"], unique=False)
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
            name=op.f("fk_email_verification_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_verification_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_email_verification_tokens_token_hash")),
    )
    with op.batch_alter_table("email_verification_tokens", schema=None) as batch_op:
        batch_op.create_index("ix_email_verification_expires", ["expires_at"], unique=False)
        batch_op.create_index(
            "ix_email_verification_user", ["user_id", "used_at"], unique=False
        )

    with op.batch_alter_table("user_identities", schema=None) as batch_op:
        batch_op.drop_index("ix_identity_user")
    op.drop_table("user_identities")
