"""replace users.roles with a single is_admin flag

The role vocabulary (user/content_editor/content_reviewer/admin) is gone: an account
either runs the admin surface or it does not. ``is_admin`` is backfilled from the old
column so every account that could reach the admin surface yesterday still can, and
nobody gains a privilege they did not have.

Revision ID: a3d7f1c9e502
Revises: c7e1a94d5f38
Create Date: 2026-10-19 15:00:00.000000+00:00
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3d7f1c9e502"
down_revision: str | None = "c7e1a94d5f38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _as_list(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return [raw]
        return list(parsed) if isinstance(parsed, list) else [raw]
    try:
        return list(raw)
    except TypeError:
        return []


def upgrade() -> None:
    conn = op.get_bind()
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=conn)

    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    result = conn.execute(sa.select(users.c.id, users.c.roles))
    for user_id, roles in result.fetchall():
        if "admin" in _as_list(roles):
            conn.execute(
                sa.update(users).where(users.c.id == user_id).values(is_admin=True)
            )

    op.drop_column("users", "roles")


def downgrade() -> None:
    conn = op.get_bind()
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=conn)

    op.add_column(
        "users",
        sa.Column("roles", sa.JSON(), nullable=False, server_default=sa.text("'[\"user\"]'")),
    )

    result = conn.execute(sa.select(users.c.id, users.c.is_admin))
    for user_id, is_admin in result.fetchall():
        roles = ["admin"] if is_admin else ["user"]
        conn.execute(sa.update(users).where(users.c.id == user_id).values(roles=roles))

    op.drop_column("users", "is_admin")
