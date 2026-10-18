"""Row → aggregate mappers.

Hand-written rather than generated, matching the rest of the project. The point is that
the SQL representation is allowed to change shape without the domain noticing: a
``choices`` JSON array, a ``state`` string, a ``position`` integer are all storage
details, and the aggregates above should not have to care.

Conversion happens in one direction only. Writing a catalogue row is the authoring
slice's job; this slice only reads.
"""

from __future__ import annotations

from app.core.types import CEFRLevel, ContentId, ContentState, QuestionKind, SkillKind
from app.modules.content.domain.entities import Lesson, Level, Question, Unit
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)


def to_level(model: LevelModel) -> Level:
    return Level(
        id=ContentId(model.id),
        code=CEFRLevel(model.code),
        title=model.title,
        position=model.position,
        description=model.description,
        state=ContentState(model.state),
    )


def to_unit(model: UnitModel) -> Unit:
    return Unit(
        id=ContentId(model.id),
        level_id=ContentId(model.level_id),
        title=model.title,
        position=model.position,
        description=model.description,
        state=ContentState(model.state),
    )


def to_lesson(model: LessonModel) -> Lesson:
    return Lesson(
        id=ContentId(model.id),
        unit_id=ContentId(model.unit_id),
        title=model.title,
        summary=model.summary,
        position=model.position,
        estimated_minutes=model.estimated_minutes,
        state=ContentState(model.state),
    )


def to_question(model: QuestionModel) -> Question:
    return Question(
        id=ContentId(model.id),
        lesson_id=ContentId(model.lesson_id),
        position=model.position,
        kind=QuestionKind(model.kind),
        skill=SkillKind(model.skill),
        prompt=model.prompt,
        choices=tuple(model.choices or ()),
        accepted_answers=tuple(model.accepted_answers or ()),
        correct_index=model.correct_index,
        explanation=model.explanation or "",
        image_url=model.image_url,
        audio_url=model.audio_url,
        video_url=model.video_url,
        points=model.points,
    )
