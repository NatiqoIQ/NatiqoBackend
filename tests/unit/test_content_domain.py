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
from app.modules.content.domain.policy import pre_attempt_choices, pre_attempt_prompts


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
    """Walk a fresh aggregate to APPROVED â€” the last state before it may go live."""
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


def _matching(**overrides: Any) -> dict[str, Any]:
    """A complete, valid matching payload a test may spoil one field at a time.

    Written as pairs rather than as stored strings, the way the seed declares them, so a test
    can re-key the question without also keeping two lists in step by hand.
    """
    payload: dict[str, Any] = {
        "id": ContentId(new_id()),
        "lesson_id": ContentId(new_id()),
        "position": 1,
        "kind": QuestionKind.MATCHING,
        "skill": SkillKind.VOCABULARY,
        "prompt": "Match each animal to its Arabic word.",
        "choices": ("kelb", "qitt", "far", "hisan"),
        "accepted_answers": ("Dog => kelb", "Cat => qitt"),
        "correct_index": None,
        "explanation": "'Kelb' is a dog and 'qitt' is a cat.",
    }
    if "matches" in overrides:
        pairs: tuple[tuple[str, str], ...] = overrides.pop("matches")
        payload["choices"] = tuple(right for _, right in pairs) or payload["choices"]
        payload["accepted_answers"] = tuple(f"{left} => {right}" for left, right in pairs)
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
        straight live then the review state would be decoration â€” a single call would
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
        to fill in â€” the feedback surface is built from it."""
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

    def test_a_same_origin_image_path_is_accepted(self) -> None:
        """Content this service ships itself is referenced by a rooted path, so a path
        with no scheme is a valid aid rather than an authoring slip."""
        question = Question(**_multiple_choice(image_url="/media/images/hello.png"))

        assert question.image_url == "/media/images/hello.png"

    def test_a_same_origin_audio_path_is_accepted(self) -> None:
        question = Question(**_multiple_choice(audio_url="/media/audio/hello.mp3"))

        assert question.audio_url == "/media/audio/hello.mp3"

    def test_a_question_can_carry_a_video_aid(self) -> None:
        question = Question(**_multiple_choice(video_url="https://cdn.test/hello.mp4"))

        assert question.video_url == "https://cdn.test/hello.mp4"

    def test_the_video_aid_is_optional(self) -> None:
        assert Question(**_multiple_choice()).video_url is None

    def test_a_blank_video_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="video_url"):
            Question(**_multiple_choice(video_url="   "))

    def test_a_video_url_must_be_an_http_resource(self) -> None:
        with pytest.raises(ValueError, match="video_url"):
            Question(**_multiple_choice(video_url="cdn.test/hello.mp4"))

    def test_a_script_video_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="video_url"):
            Question(**_multiple_choice(video_url="javascript:alert(1)"))

    def test_a_same_origin_video_path_is_accepted(self) -> None:
        question = Question(**_multiple_choice(video_url="/media/video/hello.mp4"))

        assert question.video_url == "/media/video/hello.mp4"

    def test_a_protocol_relative_video_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="video_url"):
            Question(**_multiple_choice(video_url="//evil.test/hello.mp4"))
    def test_a_protocol_relative_media_url_is_refused(self) -> None:
        """``//host`` keeps the page's scheme but is a different origin, so it is not a
        same-origin path and must be refused like any other off-origin reference."""
        with pytest.raises(ValueError, match="image_url"):
            Question(**_multiple_choice(image_url="//evil.test/hello.png"))

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

    def test_a_multi_select_question_is_accepted(self) -> None:
        question = Question(
            **_multiple_choice(
                kind=QuestionKind.MULTI_SELECT,
                choices=("Hello", "Good morning", "Goodbye"),
                accepted_answers=("Hello", "Good morning"),
                correct_index=None,
            )
        )

        assert question.kind is QuestionKind.MULTI_SELECT
        assert question.accepted_answers == ("hello", "good morning")

    def test_multi_select_needs_at_least_two_correct_answers(self) -> None:
        """One correct option is a multiple-choice question wearing the wrong kind."""
        payload = _multiple_choice(
            kind=QuestionKind.MULTI_SELECT,
            choices=("Hello", "Goodbye"),
            accepted_answers=("Hello",),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="at least 2 correct"):
            Question(**payload)

    def test_multi_select_needs_at_least_two_choices(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.MULTI_SELECT,
            choices=("Hello",),
            accepted_answers=("Hello", "Good morning"),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="at least 2"):
            Question(**payload)

    def test_a_multi_select_answer_must_be_one_of_the_choices(self) -> None:
        """A keyed-correct option the learner cannot select is unanswerable."""
        payload = _multiple_choice(
            kind=QuestionKind.MULTI_SELECT,
            choices=("Hello", "Goodbye"),
            accepted_answers=("Hello", "Good morning"),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="not one of the choices"):
            Question(**payload)

    def test_multi_select_must_not_carry_a_choice_index(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.MULTI_SELECT,
            choices=("Hello", "Good morning", "Goodbye"),
            accepted_answers=("Hello", "Good morning"),
            correct_index=0,
        )

        with pytest.raises(ValueError, match="correct_index"):
            Question(**payload)

    def test_a_word_bank_question_is_accepted(self) -> None:
        """A word-bank item offers the words to type as tiles and is answered in free text."""
        question = Question(
            **_multiple_choice(
                kind=QuestionKind.WORD_BANK,
                choices=("morning", "night", "afternoon", "teacher"),
                accepted_answers=("Morning",),
                correct_index=None,
            )
        )

        assert question.kind is QuestionKind.WORD_BANK
        assert question.accepted_answers == ("morning",)

    def test_word_bank_needs_tiles_offered(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.WORD_BANK,
            choices=(),
            accepted_answers=("morning",),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="at least 2 and at most 6 tiles"):
            Question(**payload)

    def test_word_bank_needs_at_least_one_accepted_answer(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.WORD_BANK,
            choices=("morning", "night", "afternoon"),
            accepted_answers=(),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="at least one accepted answer"):
            Question(**payload)

    def test_an_accepted_answer_must_be_present_in_the_word_bank(self) -> None:
        """A key the bank never offers is a blank the learner cannot fill from the tiles."""
        payload = _multiple_choice(
            kind=QuestionKind.WORD_BANK,
            choices=("morning", "night", "afternoon"),
            accepted_answers=("evening",),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="missing from the word bank"):
            Question(**payload)

    def test_word_bank_must_not_carry_a_choice_index(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.WORD_BANK,
            choices=("morning", "night", "afternoon"),
            accepted_answers=("morning",),
            correct_index=0,
        )

        with pytest.raises(ValueError, match="correct_index"):
            Question(**payload)

    def test_an_ordering_question_is_accepted(self) -> None:
        """An ordering item stores its items in the correct order; the order is the key."""
        question = Question(
            **_multiple_choice(
                kind=QuestionKind.ORDERING,
                choices=("First", "Second", "Third"),
                accepted_answers=(),
                correct_index=None,
            )
        )

        assert question.kind is QuestionKind.ORDERING
        assert question.choices == ("First", "Second", "Third")

    def test_ordering_needs_at_least_two_items(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.ORDERING,
            choices=("First",),
            accepted_answers=(),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="at least 2 and at most 6 items"):
            Question(**payload)

    def test_ordering_must_not_carry_a_choice_index(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.ORDERING,
            choices=("First", "Second", "Third"),
            accepted_answers=(),
            correct_index=0,
        )

        with pytest.raises(ValueError, match="correct_index"):
            Question(**payload)

    def test_ordering_must_not_set_accepted_answers(self) -> None:
        payload = _multiple_choice(
            kind=QuestionKind.ORDERING,
            choices=("First", "Second", "Third"),
            accepted_answers=("First",),
            correct_index=None,
        )

        with pytest.raises(ValueError, match="accepted_answers"):
            Question(**payload)

    def test_a_matching_question_is_accepted(self) -> None:
        """A matching item stores its key as pairs and its ``choices`` as the bank to pair against."""
        question = Question(**_matching())

        assert question.kind is QuestionKind.MATCHING
        assert question.accepted_answers == ("Dog => kelb", "Cat => qitt")
        assert question.choices == ("kelb", "qitt", "far", "hisan")

    def test_a_matching_key_keeps_the_spelling_it_was_authored_in(self) -> None:
        """The one kind whose key is not folded at rest.

        A matching key is compared half by half rather than looked up whole, and those same
        halves are the prompts a learner reads â€” so folding them here would buy grading nothing
        and cost the screen its capitals. Tagging the rest of the question is unchanged, which
        the assertion about the prompt and the bank is here to keep honest.
        """
        question = Question(**_matching())

        assert question.accepted_answers[0].startswith("Dog")
        assert question.choices[0] == "kelb"

    def test_a_matching_question_needs_two_pairs(self) -> None:
        payload = _matching(matches=(("Dog", "kelb"),))

        with pytest.raises(ValueError, match="at least 2 and at most 4 pairs"):
            Question(**payload)

    def test_a_matching_question_stops_at_four_pairs(self) -> None:
        payload = _matching(
            matches=(
                ("Dog", "kelb"),
                ("Cat", "qitt"),
                ("Horse", "hisan"),
                ("Bird", "tayr"),
                ("Fish", "samak"),
            )
        )

        with pytest.raises(ValueError, match="at least 2 and at most 4 pairs"):
            Question(**payload)

    def test_a_matching_entry_must_be_written_as_a_pair(self) -> None:
        payload = _matching(matches=())
        payload["accepted_answers"] = ("Dog -> kelb", "Cat => qitt")

        with pytest.raises(ValueError, match="left => right"):
            Question(**payload)

    def test_a_matching_question_must_not_repeat_a_prompt(self) -> None:
        """Two keys competing for one prompt would make the pairing ambiguous."""
        payload = _matching(
            matches=(("Dog", "kelb"), ("Dog", "qitt")),
        )
        payload["choices"] = ("kelb", "qitt")

        with pytest.raises(ValueError, match="left-hand value"):
            Question(**payload)

    def test_a_matching_question_must_not_repeat_an_option(self) -> None:
        """Two prompts competing for one option is the mirror of the same ambiguity."""
        payload = _matching(
            matches=(("Dog", "kelb"), ("Cat", "kelb")),
        )
        payload["choices"] = ("kelb", "far")

        with pytest.raises(ValueError, match="right-hand value"):
            Question(**payload)

    def test_a_matching_answer_must_be_in_the_bank(self) -> None:
        """An answer the learner is never offered is a pair they cannot make."""
        payload = _matching(choices=("kelb", "far"))

        with pytest.raises(ValueError, match="not among the question's choices"):
            Question(**payload)

    def test_a_matching_bank_must_not_repeat_an_option(self) -> None:
        payload = _matching(choices=("kelb", "kelb", "qitt"))

        with pytest.raises(ValueError, match="repeat an option"):
            Question(**payload)

    def test_a_matching_question_needs_an_option_for_every_pair(self) -> None:
        payload = _matching(choices=("kelb", "qitt"))
        payload["accepted_answers"] = ("Dog => kelb", "Cat => qitt", "Horse => hisan")

        with pytest.raises(ValueError, match="one option for every pair"):
            Question(**payload)

    def test_matching_must_not_carry_a_choice_index(self) -> None:
        payload = _matching(correct_index=0)

        with pytest.raises(ValueError, match="correct_index"):
            Question(**payload)

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


class TestPreAttemptChoices:
    """Which choices a pre-attempt surface (the shared catalogue) may show.

    For most kinds the choices are options or tiles and reveal nothing; an ordering
    question is the exception, because its choices are stored in the answer order.
    """

    def test_ordinary_kinds_show_their_choices(self) -> None:
        for kind in (
            QuestionKind.MULTIPLE_CHOICE,
            QuestionKind.MULTI_SELECT,
            QuestionKind.TRUE_FALSE,
            QuestionKind.WORD_BANK,
        ):
            assert pre_attempt_choices(kind, ("a", "b", "c")) == ("a", "b", "c")

    def test_a_free_text_item_has_no_choices_to_show(self) -> None:
        assert pre_attempt_choices(QuestionKind.FILL_BLANK, ()) == ()

    def test_ordering_hides_its_choices_because_they_are_the_key(self) -> None:
        sequence = ("First", "Second", "Third", "Fourth")

        assert pre_attempt_choices(QuestionKind.ORDERING, sequence) == ()

    def test_matching_hides_its_bank_because_one_order_would_be_the_pairing(self) -> None:
        """The bank is not the key, but the order it is authored in usually is.

        A matching question is read as two columns side by side, so the arrangement to keep
        off a shared, cacheable read is the one where each option faces the prompt it answers â€”
        and that is the order the pairs were written in. Only a per-attempt shuffle can avoid
        it, which is what the quiz session has and the catalogue does not.
        """
        bank = ("kelb", "qitt", "far")

        assert pre_attempt_choices(QuestionKind.MATCHING, bank) == ()


class TestPreAttemptPrompts:
    """Which left-hand values a pre-attempt surface may show â€” and only matching has any.

    A prompt is half of an answer key and is still public, which looks like a contradiction
    until the question is asked properly: knowing that ``dog`` is to be paired with *something*
    says nothing about what, so the prompt teaches what is being asked while the pairing stays
    behind with the attempt engine.
    """

    def test_a_matching_question_shows_the_left_hand_halves_in_order(self) -> None:
        answers = ("Monday => 1", "Tuesday => 2", "Wednesday => 3")

        assert pre_attempt_prompts(QuestionKind.MATCHING, answers) == (
            "Monday",
            "Tuesday",
            "Wednesday",
        )

    def test_another_kind_has_no_prompts_to_show(self) -> None:
        """A fill-blank's accepted spellings are the key itself and must not be served."""
        for kind in QuestionKind:
            if kind is QuestionKind.MATCHING:
                continue

            assert pre_attempt_prompts(kind, ("goes", "going")) == ()

    def test_an_unparseable_entry_never_becomes_a_blank_prompt(self) -> None:
        assert pre_attempt_prompts(QuestionKind.MATCHING, ("Monday", "Tuesday => 2")) == (
            "Tuesday",
        )
