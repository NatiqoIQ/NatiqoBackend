"""Admin routes: authoring the catalogue and moderating accounts.

Mounted under ``/api/v1/admin`` and role-gated, which is the defining difference from the
public ``/content`` surface. Every route here answers one of three questions the public
catalogue deliberately cannot: *what does the draft look like*, *change it*, and *who did*.

**One gate, ``is_admin``.** Authoring, publishing and account moderation are all
administrator work; the review step that supposed a second role is gone, so a single flag
answers for the whole surface. The check reads the live account, not the token claim, so a
demotion takes effect on the next request rather than at token expiry.

**The audit row is written in the same transaction as the change.** Each write delegates to a
service that persists the mutation and its :class:`AuditEntry` through one unit of work, so
"this lesson changed" and "this editor changed it" commit or roll back together. See
``application/content_service.py``.

Paths are declared in full (``/content/lessons/{id}/publish``) rather than split across two
mounted routers, the same way the gamification and users routers own their own shapes.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.api.deps import (
    AdminContentServiceDep,
    AdminRateLimitDep,
    AdminStatsServiceDep,
    AdminUserServiceDep,
    invalidate_catalogue_tree,
)
from app.core.responses import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, build_page
from app.modules.admin.application.dto import (
    MISSING,
    LessonCreateCommand,
    LessonUpdateCommand,
    LevelCreateCommand,
    LevelUpdateCommand,
    QuestionCreateCommand,
    QuestionDuplicateCommand,
    QuestionUpdateCommand,
    SetAdminCommand,
    UnitCreateCommand,
    UnitUpdateCommand,
)
from app.modules.admin.application.stats_service import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    AdminOverviewView,
)
from app.modules.admin.presentation.schemas import (
    AdminStatsResponse,
    AdminUserListResponse,
    AdminUserResponse,
    LessonCreateRequest,
    LessonDetailAdminResponse,
    LessonResponse,
    LessonUpdateRequest,
    LevelCreateRequest,
    LevelResponse,
    LevelUpdateRequest,
    QuestionAdminResponse,
    QuestionBulkRequest,
    QuestionBulkResponse,
    QuestionCreateRequest,
    QuestionDuplicateRequest,
    QuestionExportItem,
    QuestionExportResponse,
    QuestionImportItem,
    QuestionImportRequest,
    QuestionListResponse,
    QuestionReorderRequest,
    QuestionUpdateRequest,
    SetAdminRequest,
    StatsAiResponse,
    StatsContentResponse,
    StatsGamificationResponse,
    StatsLearningResponse,
    StatsPerformanceResponse,
    StatsQuestionsResponse,
    StatsSeriesPointResponse,
    StatsUsersResponse,
    UnitCreateRequest,
    UnitResponse,
    UnitUpdateRequest,
)
from app.modules.users.domain.entities import User
from app.modules.users.presentation.routes import require_admin

router = APIRouter(dependencies=[Depends(invalidate_catalogue_tree)])

Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
Cursor = Annotated[str | None, Query(description="Opaque. Pass back meta.nextCursor verbatim.")]

#: Authoring the curriculum — read or write.
#: One gate for every route here: the live account must carry the admin flag. Authoring,
#: publishing and account moderation are the same privilege now — there is no ladder to climb.
AdminDep = Annotated[User, Depends(require_admin)]

#: Ids in the path are plain UUIDs; a non-UUID is a 422 from the URL grammar, which is the
#: honest answer before any lookup happens.
_LevelId = Annotated[uuid.UUID, Path(description="The level's id.")]


# ========================================================================= levels
@router.get(
    "/content/levels",
    response_model=Page[LevelResponse],
    summary="List levels, drafts included",
)
async def list_levels(
    _admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> Page[LevelResponse]:
    """Every level in any state, ordered by curriculum position.

    The admin twin of the public tree read: where that serves only ``published`` rows, this
    serves the drafts an editor is working on and pages them by the same opaque cursor.
    """
    page = await service.list_levels(limit=limit, cursor=cursor)
    return build_page(
        items=[LevelResponse.model_validate(level) for level in page.items],
        limit=limit,
        has_more=page.has_more,
        next_cursor=page.next_cursor,
    )


@router.post(
    "/content/levels",
    response_model=LevelResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a level",
)
async def create_level(
    payload: LevelCreateRequest,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LevelResponse:
    """Create a level in ``draft``. Publishing is a separate, reviewer-gated move."""
    view = await service.create_level(
        admin.id,
        LevelCreateCommand(
            code=payload.code,
            title=payload.title,
            description=payload.description,
            position=payload.position,
        ),
    )
    return LevelResponse.model_validate(view)


@router.get(
    "/content/levels/{level_id}",
    response_model=LevelResponse,
    summary="Read a level",
)
async def get_level(
    level_id: _LevelId,
    _admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LevelResponse:
    return LevelResponse.model_validate(await service.get_level(level_id))


@router.patch(
    "/content/levels/{level_id}",
    response_model=LevelResponse,
    summary="Update a level",
)
async def update_level(
    level_id: _LevelId,
    payload: LevelUpdateRequest,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LevelResponse:
    """PATCH: only the fields present in the body are touched."""
    view = await service.update_level(admin.id, level_id, _level_update(payload))
    return LevelResponse.model_validate(view)


@router.delete(
    "/content/levels/{level_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a level and its units and lessons",
)
async def delete_level(
    level_id: _LevelId,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> Response:
    """Deletion cascades down the tree at the database level."""
    await service.delete_level(admin.id, level_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ========================================================================== units
@router.get(
    "/content/levels/{level_id}/units",
    response_model=Page[UnitResponse],
    summary="List a level's units",
)
async def list_units(
    level_id: _LevelId,
    _admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> Page[UnitResponse]:
    page = await service.list_units(level_id, limit=limit, cursor=cursor)
    return build_page(
        items=[UnitResponse.model_validate(unit) for unit in page.items],
        limit=limit,
        has_more=page.has_more,
        next_cursor=page.next_cursor,
    )


@router.post(
    "/content/levels/{level_id}/units",
    response_model=UnitResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a unit in a level",
)
async def create_unit(
    level_id: _LevelId,
    payload: UnitCreateRequest,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> UnitResponse:
    view = await service.create_unit(
        admin.id,
        level_id,
        UnitCreateCommand(
            title=payload.title,
            description=payload.description,
            position=payload.position,
        ),
    )
    return UnitResponse.model_validate(view)


@router.get(
    "/content/units/{unit_id}",
    response_model=UnitResponse,
    summary="Read a unit",
)
async def get_unit(
    unit_id: uuid.UUID,
    _admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> UnitResponse:
    return UnitResponse.model_validate(await service.get_unit(unit_id))


@router.patch(
    "/content/units/{unit_id}",
    response_model=UnitResponse,
    summary="Update a unit",
)
async def update_unit(
    unit_id: uuid.UUID,
    payload: UnitUpdateRequest,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> UnitResponse:
    view = await service.update_unit(admin.id, unit_id, _unit_update(payload))
    return UnitResponse.model_validate(view)


@router.delete(
    "/content/units/{unit_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a unit and its lessons",
)
async def delete_unit(
    unit_id: uuid.UUID,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> Response:
    await service.delete_unit(admin.id, unit_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ======================================================================== lessons
@router.get(
    "/content/units/{unit_id}/lessons",
    response_model=Page[LessonResponse],
    summary="List a unit's lessons",
)
async def list_lessons(
    unit_id: uuid.UUID,
    _admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> Page[LessonResponse]:
    page = await service.list_lessons(unit_id, limit=limit, cursor=cursor)
    return build_page(
        items=[LessonResponse.model_validate(lesson) for lesson in page.items],
        limit=limit,
        has_more=page.has_more,
        next_cursor=page.next_cursor,
    )


@router.post(
    "/content/units/{unit_id}/lessons",
    response_model=LessonResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a lesson in a unit",
)
async def create_lesson(
    unit_id: uuid.UUID,
    payload: LessonCreateRequest,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LessonResponse:
    view = await service.create_lesson(
        admin.id,
        unit_id,
        LessonCreateCommand(
            title=payload.title,
            summary=payload.summary,
            estimated_minutes=payload.estimated_minutes,
            position=payload.position,
        ),
    )
    return LessonResponse.model_validate(view)


@router.get(
    "/content/lessons/{lesson_id}",
    response_model=LessonDetailAdminResponse,
    summary="Read a lesson with its questions",
)
async def get_lesson(
    lesson_id: uuid.UUID,
    _admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LessonDetailAdminResponse:
    """The editorial view of a lesson: every question, answer key included."""
    return LessonDetailAdminResponse.model_validate(await service.get_lesson(lesson_id))


@router.patch(
    "/content/lessons/{lesson_id}",
    response_model=LessonResponse,
    summary="Update a lesson",
)
async def update_lesson(
    lesson_id: uuid.UUID,
    payload: LessonUpdateRequest,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LessonResponse:
    view = await service.update_lesson(admin.id, lesson_id, _lesson_update(payload))
    return LessonResponse.model_validate(view)


@router.delete(
    "/content/lessons/{lesson_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a lesson and its questions",
)
async def delete_lesson(
    lesson_id: uuid.UUID,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> Response:
    await service.delete_lesson(admin.id, lesson_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/content/lessons/{lesson_id}/publish",
    response_model=LessonResponse,
    summary="Publish a lesson",
)
async def publish_lesson(
    lesson_id: uuid.UUID,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LessonResponse:
    """Move a lesson to ``published``, gated behind ``isAdmin``.

    Refused as a 409 unless the lesson is ``approved`` and its unit is already published:
    the parent-published rule is enforced by the aggregate, so no endpoint can publish a
    lesson into a draft unit.
    """
    view = await service.publish_lesson(admin.id, lesson_id)
    return LessonResponse.model_validate(view)


# ====================================================================== questions
@router.post(
    "/content/lessons/{lesson_id}/questions",
    response_model=QuestionAdminResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a question to a lesson",
)
async def create_question(
    lesson_id: uuid.UUID,
    payload: QuestionCreateRequest,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionAdminResponse:
    """A question that cannot be answered as written is a 422, with every problem listed."""
    view = await service.create_question(
        admin.id,
        lesson_id,
        QuestionCreateCommand(
            kind=payload.kind,
            skill=payload.skill,
            prompt=payload.prompt,
            choices=tuple(payload.choices),
            accepted_answers=tuple(payload.accepted_answers),
            correct_index=payload.correct_index,
            explanation=payload.explanation,
            image_url=payload.image_url,
            audio_url=payload.audio_url,
            video_url=payload.video_url,
            points=payload.points,
            position=payload.position,
        ),
    )
    return QuestionAdminResponse.model_validate(view)


@router.post(
    "/content/lessons/{lesson_id}/questions/reorder",
    response_model=QuestionListResponse,
    summary="Reorder a lesson's questions",
)
async def reorder_questions(
    lesson_id: uuid.UUID,
    payload: QuestionReorderRequest,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionListResponse:
    """Rewrite the order in one move.

    The body must list every question in the lesson exactly once; a partial list is a 422
    rather than a silent reshuffle of the rest.
    """
    views = await service.reorder_questions(admin.id, lesson_id, list(payload.question_ids))
    items = [QuestionAdminResponse.model_validate(view) for view in views]
    return QuestionListResponse(items=items, total=len(items))


@router.patch(
    "/content/questions/{question_id}",
    response_model=QuestionAdminResponse,
    summary="Update a question",
)
async def update_question(
    question_id: uuid.UUID,
    payload: QuestionUpdateRequest,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionAdminResponse:
    view = await service.update_question(admin.id, question_id, _question_update(payload))
    return QuestionAdminResponse.model_validate(view)


@router.delete(
    "/content/questions/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a question",
)
async def delete_question(
    question_id: uuid.UUID,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> Response:
    await service.delete_question(admin.id, question_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/content/questions/{question_id}",
    response_model=QuestionAdminResponse,
    summary="Read a question",
)
async def get_question(
    question_id: uuid.UUID,
    _admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionAdminResponse:
    """One question by id, answer key included.

    The editorial projection, not a learner's: this route exists so a question can be opened
    for editing without pulling its whole lesson across the wire.
    """
    return QuestionAdminResponse.model_validate(await service.get_question(question_id))


@router.post(
    "/content/questions/{question_id}/duplicate",
    response_model=QuestionAdminResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Duplicate a question",
)
async def duplicate_question(
    question_id: uuid.UUID,
    payload: QuestionDuplicateRequest,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionAdminResponse:
    """Copy an item, beside the original unless ``lessonId`` or ``position`` says otherwise.

    A copy into another lesson is allowed: reusing a well-written item across a unit is the
    usual reason to duplicate one.
    """
    view = await service.duplicate_question(
        admin.id,
        question_id,
        QuestionDuplicateCommand(lesson_id=payload.lesson_id, position=payload.position),
    )
    return QuestionAdminResponse.model_validate(view)


@router.post(
    "/content/lessons/{lesson_id}/questions/bulk",
    response_model=QuestionBulkResponse,
    summary="Add several questions in one transaction",
)
async def bulk_create_questions(
    lesson_id: uuid.UUID,
    payload: QuestionBulkRequest,
    response: Response,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionBulkResponse:
    """All-or-nothing: every item is validated before any row is written.

    A rejection is a ``422`` whose ``details.failures`` names each failing ``index`` with its
    problems, and leaves the lesson exactly as it was. ``dryRun`` answers the same question
    without writing anything, which is what an import preview uses.

    The status is `201` when rows were created and `200` for a dry run, so a caller can tell
    "written" from "would be written" without reading the body.
    """
    view = await service.create_questions(
        admin.id,
        lesson_id,
        [_question_command(item) for item in payload.questions],
        dry_run=payload.dry_run,
    )
    response.status_code = status.HTTP_200_OK if view.dry_run else status.HTTP_201_CREATED
    return _bulk_response(view)


@router.get(
    "/content/lessons/{lesson_id}/questions/export",
    response_model=QuestionExportResponse,
    summary="Export a lesson's questions",
)
async def export_questions(
    lesson_id: uuid.UUID,
    _admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionExportResponse:
    """The lesson's questions as a package the import route accepts unchanged.

    Ids and positions are deliberately absent, so the package is a template for authoring
    rather than a copy of database rows a second deployment could not use.
    """
    view = await service.export_questions(lesson_id)
    return QuestionExportResponse(
        version=view.version,
        lesson_id=view.lesson_id,
        lesson_title=view.lesson_title,
        exported_at=view.exported_at,
        questions=[QuestionExportItem.model_validate(item) for item in view.questions],
    )


@router.post(
    "/content/lessons/{lesson_id}/questions/import",
    response_model=QuestionBulkResponse,
    summary="Import a lesson's questions from an exported package",
)
async def import_questions(
    lesson_id: uuid.UUID,
    payload: QuestionImportRequest,
    response: Response,
    admin: AdminDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionBulkResponse:
    """Write a package produced by the export route, atomically.

    The package's ``version`` must be one this build knows, and the whole batch is validated
    before anything is written — an import that half-succeeded would be worse than one that
    refused. Positions are appended in the package's own order, so importing twice is two
    copies rather than a silent edit of the first.
    """
    view = await service.import_questions(
        admin.id,
        lesson_id,
        [_question_command(item) for item in payload.questions],
        version=payload.version,
        dry_run=payload.dry_run,
    )
    response.status_code = status.HTTP_200_OK if view.dry_run else status.HTTP_201_CREATED
    return _bulk_response(view)


# =========================================================================== users
@router.get(
    "/users",
    response_model=AdminUserListResponse,
    summary="List accounts",
)
async def list_users(
    _admin: AdminDep,
    service: AdminUserServiceDep,
    _limit_guard: AdminRateLimitDep,
    limit: Limit = DEFAULT_PAGE_SIZE,
    cursor: Cursor = None,
) -> AdminUserListResponse:
    """Accounts newest first, with the total so the screen can show a count."""
    view = await service.list_users(limit=limit, cursor=cursor)
    return AdminUserListResponse(
        items=[AdminUserResponse.model_validate(user) for user in view.items],
        total=view.total,
        has_more=view.has_more,
        next_cursor=view.next_cursor,
    )


@router.post(
    "/users",
    response_model=AdminUserResponse,
    summary="Grant or revoke an account's admin flag",
)
async def set_user_admin(
    payload: SetAdminRequest,
    admin: AdminDep,
    service: AdminUserServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> AdminUserResponse:
    """Set an account's admin flag — one call in either direction, so revocation is easy.

    The actor is read from the live account, so the audit row names who really made the
    change rather than whoever a stale token claims to be.
    """
    view = await service.set_admin(
        admin.id, payload.user_id, SetAdminCommand(is_admin=payload.is_admin)
    )
    return AdminUserResponse.model_validate(view)


# ========================================================================== stats
#: The activity window, in days. Bounded at both ends: a window of zero has no meaning and
#: a window past a quarter stops distinguishing activity from the lifetime total.
Window = Annotated[int, Query(ge=1, le=MAX_WINDOW_DAYS, description="Activity window, in days.")]


@router.get(
    "/stats/overview",
    response_model=AdminStatsResponse,
    summary="Read the dashboard's aggregate totals",
)
async def get_stats_overview(
    _admin: AdminDep,
    service: AdminStatsServiceDep,
    _limit_guard: AdminRateLimitDep,
    window: Window = DEFAULT_WINDOW_DAYS,
) -> AdminStatsResponse:
    """Accounts, learning, catalogue, question stock, learner quality and AI spend in one
    read, as aggregates.

    Admin-only, unlike the content routes: half of this payload is account data, and a role
    that may author a lesson has no business counting users. A content editor who needs the
    catalogue numbers can derive them from the levels they already list.

    ``window`` scopes what a period can honestly scope — how many accounts joined and
    signed in during it, and the daily ``series`` the chart draws. Everything else is a
    lifetime total (or the one live figure, ``learning.sessionsActive``), so changing the
    window never makes the other numbers jump around.
    """
    return _stats_response(await service.overview(window_days=window))


# ====================================================================== internals
def _stats_response(view: AdminOverviewView) -> AdminStatsResponse:
    totals = view.totals
    questions = totals.questions
    performance = totals.performance
    gamification = totals.gamification
    ai = totals.ai
    return AdminStatsResponse(
        generated_at=view.generated_at,
        window_days=view.window_days,
        users=StatsUsersResponse(
            total=totals.users_total,
            new=totals.users_new,
            active=totals.users_active,
            deleted=totals.users_deleted,
        ),
        learning=StatsLearningResponse(
            lessons_completed=totals.lessons_completed,
            sessions_completed=totals.sessions_completed,
            sessions_active=totals.sessions_active,
            answers_graded=totals.answers_graded,
            correct_answers=totals.correct_answers,
            accuracy_percent=view.accuracy_percent,
            xp_awarded=totals.xp_awarded,
        ),
        content=StatsContentResponse(
            levels=totals.levels,
            units=totals.units,
            lessons=totals.lessons,
            published_lessons=totals.published_lessons,
            drafts=totals.drafts,
            in_review=totals.in_review,
        ),
        questions=StatsQuestionsResponse(
            total=questions.total,
            published=questions.published,
            by_kind=questions.by_kind,
            by_skill=questions.by_skill,
        ),
        performance=StatsPerformanceResponse(
            average_score=view.average_score,
            reviews_total=performance.reviews_total,
            reviews_passed=performance.reviews_passed,
            retention_percent=view.retention_percent,
            average_quality=view.average_quality,
        ),
        gamification=StatsGamificationResponse(
            xp_total=gamification.xp_total,
            active_streaks=gamification.active_streaks,
            longest_streak=gamification.longest_streak,
            achievements_unlocked=gamification.achievements_unlocked,
        ),
        ai=StatsAiResponse(
            requests=ai.requests,
            errors=ai.errors,
            tokens=ai.tokens,
            cost_usd=ai.cost_usd,
            conversations=ai.conversations,
        ),
        series=[
            StatsSeriesPointResponse(
                day=point.day,
                new_users=point.new_users,
                sessions_completed=point.sessions_completed,
                xp_awarded=point.xp_awarded,
                ai_requests=point.ai_requests,
            )
            for point in view.series
        ],
    )


def _question_command(
    item: QuestionCreateRequest | QuestionImportItem,
) -> QuestionCreateCommand:
    """A create body as the command the service takes.

    Shared by the bulk and import routes because the two bodies differ only in the fields they
    accept (import has no ``position``) — the mapping onto the command is identical, and
    duplicating it is how one of the two silently stops passing a field.
    """
    return QuestionCreateCommand(
        kind=item.kind,
        skill=item.skill,
        prompt=item.prompt,
        choices=tuple(item.choices),
        accepted_answers=tuple(item.accepted_answers),
        correct_index=item.correct_index,
        explanation=item.explanation,
        image_url=item.image_url,
        audio_url=item.audio_url,
        video_url=item.video_url,
        points=item.points,
        position=getattr(item, "position", None),
    )


def _bulk_response(view: Any) -> QuestionBulkResponse:
    """Project a bulk result onto the wire, with ``total`` as the number written now."""
    items = [QuestionAdminResponse.model_validate(item) for item in view.items]
    return QuestionBulkResponse(
        items=items,
        total=len(items),
        accepted=view.accepted,
        dry_run=view.dry_run,
    )


def _level_update(payload: LevelUpdateRequest) -> LevelUpdateCommand:
    fields = payload.model_fields_set
    return LevelUpdateCommand(
        title=_not_none(payload, "title", fields),
        description=_or_missing(payload, "description", fields),
        position=_not_none(payload, "position", fields),
        state=_state(payload, "state", fields),
    )


def _unit_update(payload: UnitUpdateRequest) -> UnitUpdateCommand:
    fields = payload.model_fields_set
    return UnitUpdateCommand(
        title=_not_none(payload, "title", fields),
        description=_or_missing(payload, "description", fields),
        position=_not_none(payload, "position", fields),
        state=_state(payload, "state", fields),
    )


def _lesson_update(payload: LessonUpdateRequest) -> LessonUpdateCommand:
    fields = payload.model_fields_set
    return LessonUpdateCommand(
        title=_not_none(payload, "title", fields),
        summary=_not_none(payload, "summary", fields),
        estimated_minutes=_not_none(payload, "estimated_minutes", fields),
        position=_not_none(payload, "position", fields),
        state=_state(payload, "state", fields),
    )


def _question_update(payload: QuestionUpdateRequest) -> QuestionUpdateCommand:
    fields = payload.model_fields_set
    return QuestionUpdateCommand(
        kind=_not_none(payload, "kind", fields),
        skill=_not_none(payload, "skill", fields),
        prompt=_not_none(payload, "prompt", fields),
        choices=_sequence(payload, "choices", fields),
        accepted_answers=_sequence(payload, "accepted_answers", fields),
        # ``correct_index`` treats an explicit ``null`` as a value: clearing it is how a
        # client finishes converting a multiple-choice item into a fill-in-the-blank one.
        correct_index=(payload.correct_index if "correct_index" in fields else MISSING),
        explanation=_not_none(payload, "explanation", fields),
        image_url=_or_missing(payload, "image_url", fields),
        audio_url=_or_missing(payload, "audio_url", fields),
        video_url=_or_missing(payload, "video_url", fields),
        points=_not_none(payload, "points", fields),
        position=_not_none(payload, "position", fields),
    )


def _not_none(payload: object, name: str, fields: set[str]) -> Any:
    """The field's value when it was supplied and is not ``None``; otherwise ``MISSING``.

    Used for fields that cannot legitimately be cleared — a title, a point value — so an
    accidental ``null`` is "leave it alone" rather than a way to blank a required field.
    """
    if name in fields:
        value = getattr(payload, name)
        if value is not None:
            return value
    return MISSING


def _or_missing(payload: object, name: str, fields: set[str]) -> Any:
    """The field's value when supplied, ``None`` included; otherwise ``MISSING``.

    Used for nullable fields, where ``null`` is a real edit ("clear the description") and
    must be distinguishable from an absent key.
    """
    if name in fields:
        return getattr(payload, name)
    return MISSING


def _sequence(payload: object, name: str, fields: set[str]) -> Any:
    """A list field as a tuple when supplied and not ``None``; otherwise ``MISSING``."""
    if name in fields:
        value = getattr(payload, name)
        if value is not None:
            return tuple(value)
    return MISSING


def _state(payload: object, name: str, fields: set[str]) -> Any:
    if name in fields:
        value = getattr(payload, name)
        if value is not None:
            return value
    return MISSING


__all__ = ["router"]
