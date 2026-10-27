"""replace the daily-challenge counters with a contribution ledger

The two counters on ``user_daily_challenges`` were incremented by whoever wrote last,
which made the day a request-time fact: a completion whose write landed after midnight — a
retry, a queued delivery, a second device — credited the day it arrived instead of the day
it finished. And because the counts were stored rather than derived, a repeated report
counted a sitting twice. Both problems share one cause: the day's progress was *state*
instead of a *sum*.

This revision makes each finished sitting a row of its own, keyed by
``(user_id, session_id)`` and carrying the day it finished on in the learner's timezone.
Counts become a ``SUM`` over that ledger, so a retry is a no-op by construction and the
day is decided once, by the sitting, not by the clock at write time. The day row keeps
only ``claimed``, the one fact a completion cannot imply.

Existing counters are not discarded: each non-zero day is backfilled as a single
synthetic contribution, because the old data records a day's total and not which sitting
produced it. The synthetic key is derived from the day itself, so it is stable across a
downgrade/upgrade cycle.

Revision ID: c3f7b21e9d84
Revises: b9e4d2f7c1a5
Create Date: 2026-10-27
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f7b21e9d84"
down_revision: str | None = "b9e4d2f7c1a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DAY_NAMESPACE = "https://natiqo.app/daily-challenge"


def _day_table() -> sa.TableClause:
    """The day row with explicit types, for the same reason as the ledger below."""
    return sa.table(
        "user_daily_challenges",
        sa.column("id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("challenge_date", sa.String(10)),
        sa.column("lessons_completed_today", sa.SmallInteger()),
        sa.column("words_learned_today", sa.SmallInteger()),
        sa.column("claimed", sa.Boolean()),
    )


def _contribution_table() -> sa.TableClause:
    """The ledger with explicit types.

    Declared rather than reflected so the ``Uuid`` bind processor applies on every
    dialect: a reflected SQLite ``CHAR(32)`` would receive a raw ``uuid.UUID`` and the
    driver would reject it.
    """
    return sa.table(
        "user_daily_challenge_contributions",
        sa.column("id", sa.Uuid()),
        sa.column("user_id", sa.Uuid()),
        sa.column("session_id", sa.Uuid()),
        sa.column("challenge_date", sa.String(10)),
        sa.column("lessons_completed", sa.SmallInteger()),
        sa.column("words_learned", sa.SmallInteger()),
    )


def upgrade() -> None:
    conn = op.get_bind()

    op.create_table(
        "user_daily_challenge_contributions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        #: The completed sitting. Not a foreign key to ``quiz_sessions``: the users module
        #: does not depend on the quiz schema, and a day's totals should outlive the row.
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("challenge_date", sa.String(length=10), nullable=False),
        sa.Column(
            "lessons_completed",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "words_learned",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id",
            "session_id",
            name="uq_daily_challenge_contribution_session",
        ),
    )
    op.create_index(
        "ix_daily_challenge_contribution_day",
        "user_daily_challenge_contributions",
        ["user_id", "challenge_date"],
        unique=False,
    )

    _backfill_legacy_counters(conn)

    # The unique ``(user_id, challenge_date)`` constraint already indexes ``user_id`` as
    # its leading column, so this standalone index only cost writes.
    op.drop_index("ix_user_daily_challenge_user", table_name="user_daily_challenges")
    op.drop_column("user_daily_challenges", "lessons_completed_today")
    op.drop_column("user_daily_challenges", "words_learned_today")


def _backfill_legacy_counters(conn: sa.Connection) -> None:
    """Turn each stored day total into one synthetic contribution row."""
    days = _day_table()
    contributions = _contribution_table()

    rows = conn.execute(
        sa.select(
            days.c.user_id,
            days.c.challenge_date,
            days.c.lessons_completed_today,
            days.c.words_learned_today,
        ).where(
            sa.or_(
                days.c.lessons_completed_today > 0,
                days.c.words_learned_today > 0,
            )
        )
    ).fetchall()

    payload: list[dict[str, object]] = []
    for user_id, challenge_date, lessons, words in rows:
        key = f"{_DAY_NAMESPACE}/{user_id}/{challenge_date}"
        payload.append(
            {
                "id": uuid.uuid5(uuid.NAMESPACE_URL, f"{key}/row"),
                "user_id": user_id,
                "session_id": uuid.uuid5(uuid.NAMESPACE_URL, key),
                "challenge_date": challenge_date,
                "lessons_completed": int(lessons or 0),
                "words_learned": int(words or 0),
            }
        )
    if payload:
        conn.execute(contributions.insert(), payload)


def downgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        "user_daily_challenges",
        sa.Column(
            "lessons_completed_today",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "user_daily_challenges",
        sa.Column(
            "words_learned_today",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_index(
        "ix_user_daily_challenge_user", "user_daily_challenges", ["user_id"], unique=False
    )

    _restore_legacy_counters(conn)

    op.drop_index(
        "ix_daily_challenge_contribution_day",
        table_name="user_daily_challenge_contributions",
    )
    op.drop_table("user_daily_challenge_contributions")


def _restore_legacy_counters(conn: sa.Connection) -> None:
    """Fold the ledger back into the counters the previous schema stored.

    A day with recorded progress but no claim has no day row at all under the ledger
    schema, so one is created here rather than letting that progress disappear on a
    downgrade.
    """
    days = _day_table()
    contributions = _contribution_table()

    totals = conn.execute(
        sa.select(
            contributions.c.user_id,
            contributions.c.challenge_date,
            sa.func.sum(contributions.c.lessons_completed),
            sa.func.sum(contributions.c.words_learned),
        ).group_by(contributions.c.user_id, contributions.c.challenge_date)
    ).fetchall()

    for user_id, challenge_date, lessons, words in totals:
        values = {
            "lessons_completed_today": int(lessons or 0),
            "words_learned_today": int(words or 0),
        }
        updated = conn.execute(
            days.update()
            .where(
                days.c.user_id == user_id,
                days.c.challenge_date == challenge_date,
            )
            .values(**values)
        )
        if updated.rowcount:
            continue
        conn.execute(
            days.insert().values(
                id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{_DAY_NAMESPACE}/{user_id}/{challenge_date}/row",
                ),
                user_id=user_id,
                challenge_date=challenge_date,
                claimed=False,
                **values,
            )
        )
