"""Seed the read-only learning catalogue.

    uv run python -m scripts.seed_content                # seed the curriculum
    uv run python -m scripts.seed_content --dry-run      # show the plan, write nothing
    uv run python -m scripts.seed_content --yes          # no interactive confirmation
    uv run python -m scripts.seed_content --include-drafts  # also seed unpublished rows

Why this is separate from ``scripts/seed.py``: that script owns *accounts*, and an account
is a row a person owns. This one owns *curriculum* — rows the editorial pipeline will later
edit through authoring commands that do not exist yet. Mixing them would mean a routine
``--yes`` to create a test learner could also rewrite published course material.

Three properties are enforced in code rather than left to the operator:

**Every id is derived, not minted.** Each row's primary key is ``uuid5`` of a fixed
namespace and its slug, so a rerun recognises its own rows and updates them in place
instead of duplicating the curriculum under fresh ids. Learners who bookmarked
``/levels/A1`` keep working; attempt history keyed on those ids stays attached.

**The domain validates the data, not this script.** Rows are built as ``Level``, ``Unit``,
``Lesson`` and ``Question`` entities first, so positions, question shapes and answer-key
normalisation are checked by ``domain/entities.py``. Only then is each entity written
through the ORM. A typo in this file fails here, loudly, instead of becoming a row the
catalogue will happily serve.

**A production environment is refused by default.** Seeding would overwrite real
curriculum, so the guard is on unless the operator passes ``--allow-production``.
``--dry-run`` is exempt: it only reads.

Writes go through the ORM models directly rather than a write port: this slice ships
read-only ports on purpose (see ``domain/ports.py``), and inventing a publish command to
serve a demo dataset would put an unaudited write path into a codebase that does not yet
have the editorial workflow to review one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.exc import DBAPIError

from app.core.config import Settings, get_settings
from app.core.database import dispose_engine, get_session_factory
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.types import ContentId, ContentState, QuestionKind
from app.modules.content.domain.entities import Lesson, Level, Question, Unit
from app.modules.content.domain.policy import MATCH_ARROW
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)
from scripts.seed_curriculum import CURRICULUM, SeedLesson, SeedLevel, SeedQuestion, SeedUnit

logger = get_logger("scripts.seed_content")

#: Fixed for the life of the project. Changing it would re-key the whole curriculum and
#: orphan every learner's progress, so it is a constant rather than a setting.
SEED_NAMESPACE = uuid.UUID("6f2b9d1e-0c4a-5f8b-9a3d-2e7c14b85f30")


class SeedRefused(RuntimeError):
    """The seed declined to run.

    Always raised before any write, and never swallowed: a script that quietly skips what
    it cannot do is worse than one that stops and says why.
    """


# The declaration of what the curriculum *is* lives in ``scripts.seed_curriculum``,
# next to the persona declarations for the same reason: that file is content, this one is
# the machinery that validates and writes it. Editing a lesson should never mean reading
# this file.
def _derive(*parts: str) -> uuid.UUID:
    """A stable id for one curriculum row.

    The slug is the identity, so the id follows a rename of the *title* but not of the
    slug — which is why slugs in :data:`CURRICULUM` are meant to be stable forever.
    """
    return uuid.uuid5(SEED_NAMESPACE, "/".join(parts))


@dataclass
class SeedCounts:
    levels: int = 0
    units: int = 0
    lessons: int = 0
    questions: int = 0

    @property
    def total(self) -> int:
        return self.levels + self.units + self.lessons + self.questions

    def __add__(self, other: SeedCounts) -> SeedCounts:
        return SeedCounts(
            levels=self.levels + other.levels,
            units=self.units + other.units,
            lessons=self.lessons + other.lessons,
            questions=self.questions + other.questions,
        )


@dataclass
class SeedReport:
    dry_run: bool
    written: SeedCounts = field(default_factory=SeedCounts)
    existing: SeedCounts = field(default_factory=SeedCounts)

    def format(self) -> str:
        if self.dry_run:
            verb = "would write"
        else:
            verb = "wrote"
        return "\n".join(
            (
                f"{verb} {self.written.total} rows "
                f"({self.written.levels} levels, {self.written.units} units, "
                f"{self.written.lessons} lessons, {self.written.questions} questions)",
                f"already present and updated in place: {self.existing.total}",
            )
        )


#: Unpublished rows are built by :func:`_draft_rows` rather than declared here, because a
#: draft lesson needs a unit id that only exists once the published tree has been built.


#: Where the unpublished demo rows hang off. Named rather than derived so they cannot
#: drift onto a different level or unit without a matching edit here.
_DRAFT_LEVEL_SLUG = "a1-starter"
_DRAFT_UNIT_SLUG = "a1-drafts"
_DRAFT_LESSON_UNIT_SLUG = "a1-greetings"
_DRAFT_LESSON_SLUG = "a1-greetings-hello-draft"


def _next_unit_position(level_slug: str) -> int:
    """The next free unit position inside a declared level."""
    for level in CURRICULUM:
        if level.slug == level_slug:
            return len(level.units) + 1
    raise SeedRefused(f"The drafts hang off level '{level_slug}', which is no longer declared.")


def _next_lesson_position(level_slug: str, unit_slug: str) -> int:
    """The next free lesson position inside a declared unit.

    Computed rather than hard-coded, and that is not a nicety: the draft lesson lives
    inside a unit that also holds *published* lessons, so a literal position stops being
    free the moment the curriculum grows past it. The result is a unique-constraint
    violation at flush time that only ``--include-drafts`` would ever reach.
    """
    for level in CURRICULUM:
        if level.slug != level_slug:
            continue
        for unit in level.units:
            if unit.slug == unit_slug:
                return len(unit.lessons) + 1
    raise SeedRefused(
        f"The draft lesson hangs off '{level_slug}/{unit_slug}', which is no longer declared."
    )


def _draft_rows(level_id: ContentId) -> tuple[Unit, Lesson]:
    """The unpublished demo rows.

    A review-state unit and a review-state lesson inside a *published* unit. The second is
    the interesting one: it proves that a learner can see the unit but not the lesson,
    which is the case a single "hide drafts" filter on the level alone would get wrong.
    """
    draft_unit = Unit(
        id=ContentId(_derive(_DRAFT_LEVEL_SLUG, _DRAFT_UNIT_SLUG)),
        level_id=level_id,
        title="Coming soon",
        description="Unpublished. Answers 404 on every public endpoint.",
        position=_next_unit_position(_DRAFT_LEVEL_SLUG),
        state=ContentState.REVIEW,
    )
    draft_lesson = Lesson(
        id=ContentId(_derive(_DRAFT_LEVEL_SLUG, _DRAFT_LESSON_UNIT_SLUG, _DRAFT_LESSON_SLUG)),
        unit_id=ContentId(_derive(_DRAFT_LEVEL_SLUG, _DRAFT_LESSON_UNIT_SLUG)),
        title="Saying hello (draft)",
        summary="An unpublished lesson inside a published unit.",
        position=_next_lesson_position(_DRAFT_LEVEL_SLUG, _DRAFT_LESSON_UNIT_SLUG),
        estimated_minutes=10,
        state=ContentState.REVIEW,
    )
    return draft_unit, draft_lesson


def _assert_writable(settings: Settings, *, allow_production: bool, dry_run: bool) -> None:
    """Refuse to write to production unless told otherwise."""
    if settings.is_production and not allow_production and not dry_run:
        raise SeedRefused(
            "Refusing to seed the curriculum of a production environment. Re-run with "
            "--allow-production if you are certain."
        )


def _build_levels() -> tuple[Level, ...]:
    """Turn the declaration above into validated domain entities."""
    return tuple(
        Level(
            id=ContentId(_derive(level.slug)),
            code=level.code,
            title=level.title,
            description=level.description,
            position=position,
            state=ContentState.PUBLISHED,
        )
        for position, level in enumerate(CURRICULUM, start=1)
    )


def _build_units(level: SeedLevel, level_id: ContentId) -> tuple[Unit, ...]:
    return tuple(
        Unit(
            id=ContentId(_derive(level.slug, unit.slug)),
            level_id=level_id,
            title=unit.title,
            description=unit.description,
            position=position,
            state=ContentState.PUBLISHED,
        )
        for position, unit in enumerate(level.units, start=1)
    )


def _build_lessons(level: SeedLevel, unit: SeedUnit, unit_id: ContentId) -> tuple[Lesson, ...]:
    return tuple(
        Lesson(
            id=ContentId(_derive(level.slug, unit.slug, lesson.slug)),
            unit_id=unit_id,
            title=lesson.title,
            summary=lesson.summary,
            position=position,
            estimated_minutes=lesson.estimated_minutes,
            state=ContentState.PUBLISHED,
        )
        for position, lesson in enumerate(unit.lessons, start=1)
    )


def _key_of(question: SeedQuestion) -> tuple[str, ...]:
    """The answer key to build a declared question with.

    Only a matching question is anything other than a copy of ``accepted_answers``: its pairs
    are declared as pairs and written out here in the stored ``"left => right"`` form, so the
    declaration never spells the separator itself and the two halves of a match cannot be
    separated by an edit to one list. Declaring *both* a pair list and a key is refused rather
    than resolved, because either reading would be a guess about which one the author meant.
    """
    if question.matches and question.kind is not QuestionKind.MATCHING:
        raise SeedRefused(
            f"Question '{question.slug}' declares matches but is a {question.kind.value} "
            "question; pairs belong to a matching question."
        )
    if question.matches and question.accepted_answers:
        raise SeedRefused(
            f"Question '{question.slug}' declares both matches and accepted_answers; a "
            "matching question's key is its pairs."
        )
    if question.matches:
        return tuple(f"{left} {MATCH_ARROW} {right}" for left, right in question.matches)
    return question.accepted_answers


def _build_questions(
    level: SeedLevel, unit: SeedUnit, lesson: SeedLesson, lesson_id: ContentId
) -> tuple[Question, ...]:
    return tuple(
        Question(
            id=ContentId(_derive(level.slug, unit.slug, lesson.slug, question.slug)),
            lesson_id=lesson_id,
            position=position,
            kind=question.kind,
            skill=question.skill,
            prompt=question.prompt,
            choices=question.choices,
            accepted_answers=_key_of(question),
            correct_index=question.correct_index,
            explanation=question.explanation or "",
            image_url=question.image_url,
            audio_url=question.audio_url,
            points=question.points,
        )
        for position, question in enumerate(lesson.questions, start=1)
    )


async def _upsert(session: object, model: type, entity: object, values: dict) -> bool:
    """Insert or update one row by primary key. Returns ``True`` when it was created.

    An update rather than insert-or-skip, deliberately: editing this file and rerunning it
    should converge on the edited curriculum. Skip-if-exists would leave the old wording
    in the database and make the script look broken.
    """
    primary_key = entity.id  # type: ignore[attr-defined]
    existing = await session.get(model, primary_key)  # type: ignore[attr-defined]
    if existing is None:
        session.add(model(id=primary_key, **values))  # type: ignore[attr-defined]
        return True
    for column, value in values.items():
        setattr(existing, column, value)
    return False


def _build_tree() -> tuple[
    tuple[Level, ...], tuple[Unit, ...], tuple[Lesson, ...], tuple[Question, ...]
]:
    """Build the whole published tree as validated domain entities, in one pass.

    Every entity is constructed rather than assembled from raw values, so
    ``__post_init__`` rejects a bad position or an impossible question before a single
    row is written. A mistake in this file is therefore a crash here, not a row the
    catalogue happily serves to a learner.
    """
    level_entities = _build_levels()
    unit_entities: list[Unit] = []
    lesson_entities: list[Lesson] = []
    question_entities: list[Question] = []

    for level, level_entity in zip(CURRICULUM, level_entities, strict=True):
        units = _build_units(level, level_entity.id)
        unit_entities.extend(units)
        for unit, unit_entity in zip(level.units, units, strict=True):
            lessons = _build_lessons(level, unit, unit_entity.id)
            lesson_entities.extend(lessons)
            for lesson, lesson_entity in zip(unit.lessons, lessons, strict=True):
                question_entities.extend(_build_questions(level, unit, lesson, lesson_entity.id))

    return (
        tuple(level_entities),
        tuple(unit_entities),
        tuple(lesson_entities),
        tuple(question_entities),
    )


async def run_seed(*, dry_run: bool, include_drafts: bool) -> SeedReport:
    """Write the curriculum, or report what would be written."""
    report = SeedReport(dry_run=dry_run)

    levels, units, lessons, questions = _build_tree()

    if include_drafts:
        draft_unit, draft_lesson = _draft_rows(levels[0].id)
        units = (*units, draft_unit)
        lessons = (*lessons, draft_lesson)

    if dry_run:
        report.written = SeedCounts(
            levels=len(levels),
            units=len(units),
            lessons=len(lessons),
            questions=len(questions),
        )
        return report

    session_factory = get_session_factory()
    async with session_factory() as session:
        for level_entity in levels:
            created = await _upsert(
                session,
                LevelModel,
                level_entity,
                {
                    "code": level_entity.code.value,
                    "title": level_entity.title,
                    "description": level_entity.description,
                    "position": level_entity.position,
                    "state": level_entity.state.value,
                },
            )
            _tally(report, created, levels=1)

        for unit_entity in units:
            created = await _upsert(
                session,
                UnitModel,
                unit_entity,
                {
                    "level_id": unit_entity.level_id,
                    "title": unit_entity.title,
                    "description": unit_entity.description,
                    "position": unit_entity.position,
                    "state": unit_entity.state.value,
                },
            )
            _tally(report, created, units=1)

        for lesson_entity in lessons:
            created = await _upsert(
                session,
                LessonModel,
                lesson_entity,
                {
                    "unit_id": lesson_entity.unit_id,
                    "title": lesson_entity.title,
                    "summary": lesson_entity.summary,
                    "position": lesson_entity.position,
                    "estimated_minutes": lesson_entity.estimated_minutes,
                    "state": lesson_entity.state.value,
                },
            )
            _tally(report, created, lessons=1)

        for question_entity in questions:
            created = await _upsert(
                session,
                QuestionModel,
                question_entity,
                {
                    "lesson_id": question_entity.lesson_id,
                    "position": question_entity.position,
                    "kind": question_entity.kind.value,
                    "skill": question_entity.skill.value,
                    "prompt": question_entity.prompt,
                    "choices": list(question_entity.choices),
                    "accepted_answers": list(question_entity.accepted_answers),
                    "correct_index": question_entity.correct_index,
                    "explanation": question_entity.explanation,
                    "image_url": question_entity.image_url,
                    "audio_url": question_entity.audio_url,
                    "points": question_entity.points,
                },
            )
            _tally(report, created, questions=1)

        await session.commit()

    logger.info("curriculum_seeded", written=report.written.total, updated=report.existing.total)
    return report


def _tally(report: SeedReport, created: bool, **counts: int) -> None:
    """Add one row to the written or already-present side of the report."""
    if created:
        report.written = report.written + SeedCounts(**counts)
    else:
        report.existing = report.existing + SeedCounts(**counts)


# --------------------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed_content",
        description="Load the demo learning catalogue into this environment.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation.")
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="Permit seeding a production environment. Off by default.",
    )
    parser.add_argument(
        "--include-drafts",
        action="store_true",
        help="Also seed unpublished rows, for exercising the 404 paths by hand.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    return parser


def _confirm(plan: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    # Refusing when there is no terminal is the safe reading: a seed that ran unattended
    # in CI should be a deliberate --yes, not a surprise.
    if not sys.stdin.isatty():
        print("Refusing to write without confirmation. Re-run with --yes.", file=sys.stderr)
        return False
    print(plan)
    try:
        answer = input("Write these rows? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    # Checked before the prompt, not after: an operator who confirmed a plan and is then
    # refused has been asked to agree to something that will not happen.
    _assert_writable(settings, allow_production=args.allow_production, dry_run=args.dry_run)

    try:
        preview = await run_seed(dry_run=True, include_drafts=args.include_drafts)
        if args.dry_run:
            print(preview.format())
            return 0
        if not _confirm(preview.format(), assume_yes=args.yes):
            print("Nothing written.", file=sys.stderr)
            return 1
        report = await run_seed(dry_run=False, include_drafts=args.include_drafts)
        print(report.format())
        return 0
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except SeedRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    except AppError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    except DBAPIError as exc:
        print(
            f"Database error: {exc}\n"
            "If the schema is missing, run `uv run alembic upgrade head` first.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
