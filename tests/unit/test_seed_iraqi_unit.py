"""The Iraqi flavour unit's contract.

``scripts/seed_curriculum.py`` is data, so nothing imports these questions except the
seeder — which means a typo there is invisible until someone opens the app. This test
pins the shape that makes the unit usable: the slug is the row's identity (renaming it
orphans progress), the two lessons exist, every question carries a non-empty
explanation, and the explanations are actually in Arabic script (the Iraqi-dialect
promise), not accidentally left in English.

It also guards the two grading invariants the seeder trusts: a choice question has a
``correct_index``, and a blank question has an accepted spelling.
"""

from __future__ import annotations

import re

import pytest

from app.core.types import QuestionKind
from scripts.seed_curriculum import CURRICULUM

_ARABIC = re.compile(r"[\u0600-\u06FF]")


@pytest.fixture(scope="module")
def iraq_unit():
    a1 = CURRICULUM[0]
    units = [unit for unit in a1.units if unit.slug == "a1-everyday-iraq"]
    assert len(units) == 1, "the unit must exist exactly once in A1"
    return units[0]


def test_unit_is_stable_and_has_two_lessons(iraq_unit) -> None:
    assert iraq_unit.title == "Everyday Iraq"
    assert [lesson.slug for lesson in iraq_unit.lessons] == [
        "a1-everyday-iraq-iraqi-foods",
        "a1-everyday-iraq-daily-words",
    ]


def test_every_explanation_is_written_in_arabic(iraq_unit) -> None:
    for lesson in iraq_unit.lessons:
        for question in lesson.questions:
            assert question.explanation, f"missing explanation: {question.slug}"
            assert _ARABIC.search(question.explanation), (
                f"explanation is not in Arabic: {question.slug}"
            )


def test_questions_are_gradeable(iraq_unit) -> None:
    for lesson in iraq_unit.lessons:
        for question in lesson.questions:
            if question.kind in (QuestionKind.MULTIPLE_CHOICE, QuestionKind.TRUE_FALSE):
                assert question.correct_index is not None, question.slug
                assert 0 <= question.correct_index < len(question.choices), question.slug
            if question.kind is QuestionKind.FILL_BLANK:
                assert question.accepted_answers, question.slug


def test_the_foods_lesson_names_iraqi_dishes(iraq_unit) -> None:
    """The point of the lesson is Iraqi food, so at least the headliners are present."""
    text = " ".join(
        " ".join(
            (question.prompt, *question.choices, *question.accepted_answers, question.explanation)
        )
        for question in iraq_unit.lessons[0].questions
    ).lower()
    for dish in ("masgoof", "kubba", "dolma", "kahi"):
        assert dish in text, f"missing dish: {dish}"


def test_every_iraqi_unit_carries_arabic_explanations() -> None:
    """All Everyday-Iraq units share one promise: English prompts, Iraqi explanations."""
    iraqi = [
        unit
        for level in CURRICULUM
        for unit in level.units
        if "iraqi" in unit.slug or unit.slug == "a1-everyday-iraq"
    ]
    assert len(iraqi) == 10
    for unit in iraqi:
        assert len(unit.lessons) == 2, unit.slug
        for lesson in unit.lessons:
            for question in lesson.questions:
                assert _ARABIC.search(question.explanation), question.slug
