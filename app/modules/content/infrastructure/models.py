"""Content module — SQLAlchemy models.

Four tables mirroring the ``Level → Unit → Lesson → Question`` tree, with
``ON DELETE CASCADE`` down the chain: deleting a level should take its curriculum with
it, and there is nothing a unit means without its level.

Every table carries the published filter in a composite index that matches the exact
keyset predicate the repositories issue (``parent, state, position, id``). That is the
difference between the catalogue being cheap to page and doing a scan-and-sort per
request as the curriculum grows.

Two database-level guards back up domain rules that would otherwise be reachable only
by a buggy write path:

* ``CHECK (position >= 1)`` — ordering starts at 1; see ``domain/policy.py``.
* ``UNIQUE (parent_id, position)`` — siblings cannot share a slot. This is stricter
  than the cursor strictly requires (``(position, id)`` is already a total order), but
  it keeps "the third unit" unambiguous for humans editing the data directly. The cost
  is that a retired unit keeps its slot, so inserting a replacement at that position
  means renumbering.

The parent/child ``relationship()``s look decorative next to those constraints — the
repositories never navigate them, they join explicitly — but they are load-bearing. With
foreign keys and no relationship, the ORM has no dependency edge between the mappers and
sorts a flush's inserts **alphabetically by class name**, so adding a level, a unit and
a lesson in one flush emits ``INSERT INTO lessons`` before ``INSERT INTO units`` and the
database rejects it with a foreign key error. The relationship is what tells the unit of
work which table has to land first.

``lazy="raise_on_sql"`` is the second half of that: the read path is built on explicit
queries, so an accidental attribute access must fail loudly rather than quietly issue one
extra ``SELECT`` per row.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.core.types import CEFRLevel, ContentState, QuestionKind, SkillKind

#: Written into every ``CHECK`` on ``state``. Kept as a module constant so the models and
#: the migration cannot drift apart by being edited separately.
CONTENT_STATES: tuple[str, ...] = tuple(state.value for state in ContentState)

CONTENT_STATE_SQL: str = ", ".join(f"'{state}'" for state in CONTENT_STATES)

QUESTION_KINDS: tuple[str, ...] = tuple(kind.value for kind in QuestionKind)

QUESTION_KINDS_SQL: str = ", ".join(f"'{kind}'" for kind in QUESTION_KINDS)

SKILL_KINDS: tuple[str, ...] = tuple(skill.value for skill in SkillKind)

SKILL_KINDS_SQL: str = ", ".join(f"'{skill}'" for skill in SKILL_KINDS)

CEFR_CODES: tuple[str, ...] = tuple(code.value for code in CEFRLevel)

CEFR_CODES_SQL: str = ", ".join(f"'{code}'" for code in CEFR_CODES)


class LevelModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "levels"

    #: The CEFR code is the catalogue's stable public handle (``/levels/A1``), so it is
    #: unique and may never be reused for a different level.
    code: Mapped[str] = mapped_column(String(2), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=ContentState.DRAFT.value)

    __table_args__ = (
        CheckConstraint(f"code IN ({CEFR_CODES_SQL})", name="valid_code"),
        CheckConstraint(f"state IN ({CONTENT_STATE_SQL})", name="valid_state"),
        CheckConstraint("position >= 1", name="positive_position"),
        Index("ix_levels_catalogue", "state", "position", "id"),
    )

    units: Mapped[list[UnitModel]] = relationship(
        back_populates="level", lazy="raise_on_sql", passive_deletes=True
    )


class UnitModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "units"

    level_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("levels.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=ContentState.DRAFT.value)

    __table_args__ = (
        CheckConstraint(f"state IN ({CONTENT_STATE_SQL})", name="valid_state"),
        CheckConstraint("position >= 1", name="positive_position"),
        UniqueConstraint("level_id", "position", name="uq_units_level_position"),
        Index("ix_units_catalogue", "level_id", "state", "position", "id"),
    )

    level: Mapped[LevelModel] = relationship(back_populates="units", lazy="raise_on_sql")
    lessons: Mapped[list[LessonModel]] = relationship(
        back_populates="unit", lazy="raise_on_sql", passive_deletes=True
    )


class LessonModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "lessons"

    unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("units.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=ContentState.DRAFT.value)

    __table_args__ = (
        CheckConstraint(f"state IN ({CONTENT_STATE_SQL})", name="valid_state"),
        CheckConstraint("position >= 1", name="positive_position"),
        CheckConstraint("estimated_minutes >= 1", name="positive_estimated_minutes"),
        UniqueConstraint("unit_id", "position", name="uq_lessons_unit_position"),
        Index("ix_lessons_catalogue", "unit_id", "state", "position", "id"),
    )

    unit: Mapped[UnitModel] = relationship(back_populates="lessons", lazy="raise_on_sql")
    questions: Mapped[list[QuestionModel]] = relationship(
        back_populates="lesson", lazy="raise_on_sql", passive_deletes=True
    )


class QuestionModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A practice item.

    Deliberately carries **no** ``state`` column. A question has no editorial lifecycle
    of its own: it is visible exactly when its lesson is, and giving it a second state to
    keep in sync would create a way to publish a question into a draft lesson.
    """

    __tablename__ = "questions"

    lesson_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    skill: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    choices: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: Answer keys for free-text items, already normalised by the domain. Empty for
    #: every other kind.
    accepted_answers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    #: Index into ``choices``. ``NULL`` for free-text items.
    correct_index: Mapped[int | None] = mapped_column(Integer)
    #: Post-attempt feedback. Never sent to a learner before they have answered; see
    #: ``presentation/schemas.py``.
    explanation: Mapped[str | None] = mapped_column(Text)
    #: Optional pre-attempt visual aid that illustrates the taught word or sentence.
    #: Public, unlike ``explanation``: it teaches the item rather than revealing the
    #: answer.
    image_url: Mapped[str | None] = mapped_column(Text)
    #: Optional pre-attempt audio aid that pronounces the taught word or sentence.
    #: Public for the same reason as ``image_url``.
    audio_url: Mapped[str | None] = mapped_column(Text)
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(f"kind IN ({QUESTION_KINDS_SQL})", name="valid_kind"),
        CheckConstraint(f"skill IN ({SKILL_KINDS_SQL})", name="valid_skill"),
        CheckConstraint("position >= 1", name="positive_position"),
        CheckConstraint("points >= 1", name="positive_points"),
        CheckConstraint(
            "correct_index IS NULL OR correct_index >= 0", name="non_negative_correct_index"
        ),
        UniqueConstraint("lesson_id", "position", name="uq_questions_lesson_position"),
        Index("ix_questions_catalogue", "lesson_id", "position", "id"),
    )

    lesson: Mapped[LessonModel] = relationship(back_populates="questions", lazy="raise_on_sql")
