"""Account moderation use cases for the admin surface.

A learner, their profile and their settings are the users module's business; the only two
things an administrator does to an account live here: list them, and change the roles on
one. Reads go through the users repository's paged query — extended in this phase with
:meth:`UserRepositoryPort.list_users` because no auth flow needs "a page of accounts".

Role assignment is validated against :class:`UserRole` rather than stored as sent. An
unknown role string would sit in the column looking authoritative while granting nothing,
so a typo is a 422 instead of a silently useless write. The change is audited like every
other admin mutation: "who gave this account admin" is the question the trail exists for.
"""

from __future__ import annotations

import uuid

from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.pagination import (
    InvalidCursor,
    decode_time_cursor,
    paginate_time,
)
from app.core.types import UserId, UserRole
from app.core.uow import UnitOfWorkProtocol
from app.modules.admin.application.dto import (
    AdminUserListView,
    AdminUserView,
    UserRolesCommand,
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
        return AdminUserListView(
            items=tuple(_user_view(user) for user in page.items),
            total=await self._uow.users.count(),
            has_more=page.has_more,
            next_cursor=page.next_cursor,
        )

    async def replace_roles(
        self, actor_id: UserId, user_id: uuid.UUID, command: UserRolesCommand
    ) -> AdminUserView:
        """Replace an account's roles wholesale.

        A replace, not a merge: revocation has to be expressible, and "remove admin from
        this account" is a single call rather than a second endpoint. An empty or unknown
        role set is refused — an account with no roles is not a state the auth layer knows
        how to reason about.
        """
        if not command.roles:
            raise ValidationFailed("At least one role is required.", details={"field": "roles"})
        try:
            roles = tuple(UserRole(role) for role in command.roles)
        except ValueError as exc:
            raise ValidationFailed(
                "Unknown role.",
                details={"field": "roles", "allowed": [role.value for role in UserRole]},
            ) from exc

        user = await self._uow.users.get(UserId(user_id))
        if user is None:
            raise NotFoundError("No account with that id.", code=ErrorCode.USER_NOT_FOUND)

        user.roles = roles
        await self._uow.users.update(user)
        await self._uow.audit_logs.add(
            AuditEntry(
                actor_id=actor_id,
                action=str(AuditAction.USER_ROLES_UPDATE),
                entity_type=ENTITY_USER,
                entity_id=user.id,
                summary=f"Set roles on {user.email}.",
                changes={"roles": [role.value for role in roles]},
            )
        )
        await self._uow.commit()
        return _user_view(user)


def _user_view(user: User) -> AdminUserView:
    return AdminUserView(
        id=user.id,
        email=user.email,
        status=user.status.value,
        roles=tuple(role.value for role in user.roles),
        created_at=user.created_at,
    )


__all__ = ["AdminUserService"]
