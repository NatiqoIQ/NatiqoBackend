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

#: A multi-select item with a single correct option is a multiple-choice item in disguise, so
#: at least two are required for the kind to mean what it says.
MIN_MULTI_SELECT_ANSWERS = 2

#: The separator a matching pair is stored with, as in ``"dog => kelb"``. One arrow and not a
#: second list: a pair travels as a single value, so the two halves of a match cannot drift
#: apart, and the kind needs no column of its own.
MATCH_ARROW = "=>"

#: A matching question pairs two to four items. Fewer than two is not a matching exercise, and
#: past four the two columns stop fitting on one phone screen without scrolling — which is the
#: whole point of asking a learner to pair them by eye.
MIN_PAIRS = 2
MAX_PAIRS = 4


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


def parse_pair(raw: str) -> tuple[str, str] | None:
    """Split a stored matching pair, or ``None`` when the text is not one.

    Only the *first* arrow splits, so a right-hand value that happens to contain ``=>``
    survives intact. Both halves are stripped and neither may be blank: ``"dog =>"`` is not a
    pair, and storing it would leave a prompt with nothing to match against.

    The halves come back in the spelling they were authored in — ``"Monday "`` is capitalised
    because a learner reads it — because a matching key is *not* folded at storage the way a
    free-text one is. Marking folds both halves at comparison time instead, so the stored form
    is free to stay readable.
    """
    left, arrow, right = raw.partition(MATCH_ARROW)
    if not arrow:
        return None
    left, right = left.strip(), right.strip()
    if not left or not right:
        return None
    return left, right


def matching_prompts(answers: Sequence[str]) -> tuple[str, ...]:
    """The left-hand values of stored matching pairs, in the order they were authored.

    Only the halves that parse are returned, so a malformed entry cannot become a blank
    prompt; a question that reached the catalogue has already been through
    :func:`question_shape_problems`, which rejects those outright.
    """
    return tuple(pair[0] for pair in (parse_pair(answer) for answer in answers) if pair is not None)


