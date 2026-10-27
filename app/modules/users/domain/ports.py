"""Persistence ports for the users module.

The application layer talks to these, never to SQLAlchemy. That is what lets the auth
use cases be unit-tested against an in-memory fake with no database at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.pagination import TimeCursor
from app.core.types import UserId
from app.modules.users.domain.entities import (
    ExternalIdentity,
    RefreshSession,
    User,
    UserDailyChallenge,
    UserDevice,
    UserProfile,
    UserSettings,
)


@runtime_checkable
class UserRepositoryPort(Protocol):
    """Read/write access to accounts.

    Implementations must not commit: the unit of work owns the transaction so a use
    case can read, decide and write atomically.
    """

    async def get(self, user_id: UserId) -> User | None: ...

    async def get_by_email(self, email: str) -> User | None: ...

    async def email_exists(self, email: str) -> bool: ...

    async def add(self, user: User) -> None: ...

    async def update(self, user: User) -> None: ...

    async def list_users(self, *, limit: int, after: TimeCursor | None) -> list[User]:
        """A page of accounts, newest first, for the admin moderation surface.

        Ordered by ``(created_at desc, id desc)`` so a page boundary stays stable as
        accounts are created around it — see :class:`app.core.pagination.TimeCursor`.
        """
        ...

    async def count(self) -> int:
        """Total number of accounts. Used for the admin list's ``total`` hint."""
        ...

    async def get_profile(self, user_id: UserId) -> UserProfile | None: ...

    async def get_display_names(self, user_ids: list[UserId]) -> dict[UserId, str]:
        """``user_id -> display_name`` for a batch of accounts.

        One query for a whole admin page instead of a profile fetch per row. Accounts
        with no profile row are simply absent from the mapping.
        """
        ...

    async def save_profile(self, profile: UserProfile) -> None: ...

    async def get_settings(self, user_id: UserId) -> UserSettings | None: ...

    async def save_settings(self, settings: UserSettings) -> None: ...

    # ------------------------------------------------------- daily challenge
    async def get_daily_challenge(
        self, user_id: UserId, *, challenge_date: str
    ) -> UserDailyChallenge:
        """The learner's challenge for one calendar day — always a record.

        Absence is zeros rather than ``None``: the counts are derived from what was
        recorded, so a learner who has done nothing reads ``0 / 0 / false`` and no row is
        written merely to answer a read.
        """
        ...

    async def record_daily_challenge_contribution(
        self,
        user_id: UserId,
        *,
        session_id: uuid.UUID,
        challenge_date: str,
        lessons_completed: int,
        words_learned: int,
    ) -> bool:
        """Attribute one finished sitting to its own day, at most once.

        Returns ``False`` when this session has already been counted, which is what makes
        a retried completion a no-op instead of a second lesson. ``challenge_date`` is the
        day the sitting finished in the learner's timezone, not the day this call runs.
        """
        ...

    async def claim_daily_challenge(self, user_id: UserId, *, challenge_date: str) -> bool:
        """Take one day's reward; ``False`` means it was already claimed.

        The guard lives in the statement's predicate, so two simultaneous claims cannot
        both report success.
        """
        ...


@runtime_checkable
class ExternalIdentityRepositoryPort(Protocol):
    """Links a provider account to a learner.

    ``(provider, subject)`` is the lookup that every sign-in performs, so an
    implementation must expose exactly that pair as an indexed, unique key and must not
    commit on its own.
    """

    async def find(self, provider: str, subject: str) -> ExternalIdentity | None: ...

    async def list_for_user(self, user_id: UserId) -> list[ExternalIdentity]: ...

    async def add(self, identity: ExternalIdentity) -> None: ...

    async def touch_login(self, provider: str, subject: str, *, at: datetime | None = None) -> None:
        """Record that this identity was just used, for the account's own audit view."""
        ...


@runtime_checkable
class RefreshTokenRepositoryPort(Protocol):
    """Refresh-token persistence for rotation and reuse detection."""

    async def add(self, session: RefreshSession) -> None: ...

    async def get_by_jti_hash(self, jti_hash: str) -> RefreshSession | None: ...

    async def revoke(self, session: RefreshSession, *, at: object | None = None) -> None: ...

    async def mark_replaced(
        self,
        session: RefreshSession,
        *,
        replaced_by_hash: str,
        at: object | None = None,
    ) -> None:
        """Consume a token as part of a rotation and point at its successor."""
        ...

    async def revoke_family(self, family_id: str, *, at: object | None = None) -> int: ...

    async def revoke_all_for_user(self, user_id: UserId, *, at: object | None = None) -> int: ...

    async def revoke_for_device(
        self, user_id: UserId, device_id: uuid.UUID, *, at: object | None = None
    ) -> int: ...

    async def purge_expired(self, *, before: object | None = None) -> int:
        """Delete rows whose ``expires_at`` has passed, and return how many.

        Rotation appends a row per issue and revokes rather than deletes, so the table
        only grows: a learner who opens the app daily for a year leaves hundreds of dead
        rows behind. Once a token has expired it can no longer authenticate anything, so
        the row is history with no reader — deleting it cannot shorten a session or hide
        a reuse attack, which is what revocation is for.

        Replaced, revoked-but-unexpired rows are deliberately *kept*: a revoked token
        that is presented again is a reuse signal, and that signal has to outlive the
        revocation for the lifetime of the token at least.
        """
        ...


@runtime_checkable
class UserDeviceRepositoryPort(Protocol):
    async def get(self, device_id: uuid.UUID) -> UserDevice | None: ...

    async def upsert_for_token(self, device: UserDevice) -> UserDevice: ...

    async def list_active(self, user_id: UserId) -> list[UserDevice]: ...

    async def revoke(self, device_id: uuid.UUID, *, at: object | None = None) -> None: ...


__all__ = [
    "ExternalIdentityRepositoryPort",
    "RefreshTokenRepositoryPort",
    "UserDeviceRepositoryPort",
    "UserRepositoryPort",
]
