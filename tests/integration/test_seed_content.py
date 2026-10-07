"""Curriculum seeder tests.

These run against a real SQLite schema, because most of what this seeder claims is a
property of the *database* rather than of the Python: derived ids have to be unique,
a second run has to write nothing at all, and the unpublished demo rows have to land on
positions the published tree has not already taken.

The last one is why this file exists. A draft lesson is written into a unit that already
holds published lessons, so any position written as a literal stops being free the moment
the curriculum grows past it — a unique-constraint violation at flush time that nothing
but ``--include-drafts`` would ever reach. The fix was to compute the position from the
declaration, and the test that would have caught it is
:func:`test_drafts_land_after_the_published_rows_without_colliding`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.registry import import_all_models
from app.core.types import CEFRLevel, ContentState
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)
from scripts.seed_content import (
    _DRAFT_LESSON_UNIT_SLUG,
    _DRAFT_LEVEL_SLUG,
    _build_tree,
    _next_lesson_position,
    _next_unit_position,
    run_seed,
)
from scripts.seed_curriculum import CURRICULUM

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def schema() -> AsyncIterator[None]:
    """A fresh schema. ``run_seed`` builds its own session, so nothing else is needed."""
    import_all_models()
    await create_all()
    yield
    await dispose_engine()


async def _counts(session: AsyncSession) -> dict[str, int]:
    """Committed row counts, read through a connection of its own.

    Reading through the seeder's session would pass even if nothing had been committed,
    which is the one failure an idempotency claim must not be able to hide.
    """
    models: tuple[tuple[str, Any], ...] = (
        ("levels", LevelModel),
        ("units", UnitModel),
        ("lessons", LessonModel),
        ("questions", QuestionModel),
    )
    return {
        name: int(await session.scalar(select(func.count()).select_from(model)) or 0)
        for name, model in models
    }


def _declared_total() -> int:
    levels, units, lessons, questions = _build_tree()
    return len(levels) + len(units) + len(lessons) + len(questions)


# ===================================================================== the declaration
def test_every_derived_id_is_unique() -> None:
    """Two rows sharing a slug path would silently be one row, so ids are checked.

    Every id in the tree is ``uuid5`` of a slug path, so a duplicated slug inside one
    parent produces a duplicate primary key — the insert would fail, or worse, update the
    wrong row. Asserting on the ids catches it at test time instead.
    """
    levels, units, lessons, questions = _build_tree()
    every_id = [entity.id for entity in (*levels, *units, *lessons, *questions)]

    assert len(set(every_id)) == len(every_id)


def test_every_cefr_level_is_declared_exactly_once() -> None:
    """The catalogue is a six-level course or it is not a course."""
    codes = [level.code for level in CURRICULUM]

    assert codes == list(CEFRLevel)


def test_no_level_ships_empty() -> None:
    """A level with no answerable question is a screen the app cannot fill.

    The lessons are the browse experience and the questions are the practice one, so a
    level needs both; a unit with no lessons is the same defect one layer down.
    """
    for level in CURRICULUM:
        assert level.units, f"{level.slug} declares no units"
        for unit in level.units:
            assert unit.lessons, f"{level.slug}/{unit.slug} declares no lessons"
            for lesson in unit.lessons:
                assert lesson.questions, f"{level.slug}/{unit.slug}/{lesson.slug} has none"


# ========================================================================= the writes
@pytest.mark.asyncio
async def test_the_run_writes_the_whole_tree_then_converges(schema: None) -> None:
    """The convergence claim, measured against the database rather than the report."""
    expected = _declared_total()

    first = await run_seed(dry_run=False, include_drafts=False)
    assert first.written.total == expected
    assert first.existing.total == 0

    session_factory = get_session_factory()
    async with session_factory() as session:
        assert sum((await _counts(session)).values()) == expected

    second = await run_seed(dry_run=False, include_drafts=False)
    assert second.written.total == 0, "a rerun wrote rows it should have recognised"
    assert second.existing.total == expected


@pytest.mark.asyncio
async def test_the_seeded_tree_is_published_throughout(schema: None) -> None:
    """Every public endpoint filters on ``published``; anything else is invisible.

    The seed exists to make the read-only catalogue return something, so an unpublished
    row here would be a row the app cannot see.
    """
    await run_seed(dry_run=False, include_drafts=False)

    session_factory = get_session_factory()
    async with session_factory() as session:
        for model in (LevelModel, UnitModel, LessonModel):
            unpublished = await session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.state != ContentState.PUBLISHED.value)
            )
            assert not unpublished, f"{model.__name__} has unpublished rows"


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(schema: None) -> None:
    """``--dry-run`` is the one command whose whole purpose is to be safe."""
    report = await run_seed(dry_run=True, include_drafts=False)

    assert report.written.total == _declared_total()
    assert report.dry_run is True

    session_factory = get_session_factory()
    async with session_factory() as session:
        assert sum((await _counts(session)).values()) == 0


@pytest.mark.asyncio
async def test_drafts_land_after_the_published_rows_without_colliding(schema: None) -> None:
    """The regression: a draft position must be computed, not assumed.

    ``a1-greetings`` is declared with published lessons, and the draft lesson is written
    into that same unit. A literal position collides with a real lesson the moment the
    declared unit grows past it, and the collision only surfaces as a unique-constraint
    violation under ``--include-drafts``.
    """
    await run_seed(dry_run=False, include_drafts=False)
    report = await run_seed(dry_run=False, include_drafts=True)

    # Only the two unpublished rows are new; everything else was already recognised.
    assert report.written.total == 2
    assert report.written.units == 1
    assert report.written.lessons == 1

    session_factory = get_session_factory()
    async with session_factory() as session:
        draft_unit = await session.scalar(
            select(UnitModel).where(UnitModel.state == ContentState.REVIEW.value)
        )
        assert draft_unit is not None
        assert draft_unit.position == _next_unit_position(_DRAFT_LEVEL_SLUG)
        assert draft_unit.title == "Coming soon"

        draft_lesson = await session.scalar(
            select(LessonModel).where(LessonModel.state == ContentState.REVIEW.value)
        )
        assert draft_lesson is not None
        assert draft_lesson.position == _next_lesson_position(
            _DRAFT_LEVEL_SLUG, _DRAFT_LESSON_UNIT_SLUG
        )
        # The draft hangs off the *published* unit, which is what makes it the case a
        # single "hide unpublished rows" filter on the level would get wrong.
        published_unit = await session.scalar(
            select(UnitModel).where(
                UnitModel.id == draft_lesson.unit_id,
                UnitModel.state == ContentState.PUBLISHED.value,
            )
        )
        assert published_unit is not None


@pytest.mark.asyncio
async def test_the_draft_rows_are_reported_by_a_dry_run_too(schema: None) -> None:
    """A plan that omits half of what a write would do is not a plan."""
    report = await run_seed(dry_run=True, include_drafts=True)
    levels, units, lessons, questions = _build_tree()

    # The drafts add one unit and one lesson, and no questions at all.
    assert report.written.units == len(units) + 1
    assert report.written.lessons == len(lessons) + 1
    assert report.written.questions == len(questions)
    assert report.written.total == len(levels) + len(units) + len(lessons) + len(questions) + 2
