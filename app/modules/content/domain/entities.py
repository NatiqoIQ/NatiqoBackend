"""The catalogue aggregates: Level → Unit → Lesson → Question.

The tree is authored by staff and read by every learner, so the rules that decide what
is allowed to become visible live here rather than in a route or a migration.

Two decisions shape everything below:

* **Visibility is inherited down the tree.** A question is not independently
  published; it is visible exactly when its lesson is. That keeps a single source of
  truth for "may a learner see this", so there is no way to publish a question into a
  draft lesson.
* **Answers are not part of the catalogue view.** :class:`Question` carries the answer
  key because the attempt engine needs it, but the presentation layer is responsible
  for not sending it to a learner who has not attempted the question yet. See
  ``presentation/schemas.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.types import CEFRLevel, ContentId, ContentState, QuestionKind, SkillKind
from app.modules.content.domain.policy import (
    InvalidContentState,
    assert_transition_allowed,
    assert_valid_position,
    normalise_accepted_answers,
    question_shape_problems,
)

__all__ = [
    "InvalidContentState",
    "Lesson",
    "Level",
    "Question",
    "Unit",
]


@dataclass(slots=True)
class Level:
    """A CEFR level: a top-level catalogue entry that owns the units beneath it."""

    id: ContentId
    code: CEFRLevel
    title: str
    position: int
    description: str | None = None
    state: ContentState = ContentState.DRAFT

    def __post_init__(self) -> None:
        assert_valid_position(self.position)

    @property
    def is_published(self) -> bool:
        return self.state is ContentState.PUBLISHED

    def transition_to(self, target: ContentState) -> None:
        assert_transition_allowed(self.state, target)
        self.state = target


@dataclass(slots=True)
class Unit:
    """A themed group of lessons inside one level."""

    id: ContentId
    level_id: ContentId
    title: str
    position: int
    description: str | None = None
    state: ContentState = ContentState.DRAFT

    def __post_init__(self) -> None:
        assert_valid_position(self.position)

    @property
    def is_published(self) -> bool:
        return self.state is ContentState.PUBLISHED

    def transition_to(self, target: ContentState) -> None:
        assert_transition_allowed(self.state, target)
        self.state = target

    def publish(self, level: Level) -> None:
        """Publish this unit, given its level.

        The parent is a parameter rather than something the aggregate looks up, so the
        rule is checked in exactly one place and a caller cannot publish an orphan by
        forgetting to pass it.
        """
        if not level.is_published:
            raise InvalidContentState("A unit cannot be published before its level.")
        assert_transition_allowed(self.state, ContentState.PUBLISHED)
        self.state = ContentState.PUBLISHED


@dataclass(slots=True)
class Lesson:
    """A single study session inside a unit."""

    id: ContentId
    unit_id: ContentId
    title: str
    summary: str
    position: int
    estimated_minutes: int
    state: ContentState = ContentState.DRAFT

    def __post_init__(self) -> None:
        assert_valid_position(self.position)
        if self.estimated_minutes < 1:
            raise ValueError(
                f"estimated_minutes must be one or greater, got {self.estimated_minutes}."
            )

    @property
    def is_published(self) -> bool:
        return self.state is ContentState.PUBLISHED

    def transition_to(self, target: ContentState) -> None:
        assert_transition_allowed(self.state, target)
        self.state = target

    def publish(self, unit: Unit) -> None:
        """Publish this lesson, given its unit. See :meth:`Unit.publish` for why the
        parent is passed in rather than looked up."""
        if not unit.is_published:
            raise InvalidContentState("A lesson cannot be published before its unit.")
        assert_transition_allowed(self.state, ContentState.PUBLISHED)
        self.state = ContentState.PUBLISHED


@dataclass(frozen=True, slots=True)
class Question:
    """A practice item inside a lesson.

    Frozen because a question is not edited in place: revising a published question
    means replacing the row, which keeps the answer key that learners have already been
    marked against consistent with the one in the catalogue.
    """

    id: ContentId
    lesson_id: ContentId
    position: int
    kind: QuestionKind
    skill: SkillKind
    prompt: str
    choices: tuple[str, ...] = ()
    accepted_answers: tuple[str, ...] = ()
    correct_index: int | None = None
    explanation: str | None = None
    points: int = 1

    def __post_init__(self) -> None:
        assert_valid_position(self.position)
        if self.points < 1:
            raise ValueError(
                f"points must be one or greater, got {self.points}; a zero-point question "
                "can never be scored."
            )
        # ``accepted_answers`` is normalised once here so that marking does not have to
        # re-derive the comparable form on every comparison.
        object.__setattr__(
            self,
            "accepted_answers",
            normalise_accepted_answers(self.accepted_answers),
        )
        problems = question_shape_problems(
            self.kind,
            choices=self.choices,
            accepted_answers=self.accepted_answers,
            correct_index=self.correct_index,
        )
        if problems:
            raise ValueError(" ".join(problems))
