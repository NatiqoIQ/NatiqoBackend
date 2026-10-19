\"\"\"remove support role from users.roles

Revision ID: f6c35e9b1a84
Revises: f5d27c9a3e41
Create Date: 2026-10-19 10:00:00.000000+00:00
\"\"\"

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f6c35e9b1a84"
down_revision: str | None = "f5d27c9a3e41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, autoload_with=conn)

    result = conn.execute(sa.select(users.c.id, users.c.roles))
    for user_id, roles in result.fetchall():
        if not roles:
            continue
        if isinstance(roles, str):
            import json

            try:
                roles_list = json.loads(roles)
            except Exception:
                roles_list = [roles]
        else:
            roles_list = list(roles)
        if "support" in roles_list:
            new_roles = [r for r in roles_list if r != "support"]
            if not new_roles:
                new_roles = ["user"]
            conn.execute(
                sa.update(users).where(users.c.id == user_id).values(roles=new_roles)
            )


def downgrade() -> None:
    pass
