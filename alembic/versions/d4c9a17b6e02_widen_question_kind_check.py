"""widen the question kind check to the full repertoire

``QuestionKind`` now carries seven values — multi-select, word-bank, ordering and matching
joined the original three — but the ``CHECK`` on ``questions.kind`` was fixed at the three
that existed when the catalogue table was created. The domain would happily build a
word-bank question and the database would refuse to store it — which is the failure that
matters: not a wrong answer, but a whole question type that cannot be seeded or authored.

It also carries no column of its own, and that is the point of how the last two kinds are
stored rather than an omission: an ordering question keeps its key in ``choices`` and a
matching question keeps its pairs in ``accepted_answers``, so widening this constraint is the
entire schema change the repertoire needs.

The constraint is dropped and recreated rather than left alone because a ``CHECK`` is the
last line of defence for rows written by anything that bypasses the domain, and widening it
is the only way to keep that defence while letting the new kinds in.

Existing rows are unaffected: every value the old constraint allowed is still allowed.

This revision sits on ``b13ae24dcdfd``, not on the ``b1f7c2e9a4d6`` it was written against:
gamification, audit logs and media assets landed while it was being authored, and branching
from the older base left the history with two heads. ``alembic upgrade head`` refuses that
outright, so the container exited on startup instead of serving — the reason this had to be
re-parented rather than merged with an empty revision.

Revision ID: d4c9a17b6e02
Revises: b13ae24dcdfd
Create Date: 2026-10-16 22:40:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4c9a17b6e02"
down_revision: str | None = "b13ae24dcdfd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Spell the values out rather than importing them from ``app.core.types``. A migration
#: describes one moment in the schema's history, so it has to keep meaning the same thing
#: even after the enum grows again; ``app/modules/content/infrastructure/models.py`` derives
#: the live constraint from the enum, and the two are compared by the migration tests.
_ORIGINAL_KINDS = ("multiple_choice", "true_false", "fill_blank")

_WIDENED_KINDS = (
    "multiple_choice",
    "multi_select",
    "true_false",
    "fill_blank",
    "word_bank",
    "ordering",
    "matching",
)


def _kind_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_questions_valid_kind"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_questions_valid_kind"), f"kind IN ({_kind_list(_WIDENED_KINDS)})"
        )


def downgrade() -> None:
    # Narrowing the constraint back deletes nothing, but it does fail loudly if a row of a
    # kind the old schema had no name for is present — which is the correct outcome: the
    # alternative would be silently discarding questions that a learner has already met.
    with op.batch_alter_table("questions", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_questions_valid_kind"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_questions_valid_kind"), f"kind IN ({_kind_list(_ORIGINAL_KINDS)})"
        )