def dedupe_strings(values: Sequence[str]) -> tuple[str, ...]:
    """Drop exactly repeated entries, keeping the first and the spelling it was written in.

    Order is part of the value for the kinds that use this: a matching question's pairs are
    served as its prompts in the order they appear, so a set would not do. Only an entry
    repeated character for character is dropped, which is what makes this the safe half of
    normalisation for a key that is not compared as one string.
    """
    seen: dict[str, None] = {}
    for value in values:
        if value.strip():
            seen.setdefault(value, None)
    return tuple(seen)


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
    """Why an optional media aid is unusable, or an empty list when it is fine.

    The image and audio aids are both optional, so ``None`` passes. When one is set it
    has to point at a resource a client can actually fetch: either an absolute
    ``http(s)`` URL, or a same-origin path (``/media/...``) for content this service
    ships itself. Anything else — a blank string, a bare path with no leading slash, a
    protocol-relative ``//host`` (which silently changes origin), a ``javascript:`` URL
    — renders as a broken image or a silent player on the learner's screen rather than
    as a caught authoring error.
    """
    if value is None:
        return []
    if not value.strip():
        return [f"The {field} must not be blank when it is set."]
    if value.startswith(("http://", "https://")):
        return []
    # Same-origin asset: a single leading slash, not a protocol-relative ``//host``, and
    # no backslash (which a filesystem-backed static handler could read as a separator).
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return []
    return [f"The {field} must be an http(s) URL or a same-origin path, got {value!r}."]


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

    elif kind is QuestionKind.MULTI_SELECT:
        # A multi-select is the same discrimination test as a multiple choice, but with more
        # than one right answer. Fewer than two correct options would just be a single-choice
        # question wearing the wrong kind, and an option keyed correct that is not among the
        # offered choices is unanswerable — the learner could never select it.
        if not MIN_CHOICES <= len(choices) <= MAX_CHOICES:
            problems.append(
                f"A multi_select question needs at least {MIN_CHOICES} and at most "
                f"{MAX_CHOICES} choices, got {len(choices)}."
            )
        if len(accepted_answers) < MIN_MULTI_SELECT_ANSWERS:
            problems.append(
                f"A multi_select question needs at least {MIN_MULTI_SELECT_ANSWERS} correct "
                f"answers, got {len(accepted_answers)}."
            )
        offered = {choice.strip().lower() for choice in choices}
        for answer in accepted_answers:
            if answer not in offered:
                problems.append(f"The correct answer {answer!r} is not one of the choices.")
        if correct_index is not None:
            problems.append(
                "A multi_select question must not set correct_index; use accepted_answers."
            )

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

    elif kind is QuestionKind.WORD_BANK:
        # A word-bank item is answered in free text, but unlike ``fill_blank`` it offers the
        # words to type as tiles. So ``choices`` is a bank, not a set of options: every
        # accepted answer has to appear among the tiles or the learner could never assemble
        # it, while distractors are expected and fine.
        if not MIN_CHOICES <= len(choices) <= MAX_CHOICES:
            problems.append(
                f"A word_bank question needs at least {MIN_CHOICES} and at most "
                f"{MAX_CHOICES} tiles, got {len(choices)}."
            )
        if correct_index is not None:
            problems.append(
                "A word_bank question must not set correct_index; use accepted_answers."
            )
        if not accepted_answers:
            problems.append("A word_bank question needs at least one accepted answer.")
        offered = {choice.strip().lower() for choice in choices}
        for answer in accepted_answers:
            if answer not in offered:
                problems.append(f"The accepted answer {answer!r} is missing from the word bank.")

    elif kind is QuestionKind.ORDERING:
        # In an ordering question the items *are* the key: ``choices`` is stored in the
        # correct order and served shuffled, so the index of an item is its correct position
        # and there is nothing for ``accepted_answers`` to add. Fewer than two items is not a
        # sequence to reconstruct.
        if not MIN_CHOICES <= len(choices) <= MAX_CHOICES:
            problems.append(
                f"An ordering question needs at least {MIN_CHOICES} and at most "
                f"{MAX_CHOICES} items, got {len(choices)}."
            )
        if correct_index is not None:
            problems.append(
                "An ordering question must not set correct_index; the item order is the key."
            )
        if accepted_answers:
            problems.append(
                "An ordering question must not set accepted_answers; the item order is the key."
            )

    elif kind is QuestionKind.MATCHING:
        # A matching item is stored as pairs, not as two parallel lists: ``accepted_answers``
        # carries the ``"left => right"`` keys, and ``choices`` is the bank of right-hand
        # values the learner pairs them with — distractors welcome, duplicates not.
        #
        # Both halves have to be distinct, and that is a grading rule rather than a taste one:
        # a repeated left would leave two keys competing for one prompt, and a repeated right
        # would leave two prompts competing for one option, so a learner could be marked wrong
        # for a pairing their own screen made indistinguishable.
        pairs = [parse_pair(answer) for answer in accepted_answers]
        if not MIN_PAIRS <= len(pairs) <= MAX_PAIRS:
            problems.append(
                f"A matching question needs at least {MIN_PAIRS} and at most {MAX_PAIRS} "
                f"pairs, got {len(pairs)}."
            )
        for answer, pair in zip(accepted_answers, pairs, strict=True):
            if pair is None:
                problems.append(f"The matching pair {answer!r} is not written as 'left => right'.")
        lefts = [pair[0] for pair in pairs if pair is not None]
        rights = [pair[1] for pair in pairs if pair is not None]
        for side, values in (("left", lefts), ("right", rights)):
            if len({value.lower() for value in values}) != len(values):
                problems.append(
                    f"A matching question must not repeat a {side}-hand value; a duplicate "
                    "makes the pairing ambiguous."
                )
        if correct_index is not None:
            problems.append(
                "A matching question must not set correct_index; the pairs are the key."
            )
        if not MIN_CHOICES <= len(choices) <= MAX_CHOICES:
            problems.append(
                f"A matching question needs at least {MIN_CHOICES} and at most "
                f"{MAX_CHOICES} options to pair with, got {len(choices)}."
            )
        offered = {choice.strip().lower() for choice in choices}
        if len(offered) != len(choices):
            problems.append("A matching question must not repeat an option in its choices.")
        if len(choices) < len(pairs):
            problems.append("A matching question needs at least one option for every pair.")
        for right in rights:
            if right.lower() not in offered:
                problems.append(
                    f"The matching answer {right!r} is not among the question's choices."
                )

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


