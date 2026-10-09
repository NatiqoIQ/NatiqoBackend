"""Domain invariants for the content catalogue.

The catalogue is the one part of Natiqo that is *written by staff and read by every
learner*, so its rules live in the domain rather than in a route or a migration. These
tests pin the three rules that are expensive to get wrong:

1. **Only published content is a catalogue entry.** A draft leaking into a learner's
   unit list is a broken promise about what they can study, not a cosmetic issue.
2. **The hierarchy publishes top-down.** A published lesson inside a draft unit is
   unreachable, so allowing it produces content that exists and cannot be opened.
3. **A question is only answerable if its shape is self-consistent.** An answer index
   pointing past the end of the choice list is a question nobody can get right, and it
   is much cheaper to reject it at construction than to discover it in a learner's
   session.

Every assertion here is a rule, not an implementation detail: changing any of these
behaviours should require changing these tests on purpose.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.types import CEFRLevel, ContentId, ContentState, QuestionKind, SkillKind, new_id
from app.modules.content.domain import (
    InvalidContentState,
    Lesson,
    Level,
    Question,
    Unit,
)


def _kwargs(**overrides: Any) -> dict[str, Any]:
    """A complete, valid payload that a test may spoil one field at a time."""
    payload: dict[str, Any] = {
        "id": ContentId(new_id()),
        "code": CEFRLevel.A1,
        "title": "Beginner",
        "position": 1,
    }
    payload.update(overrides)
    return payload


def _approved(entity: Any) -> Any:
    """Walk a fresh aggregate to APPROVED — the last state before it may go live."""
    entity.transition_to(ContentState.REVIEW)
    entity.transition_to(ContentState.APPROVED)
    return entity


def _published_level(**overrides: Any) -> Level:
    level = _approved(Level(**_kwargs(**overrides)))
    level.transition_to(ContentState.PUBLISHED)
    return level


def _unit_kwargs(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": ContentId(new_id()),
        "level_id": ContentId(new_id()),
        "title": "Introductions",
        "position": 1,
    }
    payload.update(overrides)
    return payload


def _published_unit(**overrides: Any) -> Unit:
    unit = _approved(Unit(**_unit_kwargs(**overrides)))
    unit.publish(_published_level())
    return unit


def _lesson_kwargs(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": ContentId(new_id()),
        "unit_id": ContentId(new_id()),
        "title": "Greetings",
        "summary": "Say hello and goodbye.",
        "position": 1,
        "estimated_minutes": 8,
    }
    payload.update(overrides)
    return payload


def _multiple_choice(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": ContentId(new_id()),
        "lesson_id": ContentId(new_id()),
        "position": 1,
        "kind": QuestionKind.MULTIPLE_CHOICE,
        "skill": SkillKind.VOCABULARY,
        "prompt": "Which one means 'hello'?",
        "choices": ("Hello", "Goodbye", "Thank you"),
        "correct_index": 0,
        "explanation": "A greeting.",
    }
    payload.update(overrides)
    return payload


# ======================================================================== level
class TestLevel:
    def test_a_new_level_is_a_draft(self) -> None:
        level = Level(**_kwargs())

        assert level.state is ContentState.DRAFT
        assert level.is_published is False

    def test_position_must_be_one_or_greater(self) -> None:
        """Ordering starts at 1. A 0 would sort before every real position and make
        "the first unit" ambiguous the moment a second one is added."""
        with pytest.raises(ValueError, match="position"):
            Level(**_kwargs(position=0))

    def test_the_editorial_lifecycle_can_be_walked_to_published(self) -> None:
        level = Level(**_kwargs())

        level.transition_to(ContentState.REVIEW)
        level.transition_to(ContentState.APPROVED)
        level.transition_to(ContentState.PUBLISHED)

        assert level.is_published is True

    def test_a_draft_cannot_jump_straight_to_published(self) -> None:
        """Skipping review is exactly the mistake a two-person editorial process
        exists to prevent, so the aggregate refuses it rather than trusting callers."""
        level = Level(**_kwargs())

        with pytest.raises(InvalidContentState):
            level.transition_to(ContentState.PUBLISHED)

        assert level.state is ContentState.DRAFT, "a refused transition must not mutate"


# ========================================================================== unit
class TestUnit:
    def test_a_new_unit_is_a_draft(self) -> None:
        unit = Unit(**_unit_kwargs())

        assert unit.state is ContentState.DRAFT

    def test_a_unit_publishes_once_its_level_is_published(self) -> None:
        unit = _approved(Unit(**_unit_kwargs()))

        unit.publish(_published_level())

        assert unit.is_published is True

    def test_a_unit_cannot_publish_before_its_level(self) -> None:
        """The rule the hierarchy exists for: a published unit under a draft level is
        content no learner can reach, so it is refused at the point of publication."""
        unit = _approved(Unit(**_unit_kwargs()))

        with pytest.raises(InvalidContentState, match="level"):
            unit.publish(Level(**_kwargs()))

        assert unit.state is ContentState.APPROVED, "a refused publish must not mutate"

    def test_publish_does_not_skip_the_review_steps(self) -> None:
        """``publish`` is the last hop, not a shortcut. If it could take a draft
        straight live then the review state would be decoration — a single call would
        bypass the two-person process the lifecycle is built around."""
        unit = Unit(**_unit_kwargs())

        with pytest.raises(InvalidContentState, match="draft"):
            unit.publish(_published_level())

        assert unit.state is ContentState.DRAFT

    def test_position_must_be_one_or_greater(self) -> None:
        with pytest.raises(ValueError, match="position"):
            Unit(**_unit_kwargs(position=0))


# ========================================================================= lesson
class TestLesson:
    def test_a_new_lesson_is_a_draft(self) -> None:
        lesson = Lesson(**_lesson_kwargs())

        assert lesson.state is ContentState.DRAFT

    def test_a_lesson_publishes_once_its_unit_is_published(self) -> None:
        lesson = _approved(Lesson(**_lesson_kwargs()))

        lesson.publish(_published_unit())

        assert lesson.is_published is True

    def test_a_lesson_cannot_publish_before_its_unit(self) -> None:
        lesson = _approved(Lesson(**_lesson_kwargs()))

        with pytest.raises(InvalidContentState, match="unit"):
            lesson.publish(Unit(**_unit_kwargs()))

        assert lesson.state is ContentState.APPROVED, "a refused publish must not mutate"

    def test_publish_does_not_skip_the_review_steps(self) -> None:
        lesson = Lesson(**_lesson_kwargs())

        with pytest.raises(InvalidContentState, match="draft"):
            lesson.publish(_published_unit())

        assert lesson.state is ContentState.DRAFT

    def test_estimated_minutes_must_be_positive(self) -> None:
        """A lesson shown as "0 min" reads as broken to a learner deciding whether to
        start it, so the value is refused rather than defaulted."""
        with pytest.raises(ValueError, match="estimated_minutes"):
            Lesson(**_lesson_kwargs(estimated_minutes=0))


# ====================================================================== question
class TestQuestionShape:
    """A question has to be answerable by the shape of its own data.

    Everything here would otherwise surface as a learner staring at a question whose
    correct answer is not among the options.
    """

    def test_a_valid_multiple_choice_question_is_accepted(self) -> None:
        question = Question(**_multiple_choice())

        assert question.kind is QuestionKind.MULTIPLE_CHOICE
        assert question.correct_index == 0

    def test_a_question_carries_its_explanation(self) -> None:
        """The explanation is part of the aggregate, not a field the seed layer happens
        to fill in — the feedback surface is built from it."""
        question = Question(**_multiple_choice(explanation="Bye is the farewell."))

        assert question.explanation == "Bye is the farewell."

    def test_a_question_must_carry_an_explanation(self) -> None:
        """The explanation is required even though a question may also carry image and
        audio aids: those teach the item before an attempt, but only the explanation
        tells a learner why an answer was right or wrong."""
        with pytest.raises(ValueError, match="explanation"):
            Question(**_multiple_choice(explanation=""))

    def test_a_blank_explanation_is_refused(self) -> None:
        with pytest.raises(ValueError, match="explanation"):
            Question(**_multiple_choice(explanation="   "))

    def test_a_question_can_carry_a_visual_aid(self) -> None:
        """The image aid is additive: it illustrates the taught word without replacing
        the explanation, which the tests below keep requiring."""
        question = Question(**_multiple_choice(image_url="https://cdn.test/hello.png"))

        assert question.image_url == "https://cdn.test/hello.png"

    def test_the_visual_aid_is_optional(self) -> None:
        assert Question(**_multiple_choice()).image_url is None

    def test_a_blank_image_url_is_refused(self) -> None:
        """A set-but-blank value is an authoring slip, not "no image": setting the field
        promises a resource, and a blank one renders as a broken image."""
        with pytest.raises(ValueError, match="image_url"):
            Question(**_multiple_choice(image_url="   "))

    def test_an_image_url_must_be_an_http_resource(self) -> None:
        with pytest.raises(ValueError, match="image_url"):
            Question(**_multiple_choice(image_url="cdn.test/hello.png"))

    def test_a_script_image_url_is_refused(self) -> None:
        """A ``javascript:`` scheme is the classic injection through an image field."""
        with pytest.raises(ValueError, match="image_url"):
            Question(**_multiple_choice(image_url="javascript:alert(1)"))

    def test_a_question_can_carry_an_audio_aid(self) -> None:
        """The audio aid is the counterpart of the image aid: additive, so it pronounces
        the taught word without replacing the explanation."""
        question = Question(**_multiple_choice(audio_url="https://cdn.test/hello.mp3"))

        assert question.audio_url == "https://cdn.test/hello.mp3"

    def test_the_audio_aid_is_optional(self) -> None:
        assert Question(**_multiple_choice()).audio_url is None

    def test_a_blank_audio_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="audio_url"):
            Question(**_multiple_choice(audio_url="   "))

    def test_an_audio_url_must_be_an_http_resource(self) -> None:
        with pytest.raises(ValueError, match="audio_url"):
            Question(**_multiple_choice(audio_url="cdn.test/hello.mp3"))

    def test_a_script_audio_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="audio_url"):
            Question(**_multiple_choice(audio_url="javascript:alert(1)"))

    def test_multiple_choice_needs_at_least_two_choices(self) -> None:
        with pytest.raises(ValueError, match="at least 2"):
            Question(**_multiple_choice(choices=("Hello",), correct_index=0))

    def test_multiple_choice_caps_the_choice_count(self) -> None:
        """Six is the ceiling: past that, the list stops being scannable on a phone,
        which defeats the point of a multiple-choice item."""
        choices = tuple(f"choice-{index}" for index in range(7))

        with pytest.raises(ValueError, match="at most 6"):
            Question(**_multiple_choice(choices=choices, correct_index=0))

    def test_the_answer_must_index_into_the_choices(self) -> None:
        with pytest.raises(ValueError, match="correct_index"):
            Question(**_multiple_choice(choices=("Hello", "Goodbye"), correct_index=5))

    def test_the_last_choice_can_be_the_answer(self) -> None:
        """Guards an off-by-one in the bound check: the upper limit is inclusive."""
        question = Question(**_multiple_choice(choices=("Hello", "Goodbye"), correct_index=1))

        assert question.correct_index == 1

    def test_multiple_choice_must_declare_an_answer(self) -> None:
        with pytest.raises(ValueError, match="correct_index"):
            Question(**_multiple_choice(correct_index=None))

    def test_true_false_needs_exactly_two_choices(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.TRUE_FALSE,
            choices=("True", "False", "Maybe"),
        )

        with pytest.raises(ValueError, match="exactly 2"):
            Question(**payload)

    def test_a_true_false_question_is_accepted(self) -> None:
        question = Question(
            **_multiple_choice(
                kind=QuestionKind.TRUE_FALSE,
                prompt="'Goodbye' means 'hello'.",
                choices=("True", "False"),
                correct_index=1,
            )
        )

        assert question.kind is QuestionKind.TRUE_FALSE

    def test_a_true_false_question_must_say_which_one_is_correct(self) -> None:
        """Two choices and no ``correct_index`` describes an unmarkable item.

        The choices can be right in any order, so a boolean question is not exempt from
        the rule that every question has to name its own answer.
        """
        payload = _multiple_choice(
            kind=QuestionKind.TRUE_FALSE,
            choices=("True", "False"),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="must set correct_index"):
            Question(**payload)

    def test_a_true_false_index_must_be_zero_or_one(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.TRUE_FALSE,
            choices=("True", "False"),
            correct_index=2,
        )

        with pytest.raises(ValueError, match="is not 0 or 1"):
            Question(**payload)

    def test_fill_blank_needs_at_least_one_accepted_answer(self) -> None:
        """A fill-in-the-blank with no answer key is unmarkable, so it is refused."""
        payload = _multiple_choice(
            kind=QuestionKind.FILL_BLANK,
            prompt="Complete: 'She ___ to school.'",
            choices=(),
            accepted_answers=(),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="accepted answer"):
            Question(**payload)

    def test_fill_blank_must_not_offer_choices(self) -> None:
        """Choices plus a free-text answer means two different ways to answer the same
        item, and the attempt engine would only score one of them."""
        payload = _multiple_choice(
            kind=QuestionKind.FILL_BLANK,
            choices=("goes", "go"),
            accepted_answers=("goes",),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="must not offer choices"):
            Question(**payload)

    def test_fill_blank_must_not_carry_a_choice_index(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.FILL_BLANK,
            choices=(),
            accepted_answers=("goes",),
            correct_index=0,
        )

        with pytest.raises(ValueError, match="correct_index"):
            Question(**payload)

    def test_a_fill_blank_question_is_accepted(self) -> None:
        question = Question(
            **_multiple_choice(
                kind=QuestionKind.FILL_BLANK,
                prompt="Complete: 'She ___ to school.'",
                choices=(),
                accepted_answers=("goes", "is going"),
                correct_index=None,
            )
        )

        assert question.accepted_answers == ("goes", "is going")

    def test_position_must_be_one_or_greater(self) -> None:
        with pytest.raises(ValueError, match="position"):
            Question(**_multiple_choice(position=0))

    def test_points_must_be_positive(self) -> None:
        """A zero-point question is invisible to any score-based unlock, so it can
        never be completed in a way that counts."""
        with pytest.raises(ValueError, match="points"):
            Question(**_multiple_choice(points=0))

    def test_accepted_answers_are_normalised_and_deduplicated(self) -> None:
        """Marking a free-text answer is case- and whitespace-insensitive, so the
        stored key is folded once at construction rather than on every comparison. A
        repeated answer is an authoring slip and must not inflate the count."""
        question = Question(
            **_multiple_choice(
                kind=QuestionKind.FILL_BLANK,
                choices=(),
                accepted_answers=(" Goes ", "GOING", "goes", "   "),
                correct_index=None,
            )
        )

        assert question.accepted_answers == ("goes", "going")
