"""What makes authored content valid, and what the editorial lifecycle allows.

These rules are kept out of the entities so that the same checks can be reused by the
authoring slice and by the seed script without having to construct a throwaway object
just to have it validated.

Nothing here imports a framework. The application layer translates
:class:`InvalidContentState` into a 409 and the shape problems into a 422, so the
domain stays free of any notion of HTTP.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.types import ContentState, QuestionKind

#: A multiple-choice item with fewer than two options is not a discrimination test, and
#: one with more than six stops being scannable on a phone — which is the whole point of
#: asking a learner to pick.
MIN_CHOICES = 2
MAX_CHOICES = 6

#: A true/false item is exactly this many options by definition.
TRUE_FALSE_CHOICES = 2


class InvalidContentState(ValueError):
    """An editorial move the lifecycle forbids.

    Raised both for illegal transitions (``DRAFT`` straight to ``PUBLISHED``) and for
    publishing a child whose parent is not published. Both are the same kind of
    mistake — a piece of content becoming visible that should not be reachable yet.
    """


def assert_valid_position(position: int) -> None:
    """Reject positions below 1.

    Ordering starts at 1. A 0 would sort ahead of every real position and make "the
    first unit" ambiguous the moment a second sibling was added.
    """
    if position < 1:
        raise ValueError(f"position must be one or greater, got {position}.")


def normalise_accepted_answers(answers: Sequence[str]) -> tuple[str, ...]:
    """Fold free-text answer keys to a comparable form.

    Marking a typed answer is case- and whitespace-insensitive, so the key is
    normalised once here rather than on every comparison during marking. Duplicates
    are dropped: a second copy of the same answer is an authoring slip, and keeping it
    would make the accepted-answer count lie.
    """
    seen: dict[str, None] = {}
    for answer in answers:
        key = answer.strip().lower()
        if key:
            seen.setdefault(key, None)
    return tuple(seen)


def _media_url_problems(field: str, value: str | None) -> list[str]:
    """Why an optional media URL is unusable, or an empty list when it is fine.

    The image and audio aids are both optional, so ``None`` passes. When one is set it
    has to point at a resource a client can actually fetch: a blank string or a
    non-http(s) scheme (a bare path, a ``javascript:`` URL) renders as a broken image or
    a silent player on the learner's screen rather than as a caught authoring error.
    """
    if value is None:
        return []
    if not value.strip():
        return [f"The {field} must not be blank when it is set."]
    if not value.startswith(("http://", "https://")):
        return [f"The {field} must be an http(s) resource, got {value!r}."]
    return []


def question_shape_problems(
    kind: QuestionKind,
    *,
    choices: Sequence[str],
    accepted_answers: Sequence[str],
    correct_index: int | None,
    explanation: str | None,
    image_url: str | None,
    audio_url: str | None,
) -> tuple[str, ...]:
    """Every reason this question could not be answered, in report order.

    All problems are returned rather than just the first so an authoring form can mark
    every field that needs fixing in one pass.
    """
    problems: list[str] = []

    # The product rule is "a question carries an explanation, and may also carry media
    # aids". The image and audio aids are optional pre-attempt supports that illustrate
    # or pronounce the taught word or sentence; neither is a substitute for the
    # explanation. The explanation stays mandatory because it is the post-attempt
    # rationale — without it a learner answers and gets no feedback.
    if not explanation or not explanation.strip():
        problems.append("A question must carry an explanation.")

    # Both aids are optional, so ``None`` passes; see ``_media_url_problems`` for why a
    # blank or non-http(s) value is rejected rather than stored.
    problems.extend(_media_url_problems("image_url", image_url))
    problems.extend(_media_url_problems("audio_url", audio_url))

    if kind is QuestionKind.MULTIPLE_CHOICE:
        if not MIN_CHOICES <= len(choices) <= MAX_CHOICES:
            problems.append(
                f"A multiple_choice question needs at least {MIN_CHOICES} and at most "
                f"{MAX_CHOICES} choices, got {len(choices)}."
            )
        if correct_index is None:
            problems.append("A multiple_choice question must set correct_index.")
        elif not 0 <= correct_index < len(choices):
            problems.append(f"correct_index {correct_index} is outside the {len(choices)} choices.")

    elif kind is QuestionKind.TRUE_FALSE:
        if len(choices) != TRUE_FALSE_CHOICES:
            problems.append(
                f"A true_false question must offer exactly {TRUE_FALSE_CHOICES} choices, "
                f"got {len(choices)}."
            )
        if correct_index is None:
            problems.append("A true_false question must set correct_index.")
        elif not 0 <= correct_index < TRUE_FALSE_CHOICES:
            problems.append(f"correct_index {correct_index} is not 0 or 1.")

    else:  # QuestionKind.FILL_BLANK
        if choices:
            problems.append(
                "A fill_blank question must not offer choices; it is answered in free text."
            )
        if correct_index is not None:
            problems.append(
                "A fill_blank question must not set correct_index; use accepted_answers."
            )
        if not accepted_answers:
            problems.append("A fill_blank question needs at least one accepted answer.")

    return tuple(problems)


def assert_transition_allowed(current: ContentState, target: ContentState) -> None:
    """Refuse an editorial move the lifecycle does not allow.

    Skipping review is exactly the mistake a two-person editorial process exists to
    prevent, so the aggregate refuses it rather than trusting its caller.
    """
    if not current.can_transition_to(target):
        raise InvalidContentState(f"Cannot move content from {current.value} to {target.value}.")
