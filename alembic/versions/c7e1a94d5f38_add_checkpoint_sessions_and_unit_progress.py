"""checkpoint sessions and unit_progress behind sequential unlocking

Two schema changes underpin the checkpoint slice:

* ``quiz_sessions.unit_id`` — a checkpoint's questions span a whole unit's lessons, so no
  single lesson owns the session. Nullable, and ``SET NULL`` on delete for the same
  reason ``lesson_id`` is: retiring a unit must not erase the sessions that sat it.
* ``unit_progress`` — one row per ``(user, unit)`` holding the checkpoint's best score,
  attempt count and latched ``passed_at``. ``passed_at`` is the column the unlock rule
  reads; set means the unit's 80% bar was cleared at least once, and it never clears.

The ``valid_mode`` CHECK on ``quiz_sessions`` is dropped and recreated rather than left
alone because a ``CHECK`` is the last line of defence for rows written by anything that
bypasses the domain — and this revision widens it, it does not narrow it: every value the
old constraint allowed is still allowed.

Backfill: every ``(user, unit)`` pair whose published lessons are *all* completed earns a
grandfathered pass — ``passed_at`` set now, ``attempts`` 0, ``best_score`` 0. Before this
revision there was no checkpoint to take, so locking learners out of units they had
already finished would punish them for a rule that did not exist yet. ``best_score``
stays 0 precisely because nobody scored it: the unlock rule reads ``passed_at`` and never
the score, so the honest record is "passed, never attempted".

Revision ID: c7e1a94d5f38
Revises: f6c35e9b1a84
Create Date: 2026-10-19 12:50:00.000000+00:00
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7e1a94d5f38"
down_revision: str | None = "f6c35e9b1a84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spelled out rather than imported from ``app`` — a migration describes one moment in
#: the schema's history and has to keep meaning the same thing after the enum grows
#: again. The model derives the live constraint from ``SessionMode``; ``alembic check``
#: in the migration tests compares the two.
_MODES_AT_THIS_REVISION = ("lesson", "checkpoint", "review", "placement")

#: A pair passes the backfill when this user has a ``completed`` row for every published
#: lesson of this unit. Triple ``NOT EXISTS`` rather than ``GROUP BY ... HAVING`` so the
#: statement is plain correlated SQL that both SQLite and PostgreSQL agree on, including
#: the empty-unit case — an outer row only exists when the user completed at least one
#: published lesson of the unit, so a unit with nothing completed (or nothing published)
#: is never grandfathered.
_COMPLETED_UNITS_SQL = """
    SELECT DISTINCT lp.user_id, l.unit_id
    FROM lesson_progress lp
    JOIN lessons l ON l.id = lp.lesson_id
    WHERE l.state = 'published'
      AND lp.state = 'completed'
      AND NOT EXISTS (
          SELECT 1
          FROM lessons l2
          WHERE l2.unit_id = l.unit_id
            AND l2.state = 'published'
            AND NOT EXISTS (
                SELECT 1
                FROM lesson_progress lp2
                WHERE lp2.lesson_id = l2.id
                  AND lp2.user_id = lp.user_id
                  AND lp2.state = 'completed'
            )
      )
"""

_INSERT_UNIT_PROGRESS_SQL = """
    INSERT INTO unit_progress (id, user_id, unit_id, best_score, attempts, passed_at)
    VALUES (:id, :user_id, :unit_id, 0, 0, CURRENT_TIMESTAMP)
"""


def _fresh_uuid(bind: sa.Connection) -> object:
    """A primary key in whatever format this backend stores UUIDs.

    PostgreSQL's native ``uuid`` takes the dashed form the driver hands it; SQLite's
    ``CHAR(32)`` (see ``type_annotation_map`` in ``app/core/base``) stores the bare hex.
    Binding the wrong one does not fail loudly — SQLite happily stores 36 characters in a
    32-character column — it fails later, when the ORM cannot parse the row back.
    """
    if bind.dialect.name == "postgresql":
        return uuid.uuid4()
    return uuid.uuid4().hex


def upgrade() -> None:
    bind = op.get_bind()

    with op.batch_alter_table("quiz_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("unit_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_quiz_sessions_unit_id_units"),
            "units",
            ["unit_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_quiz_sessions_unit", ["unit_id"], unique=False)
        batch_op.drop_constraint(op.f("ck_quiz_sessions_valid_mode"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_quiz_sessions_valid_mode"),
            f"mode IN ({', '.join(repr(mode) for mode in _MODES_AT_THIS_REVISION)})",
        )

    op.create_table(
        "unit_progress",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("best_score", sa.SmallInteger(), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), nullable=False),
        sa.Column("passed_at", sa.DateTime(timezone=True), nullable=True),
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
            "best_score BETWEEN 0 AND 100",
            name=op.f("ck_unit_progress_score_in_range"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_unit_progress_non_negative_attempts")
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"],
            ["units.id"],
            name=op.f("fk_unit_progress_unit_id_units"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_unit_progress_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unit_progress")),
        sa.UniqueConstraint("user_id", "unit_id", name="uq_unit_progress_user_unit"),
    )
    with op.batch_alter_table("unit_progress", schema=None) as batch_op:
        batch_op.create_index("ix_unit_progress_user", ["user_id"], unique=False)
        batch_op.create_index("ix_unit_progress_unit", ["unit_id"], unique=False)

    # Grandfather every learner who already finished their units — see the module
    # docstring for why the scores stay 0. A row per pair, not a bulk INSERT ... SELECT,
    # because the primary key is a UUID this process must invent and neither backend
    # spells that the same way; the volume is bounded by learners, not by answers.
    rows = bind.execute(sa.text(_COMPLETED_UNITS_SQL)).fetchall()
    for user_id, unit_id in rows:
        bind.execute(
            sa.text(_INSERT_UNIT_PROGRESS_SQL),
            {"id": _fresh_uuid(bind), "user_id": user_id, "unit_id": unit_id},
        )


def downgrade() -> None:
    # Dropping unit_progress takes every learner's checkpoint record with it, and there
    # is nowhere to put it back: the score was computed against questions this schema no
    # longer groups by unit. The alternative — exporting to a side table nobody reads —
    # would be a worse kind of loss, one that looks like a backup.
    with op.batch_alter_table("unit_progress", schema=None) as batch_op:
        batch_op.drop_index("ix_unit_progress_unit")
        batch_op.drop_index("ix_unit_progress_user")

    op.drop_table("unit_progress")

    with op.batch_alter_table("quiz_sessions", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_quiz_sessions_valid_mode"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_quiz_sessions_valid_mode"),
            "mode IN ('lesson', 'review', 'placement')",
        )
        batch_op.drop_constraint(
            op.f("fk_quiz_sessions_unit_id_units"), type_="foreignkey"
        )
        batch_op.drop_index("ix_quiz_sessions_unit")
        batch_op.drop_column("unit_id")
