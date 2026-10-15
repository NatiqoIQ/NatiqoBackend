"""Admin routes: authoring the catalogue and moderating accounts.

Mounted under ``/api/v1/admin`` and role-gated, which is the defining difference from the
public ``/content`` surface. Every route here answers one of three questions the public
catalogue deliberately cannot: *what does the draft look like*, *change it*, and *who did*.

**Two roles, by consequence.** Reading and writing content needs ``content_editor``;
publishing needs ``content_reviewer``, because publishing is the one editorial move that
changes what learners see and the review step exists to keep it a deliberate act. Account
moderation needs ``admin``. The role check reads the live account, not the token claim, so a
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
    AdminUserServiceDep,
)
from app.core.responses import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, build_page
from app.core.types import UserRole
from app.modules.admin.application.dto import (
    MISSING,
    LessonCreateCommand,
    LessonUpdateCommand,
    LevelCreateCommand,
    LevelUpdateCommand,
    QuestionCreateCommand,
    QuestionUpdateCommand,
    UnitCreateCommand,
    UnitUpdateCommand,
    UserRolesCommand,
)
from app.modules.admin.presentation.schemas import (
    AdminUserListResponse,
    AdminUserResponse,
    LessonCreateRequest,
    LessonDetailResponse,
    LessonResponse,
    LessonUpdateRequest,
    LevelCreateRequest,
    LevelResponse,
    LevelUpdateRequest,
    QuestionAdminResponse,
    QuestionCreateRequest,
    QuestionListResponse,
    QuestionReorderRequest,
    QuestionUpdateRequest,
    UnitCreateRequest,
    UnitResponse,
    UnitUpdateRequest,
    UserRolesRequest,
)
from app.modules.users.domain.entities import User
from app.modules.users.presentation.routes import require_role

router = APIRouter()

Limit = Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)]
Cursor = Annotated[str | None, Query(description="Opaque. Pass back meta.nextCursor verbatim.")]

#: Authoring the curriculum — read or write.
ContentEditorDep = Annotated[User, Depends(require_role(UserRole.CONTENT_EDITOR))]
#: Publishing, the move that reaches learners.
ContentReviewerDep = Annotated[User, Depends(require_role(UserRole.CONTENT_REVIEWER))]
#: Account moderation.
AdminDep = Annotated[User, Depends(require_role(UserRole.ADMIN))]

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
    _editor: ContentEditorDep,
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
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LevelResponse:
    """Create a level in ``draft``. Publishing is a separate, reviewer-gated move."""
    view = await service.create_level(
        editor.id,
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
    _editor: ContentEditorDep,
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
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LevelResponse:
    """PATCH: only the fields present in the body are touched."""
    view = await service.update_level(editor.id, level_id, _level_update(payload))
    return LevelResponse.model_validate(view)


@router.delete(
    "/content/levels/{level_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a level and its units and lessons",
)
async def delete_level(
    level_id: _LevelId,
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> Response:
    """Deletion cascades down the tree at the database level."""
    await service.delete_level(editor.id, level_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ========================================================================== units
@router.get(
    "/content/levels/{level_id}/units",
    response_model=Page[UnitResponse],
    summary="List a level's units",
)
async def list_units(
    level_id: _LevelId,
    _editor: ContentEditorDep,
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
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> UnitResponse:
    view = await service.create_unit(
        editor.id,
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
    _editor: ContentEditorDep,
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
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> UnitResponse:
    view = await service.update_unit(editor.id, unit_id, _unit_update(payload))
    return UnitResponse.model_validate(view)


@router.delete(
    "/content/units/{unit_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a unit and its lessons",
)
async def delete_unit(
    unit_id: uuid.UUID,
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> Response:
    await service.delete_unit(editor.id, unit_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ======================================================================== lessons
@router.get(
    "/content/units/{unit_id}/lessons",
    response_model=Page[LessonResponse],
    summary="List a unit's lessons",
)
async def list_lessons(
    unit_id: uuid.UUID,
    _editor: ContentEditorDep,
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
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LessonResponse:
    view = await service.create_lesson(
        editor.id,
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
    response_model=LessonDetailResponse,
    summary="Read a lesson with its questions",
)
async def get_lesson(
    lesson_id: uuid.UUID,
    _editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LessonDetailResponse:
    """The editorial view of a lesson: every question, answer key included."""
    return LessonDetailResponse.model_validate(await service.get_lesson(lesson_id))


@router.patch(
    "/content/lessons/{lesson_id}",
    response_model=LessonResponse,
    summary="Update a lesson",
)
async def update_lesson(
    lesson_id: uuid.UUID,
    payload: LessonUpdateRequest,
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LessonResponse:
    view = await service.update_lesson(editor.id, lesson_id, _lesson_update(payload))
    return LessonResponse.model_validate(view)


@router.delete(
    "/content/lessons/{lesson_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a lesson and its questions",
)
async def delete_lesson(
    lesson_id: uuid.UUID,
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> Response:
    await service.delete_lesson(editor.id, lesson_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/content/lessons/{lesson_id}/publish",
    response_model=LessonResponse,
    summary="Publish a lesson",
)
async def publish_lesson(
    lesson_id: uuid.UUID,
    editor: ContentReviewerDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> LessonResponse:
    """Move a lesson to ``published``, gated at ``content_reviewer``.

    Refused as a 409 unless the lesson is ``approved`` and its unit is already published:
    the parent-published rule is enforced by the aggregate, so no endpoint can publish a
    lesson into a draft unit.
    """
    view = await service.publish_lesson(editor.id, lesson_id)
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
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionAdminResponse:
    """A question that cannot be answered as written is a 422, with every problem listed."""
    view = await service.create_question(
        editor.id,
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
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionListResponse:
    """Rewrite the order in one move.

    The body must list every question in the lesson exactly once; a partial list is a 422
    rather than a silent reshuffle of the rest.
    """
    views = await service.reorder_questions(editor.id, lesson_id, list(payload.question_ids))
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
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> QuestionAdminResponse:
    view = await service.update_question(editor.id, question_id, _question_update(payload))
    return QuestionAdminResponse.model_validate(view)


@router.delete(
    "/content/questions/{question_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a question",
)
async def delete_question(
    question_id: uuid.UUID,
    editor: ContentEditorDep,
    service: AdminContentServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> Response:
    await service.delete_question(editor.id, question_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    summary="Replace an account's roles",
)
async def replace_user_roles(
    payload: UserRolesRequest,
    admin: AdminDep,
    service: AdminUserServiceDep,
    _limit_guard: AdminRateLimitDep,
) -> AdminUserResponse:
    """Set an account's roles wholesale — a replace, so revocation is one call.

    The actor is read from the live account, so the audit row names who really made the
    change rather than whoever a stale token claims to be.
    """
    view = await service.replace_roles(
        admin.id, payload.user_id, UserRolesCommand(roles=tuple(payload.roles))
    )
    return AdminUserResponse.model_validate(view)


# ====================================================================== internals
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
