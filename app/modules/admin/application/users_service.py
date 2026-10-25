"""Account moderation use cases for the admin surface.

A learner, their profile and their settings are the users module's business; the only two
things an administrator does to an account live here: list them, and grant or revoke the
admin flag on one. Reads go through the users repository's paged query — extended in this
phase with :meth:`UserRepositoryPort.list_users` because no auth flow needs "a page of
accounts".

There is no role vocabulary to validate: the flag is a boolean, so there is no typo that
could be stored looking authoritative while granting nothing. The change is audited like
every other admin mutation — "who gave this account admin" is the question the trail exists
for.
"""

from __future__ import annotations

import uuid

from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.pagination import (
    InvalidCursor,
    decode_time_cursor,
    paginate_time,
)
from app.core.types import UserId
from app.core.uow import UnitOfWorkProtocol
from app.modules.admin.application.dto import (
    AdminUserListView,
    AdminUserView,
    SetAdminCommand,
)
from app.modules.admin.domain.audit import ENTITY_USER, AuditAction, AuditEntry
from app.modules.users.domain.entities import User


class AdminUserService:
    def __init__(self, uow: UnitOfWorkProtocol) -> None:
        self._uow = uow

    async def list_users(self, *, limit: int, cursor: str | None = None) -> AdminUserListView:
        after = None
        if cursor is not None:
            try:
                after = decode_time_cursor(cursor)
            except InvalidCursor as exc:
                raise ValidationFailed(
                    "The cursor is not usable. Start again from the first page.",
                    details={"field": "cursor"},
                ) from exc
        rows = await self._uow.users.list_users(limit=limit + 1, after=after)
        page = paginate_time(rows, limit=limit, key_of=lambda user: (user.created_at, user.id))
        names = await self._uow.users.get_display_names([user.id for user in page.items])
        return AdminUserListView(
            items=tuple(_user_view(user, names.get(user.id, "")) for user in page.items),
            total=await self._uow.users.count(),
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    async def set_admin(
        self, actor_id: UserId, user_id: uuid.UUID, command: SetAdminCommand
    ) -> AdminUserView:
        """Grant or revoke the admin flag on an account.

        Revocation has to be as expressible as granting — both directions are one call,
        because "take admin away from this account" is the security-relevant half and it
        must never require a second round trip.
        """
        user = await self._uow.users.get(UserId(user_id))
        if user is None:
            raise NotFoundError("No account with that id.", code=ErrorCode.USER_NOT_FOUND)

        user.is_admin = command.is_admin
        await self._uow.users.update(user)
        await self._uow.audit_logs.add(
            AuditEntry(
                actor_id=actor_id,
                action=str(AuditAction.USER_ADMIN_UPDATE),
                entity_type=ENTITY_USER,
                entity_id=user.id,
                summary=(
                    f"Granted admin on {user.email}."
                    if command.is_admin
                    else f"Revoked admin on {user.email}."
                ),
                changes={"is_admin": command.is_admin},
            )
        )
        await self._uow.commit()
        profile = await self._uow.users.get_profile(user.id)
        return _user_view(user, profile.display_name if profile else "")


def _user_view(user: User, name: str) -> AdminUserView:
    return AdminUserView(
        id=user.id,
        email=user.email,
        name=name,
        status=user.status.value,
        is_admin=user.is_admin,
        created_at=user.created_at,
    )


__all__ = ["AdminUserService"]
