"""Read-only use cases for the public catalogue.

Five entry points: browse levels, browse a level's units, browse a unit's lessons, open a
lesson, and compose the whole tree. The three paged reads and the tree each apply the same
two rules, so they are written as one rule applied repeatedly rather than several separate
rules:

**Only published content is visible.** The repositories already filter on state and this
layer never asks for anything else, so a draft cannot reach a learner. When something is
missing *or* unpublished the result is a 404, never a 403 — a 403 would answer a
question a learner should not be able to ask ("does this exist but is not ready yet?")
and turn the public API into a progress bar for the editorial pipeline.

**Counts cost one query per screen, not one per row.** A curriculum browser that shows
"12 lessons" cannot afford a per-row count, so the ``count_*_by_*`` reads are answered
by a single grouped aggregate regardless of page size.

``limit`` is expected to have been range-checked by the caller (the route does it with a
``Query`` constraint). This layer is not the boundary for that.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict

from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.logging import get_logger
from app.core.pagination import (
    InvalidCursor,
    PageResult,
    PositionCursor,
    decode_position_cursor,
    paginate,
)
from app.core.responses import MAX_PAGE_SIZE
from app.core.types import CEFRLevel, ContentId
from app.core.uow import UnitOfWorkProtocol
from app.modules.content.application.dto import (
    LessonDetailView,
    LessonSummaryView,
    LevelNodeView,
    LevelView,
    QuestionView,
    UnitNodeView,
    UnitView,
)
from app.modules.content.domain.entities import (
    Lesson,
    LessonPlacement,
    Level,
    Question,
    Unit,
    UnitPlacement,
)
from app.modules.content.domain.policy import pre_attempt_choices, pre_attempt_prompts
from app.platform.cache.port import CachePort
from app.platform.cache.single_flight import flight_lock

logger = get_logger("catalogue.service")

#: Cache format version. Bump when a view gains or renames a field so an old payload is
#: a miss rather than a KeyError on the read path.
_TREE_CACHE_VERSION = "v1"


def tree_cache_key(cache: CachePort) -> str:
    """The one key the tree is stored under, and the one the editor invalidates."""
    return cache.build_key("catalogue", "tree", _TREE_CACHE_VERSION)


def _key(row: Level | Unit | Lesson) -> tuple[int, uuid.UUID]:
    return (row.position, row.id)


class CatalogueService:
    def __init__(
        self,
        uow: UnitOfWorkProtocol,
        cache: CachePort | None = None,
        *,
        ttl_seconds: int = 30,
    ) -> None:
        self._uow = uow
        self._cache = cache
        self._ttl_seconds = ttl_seconds
        self._tree_key = tree_cache_key(cache) if cache is not None else ""

    # ------------------------------------------------------------------ levels
    async def list_levels(self, *, limit: int, cursor: str | None = None) -> PageResult[LevelView]:
        after = _decode(cursor)
        unit_counts = await self._uow.units.count_published_by_level()
        page = paginate(
            await self._uow.levels.list_published(limit=limit + 1, after=after),
            limit=limit,
            key_of=_key,
        )
        return PageResult(
            items=[
                LevelView(
                    id=level.id,
                    code=level.code.value,
                    title=level.title,
                    description=level.description,
                    position=level.position,
                    unit_count=unit_counts.get(level.id, 0),
                )
                for level in page.items
            ],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    # ------------------------------------------------------------------- units
    async def list_units(
        self, code: str | CEFRLevel, *, limit: int, cursor: str | None = None
    ) -> PageResult[UnitView]:
        """Published units of one level, addressed by its public CEFR code.

        ``code`` is accepted as a plain string on purpose. A route that typed the path
        parameter as ``CEFRLevel`` would have FastAPI reject an unknown code with a 422
        *before* this method ran, which would make "no such level" distinguishable from
        "a level that exists but is not published" — the exact leak the 404 rule below
        exists to close. Parsing here keeps both cases on one path.
        """
        resolved = _parse_code(code)
        level = (
            await self._uow.levels.get_published_by_code(resolved) if resolved is not None else None
        )
        if level is None:
            raise NotFoundError(
                f"There is no published level {_code_text(code)}.",
                code=ErrorCode.LEVEL_NOT_FOUND,
            )
        after = _decode(cursor)
        lesson_counts = await self._uow.lessons.count_published_by_unit()
        page = paginate(
            await self._uow.units.list_published_for_level(level.id, limit=limit + 1, after=after),
            limit=limit,
            key_of=_key,
        )
        return PageResult(
            items=[
                UnitView(
                    id=unit.id,
                    level_code=level.code.value,
                    title=unit.title,
                    description=unit.description,
                    position=unit.position,
                    lesson_count=lesson_counts.get(unit.id, 0),
                )
                for unit in page.items
            ],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    # ----------------------------------------------------------------- lessons
    async def list_lessons(
        self, unit_id: uuid.UUID, *, limit: int, cursor: str | None = None
    ) -> PageResult[LessonSummaryView]:
        unit = await self._uow.units.get_published(ContentId(unit_id))
        if unit is None:
            raise NotFoundError(
                "There is no published unit with that id.", code=ErrorCode.UNIT_NOT_FOUND
            )
        after = _decode(cursor)
        page = paginate(
            await self._uow.lessons.list_published_for_unit(unit.id, limit=limit + 1, after=after),
            limit=limit,
            key_of=_key,
        )
        # One query for the whole page's questions, then bucketed onto their lessons —
        # rather than a count query per lesson, which would make the screen scale with
        # the page size.
        questions = await self._uow.questions.list_for_lessons([lesson.id for lesson in page.items])
        counts: dict[ContentId, int] = defaultdict(int)
        for question in questions:
            counts[question.lesson_id] += 1
        return PageResult(
            items=[_lesson_summary(lesson, counts.get(lesson.id, 0)) for lesson in page.items],
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    async def get_lesson(self, lesson_id: uuid.UUID) -> LessonDetailView:
        lesson = await self._uow.lessons.get_published(ContentId(lesson_id))
        if lesson is None:
            raise NotFoundError(
                "There is no published lesson with that id.", code=ErrorCode.LESSON_NOT_FOUND
            )
        questions = await self._uow.questions.list_for_lessons([lesson.id])
        # Built field by field rather than with ``dataclasses.replace``: ``replace``
        # rebuilds ``type(obj)``, which here is the summary, so the extra
        # ``questions`` field would be rejected.
        return LessonDetailView(
            id=lesson.id,
            unit_id=lesson.unit_id,
            title=lesson.title,
            summary=lesson.summary,
            position=lesson.position,
            estimated_minutes=lesson.estimated_minutes,
            question_count=len(questions),
            questions=tuple(_question_view(question) for question in questions),
        )

    # ------------------------------------------------------------------- tree
    async def tree(self) -> tuple[LevelNodeView, ...]:
        """The whole published catalogue as one nested tree: levels → units → lessons.

        This is the first screen every client loads, so it must not be a walk. It reads
        the four facts it needs in four queries — published levels, every published unit
        with its placement, every published lesson with its placement, and the question
        counts for that whole set — then assembles the nesting in memory. The previous
        implementation composed the paged reads, which was cleaner but issued roughly one
        query per unit *per level*: on the seeded curriculum that was ~37 sequential
        round-trips, and every morning launch paid it again. The published-only rule did
        not move: ``list_published_ordered`` joins the level and (for lessons) the unit,
        so a child of a draft parent is absent here exactly as it is from the paged reads.

        Each level of nesting stays capped at ``MAX_PAGE_SIZE``. A curriculum larger than
        that is truncated, not paged: this endpoint is a single cached document, and the
        real catalogue is far below the cap.
        """
        cached = await self._cached_tree()
        if cached is not None:
            return cached
        if self._cache is None or self._ttl_seconds <= 0:
            # No shared cache means no cache stampede either: every caller must build.
            return await self._build_tree()

        async with flight_lock(self._tree_key):
            # Another request may have filled the key while this one waited. Re-check
            # inside the lock; without this second read every waiter would rebuild the
            # same tree in turn, which is the herd with extra steps.
            cached = await self._cached_tree()
            if cached is not None:
                return cached
            tree = await self._build_tree()
            await self._store_tree(tree)
            return tree

    async def _build_tree(self) -> tuple[LevelNodeView, ...]:
        """Read the four facts and assemble the nested tree (no cache access)."""
        levels = await self._uow.levels.list_published(limit=MAX_PAGE_SIZE, after=None)
        level_codes = {level.code for level in levels}
        units = [
            placement
            for placement in await self._uow.units.list_published_ordered()
            if placement.level_code in level_codes
        ]
        lessons = await self._uow.lessons.list_published_ordered()
        lesson_counts: dict[ContentId, int] = defaultdict(int)
        for question in await self._uow.questions.list_for_lessons(
            [placement.lesson.id for placement in lessons]
        ):
            lesson_counts[question.lesson_id] += 1

        units_by_level: dict[CEFRLevel, list[UnitPlacement]] = defaultdict(list)
        for placement in units:
            units_by_level[placement.level_code].append(placement)
        lessons_by_unit: dict[ContentId, list[LessonPlacement]] = defaultdict(list)
        for placement in lessons:
            lessons_by_unit[placement.lesson.unit_id].append(placement)

        nodes: list[LevelNodeView] = []
        for level in levels:
            level_units = units_by_level.get(level.code, [])
            unit_nodes: list[UnitNodeView] = []
            for placement in level_units[:MAX_PAGE_SIZE]:
                unit = placement.unit
                unit_lessons = lessons_by_unit.get(unit.id, [])
                unit_nodes.append(
                    UnitNodeView(
                        id=unit.id,
                        level_code=placement.level_code.value,
                        title=unit.title,
                        description=unit.description,
                        position=unit.position,
                        lesson_count=len(unit_lessons),
                        lessons=tuple(
                            _lesson_summary(lp.lesson, lesson_counts.get(lp.lesson.id, 0))
                            for lp in unit_lessons[:MAX_PAGE_SIZE]
                        ),
                    )
                )
            nodes.append(
                LevelNodeView(
                    id=level.id,
                    code=level.code.value,
                    title=level.title,
                    description=level.description,
                    position=level.position,
                    unit_count=len(level_units),
                    units=tuple(unit_nodes),
                )
            )
        return tuple(nodes)

    # ------------------------------------------------------------ tree cache
    async def _cached_tree(self) -> tuple[LevelNodeView, ...] | None:
        """The tree from the shared cache, or ``None`` to rebuild.

        A cache is an optimisation on a read, never a precondition for one: a Redis
        outage, a malformed payload or a container change must all fall through to the
        database rather than become a 500 on the first screen of the app. That is why
        every failure here is logged and swallowed.
        """
        if self._cache is None or self._ttl_seconds <= 0:
            return None
        try:
            raw = await self._cache.get(self._tree_key)
        except Exception as exc:  # noqa: BLE001 - a cache outage must not fail the read
            logger.warning("catalogue.cache_read_failed", error=str(exc)[:200])
            return None
        if not raw:
            return None
        try:
            return _decode_tree(raw)
        except (KeyError, TypeError, ValueError):
            # A payload written by an older shape, or a truncated value. Rebuilding is
            # cheap and correct; serving half a tree is neither.
            logger.warning("catalogue.cache_payload_invalid")
            return None

    async def _store_tree(self, tree: tuple[LevelNodeView, ...]) -> None:
        if self._cache is None or self._ttl_seconds <= 0:
            return
        try:
            await self._cache.set(self._tree_key, _encode_tree(tree), ttl_seconds=self._ttl_seconds)
        except Exception as exc:  # noqa: BLE001 - a failed write is just a miss next time
            logger.warning("catalogue.cache_write_failed", error=str(exc)[:200])


# ------------------------------------------------------------------- internals
def _encode_tree(tree: tuple[LevelNodeView, ...]) -> str:
    """JSON for the cache.

    UTF-8 rather than ASCII escapes: Arabic titles are the common case, and escaping
    every letter triples the payload for no reason.
    """
    return json.dumps(
        [
            {
                "id": str(level.id),
                "code": level.code,
                "title": level.title,
                "description": level.description,
                "position": level.position,
                "unit_count": level.unit_count,
                "units": [
                    {
                        "id": str(unit.id),
                        "level_code": unit.level_code,
                        "title": unit.title,
                        "description": unit.description,
                        "position": unit.position,
                        "lesson_count": unit.lesson_count,
                        "lessons": [
                            {
                                "id": str(lesson.id),
                                "unit_id": str(lesson.unit_id),
                                "title": lesson.title,
                                "summary": lesson.summary,
                                "position": lesson.position,
                                "estimated_minutes": lesson.estimated_minutes,
                                "question_count": lesson.question_count,
                            }
                            for lesson in unit.lessons
                        ],
                    }
                    for unit in level.units
                ],
            }
            for level in tree
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _decode_tree(raw: str) -> tuple[LevelNodeView, ...]:
    """Rebuild the views from :func:`_encode_tree`.

    Kept next to the encoder so the two cannot drift. Any shape mismatch raises and the
    caller falls back to the database; this is validation by construction, not a second
    schema to maintain.
    """
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise TypeError("catalogue payload is not a list")
    return tuple(
        LevelNodeView(
            id=uuid.UUID(level["id"]),
            code=str(level["code"]),
            title=str(level["title"]),
            description=level["description"],
            position=int(level["position"]),
            unit_count=int(level["unit_count"]),
            units=tuple(
                UnitNodeView(
                    id=uuid.UUID(unit["id"]),
                    level_code=str(unit["level_code"]),
                    title=str(unit["title"]),
                    description=unit["description"],
                    position=int(unit["position"]),
                    lesson_count=int(unit["lesson_count"]),
                    lessons=tuple(
                        LessonSummaryView(
                            id=uuid.UUID(lesson["id"]),
                            unit_id=uuid.UUID(lesson["unit_id"]),
                            title=str(lesson["title"]),
                            summary=str(lesson["summary"]),
                            position=int(lesson["position"]),
                            estimated_minutes=int(lesson["estimated_minutes"]),
                            question_count=int(lesson["question_count"]),
                        )
                        for lesson in unit["lessons"]
                    ),
                )
                for unit in level["units"]
            ),
        )
        for level in payload
    )


def _parse_code(code: str | CEFRLevel) -> CEFRLevel | None:
    """A code that is not CEFR is treated as *absent* rather than *invalid*.

    Returning ``None`` keeps an unparsable code on the same 404 as a level that was never
    created, so the endpoint gives away nothing about which CEFR codes exist.
    """
    if isinstance(code, CEFRLevel):
        return code
    try:
        return CEFRLevel(code.strip().upper())
    except ValueError:
        return None


def _code_text(code: str | CEFRLevel) -> str:
    return code.value if isinstance(code, CEFRLevel) else code


def _decode(cursor: str | None) -> PositionCursor | None:
    """Turn an opaque cursor into a sort key, or ``None`` for the first page.

    A malformed cursor is a 422 rather than something we repair. Guessing a position
    would silently skip content, and a learner seeing a gap has no way to tell that from
    a bug.
    """
    if cursor is None:
        return None
    try:
        return decode_position_cursor(cursor)
    except InvalidCursor as exc:
        raise ValidationFailed(
            "The cursor is not usable. Start again from the first page.",
            details={"field": "cursor"},
        ) from exc


def _lesson_summary(lesson: Lesson, question_count: int) -> LessonSummaryView:
    return LessonSummaryView(
        id=lesson.id,
        unit_id=lesson.unit_id,
        title=lesson.title,
        summary=lesson.summary,
        position=lesson.position,
        estimated_minutes=lesson.estimated_minutes,
        question_count=question_count,
    )


def _question_view(question: Question) -> QuestionView:
    """Project a question aggregate onto what a learner may see *before* answering.

    See the docstring in ``dto.py``: this projection is a security boundary, and the
    fields left off it are the answer key. ``image_url``, ``audio_url`` and ``video_url``
    are left *on* it on purpose — they are pre-attempt aids that teach the item, not a
    reveal of the answer. ``choices`` is filtered through :func:`pre_attempt_choices`,
    because an ordering question's choices are stored in the correct sequence and would be
    the key.
    """
    return QuestionView(
        id=question.id,
        kind=question.kind,
        skill=question.skill,
        prompt=question.prompt,
        choices=pre_attempt_choices(question.kind, question.choices),
        points=question.points,
        image_url=question.image_url,
        audio_url=question.audio_url,
        video_url=question.video_url,
        prompts=pre_attempt_prompts(question.kind, question.accepted_answers),
    )


__all__ = ["CatalogueService"]