def pre_attempt_choices(kind: QuestionKind, choices: Sequence[str]) -> tuple[str, ...]:
    """The choices a *pre-attempt* surface may show a learner for a kind.

    Most kinds may show their choices freely — for a multiple choice they are the options
    to weigh, for a word bank the tiles to assemble from, and neither reveals the key. Two
    kinds do not:

    * An **ordering** question stores its ``choices`` in the correct sequence, so showing
      them in that order *is* handing over the answer.
    * A **matching** question stores its ``choices`` as the bank to pair against. The bank is
      only harmless once it is served in an order that does not line each option up with the
      prompt it answers, which takes a shuffle; the order it is stored in is usually the
      order it was authored in, which is the order of the pairs.

    The public catalogue is a shared, cacheable read with no per-learner seed to reshuffle
    against, so both kinds show nothing here, and the answerable, shuffled columns are the
    quiz session's job.

    Kept in the domain rather than at the projection site so the rule sits with the shape
    rules it guards, and so a future pre-attempt surface reuses one verdict.
    """
    if kind in (QuestionKind.ORDERING, QuestionKind.MATCHING):
        return ()
    return tuple(choices)


def matching_answer_positions(
    kind: QuestionKind, choices: Sequence[str], accepted_answers: Sequence[str]
) -> tuple[int | None, ...]:
    """For each prompt of a matching question, where the option that answers it sits.

    The pairing read backwards, and the one fact a presentation layer needs to stop the two
    columns from spelling out which prompt goes with which option: a matching question is
    served as two lists side by side, so the arrangement to avoid is the one where the option
    that answers the prompt at position *i* is itself at position *i*. That is the authored
    order, which is exactly what an unshuffled list — or a shuffle that happened to come out
    even — would reproduce.

    ``None`` marks a prompt whose option is not in the bank, which leaves that position
    unconstrained rather than making the question unservable. An authored question cannot
    reach that state: ``question_shape_problems`` requires every answer to be offered.
    """
    if kind is not QuestionKind.MATCHING:
        return ()
    offered = {choice.strip().lower(): index for index, choice in enumerate(choices)}
    positions: list[int | None] = []
    for answer in accepted_answers:
        pair = parse_pair(answer)
        positions.append(offered.get(pair[1].lower()) if pair is not None else None)
    return tuple(positions)


def pre_attempt_prompts(kind: QuestionKind, accepted_answers: Sequence[str]) -> tuple[str, ...]:
    """The prompts a *pre-attempt* surface may show, and only a matching question has any.

    A matching question's prompts are halves of its answer key, which makes them look like
    key material — they are not. Knowing that ``dog`` is to be paired with something says
    nothing about *what*, so the prompts are public and the pairing is the secret; the bank
    they are paired against is the half a shift in order could give away, and is handled by
    :func:`pre_attempt_choices`.
    """
    if kind is not QuestionKind.MATCHING:
        return ()
    return matching_prompts(accepted_answers)


def assert_transition_allowed(current: ContentState, target: ContentState) -> None:
    """Refuse an editorial move the lifecycle does not allow.

    Skipping review is exactly the mistake a two-person editorial process exists to
    prevent, so the aggregate refuses it rather than trusting its caller.
    """
    if not current.can_transition_to(target):
        raise InvalidContentState(f"Cannot move content from {current.value} to {target.value}.")
