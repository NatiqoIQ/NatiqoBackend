"""Persistence ports for the users module.

The application layer talks to these, never to SQLAlchemy. That is what lets the auth
use cases be unit-tested against an in-memory fake with no database at all.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.core.time import ensure_aware, utc_now
from app.core.types import UserId
from app.modules.users.domain.entities import (
    RefreshSession,
    User,
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

    async def get_profile(self, user_id: UserId) -> UserProfile | None: ...

    async def save_profile(self, profile: UserProfile) -> None: ...

    async def get_settings(self, user_id: UserId) -> UserSettings | None: ...

    async def save_settings(self, settings: UserSettings) -> None: ...


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


@runtime_checkable
class UserDeviceRepositoryPort(Protocol):
    async def get(self, device_id: uuid.UUID) -> UserDevice | None: ...

    async def upsert_for_token(self, device: UserDevice) -> UserDevice: ...

    async def list_active(self, user_id: UserId) -> list[UserDevice]: ...

    async def revoke(self, device_id: uuid.UUID, *, at: object | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class PasswordResetRecord:
    user_id: UserId
    token_hash: str
    expires_at: datetime
    created_at: datetime
    used_at: datetime | None = None
    requested_ip: str | None = None

    def is_usable(self, at: datetime | None = None) -> bool:
        return self.used_at is None and ensure_aware(self.expires_at) > (at or utc_now())


@runtime_checkable
class PasswordResetRepositoryPort(Protocol):
    """Single-use password-reset tokens, stored as digests only."""

    async def add(
        self,
        *,
        user_id: UserId,
        token_hash: str,
        expires_at: datetime,
        requested_ip: str | None = None,
    ) -> None: ...

    async def get(self, token_hash: str) -> PasswordResetRecord | None: ...

    async def consume(self, token_hash: str, *, at: datetime | None = None) -> None: ...

    async def revoke_outstanding(self, user_id: UserId, *, at: datetime | None = None) -> int: ...


@runtime_checkable
class EmailVerificationRepositoryPort(Protocol):
    """Single-use email-verification tokens, stored as digests only.

    Mirrors :class:`PasswordResetRepositoryPort` because the storage problem is
    identical — an opaque token whose digest is the only persisted form — while
    deliberately staying a separate port so the two flows cannot revoke each other's
    tokens by accident.
    """

    async def add(
        self,
        *,
        user_id: UserId,
        token_hash: str,
        expires_at: datetime,
        requested_ip: str | None = None,
    ) -> None: ...

    async def get(self, token_hash: str) -> PasswordResetRecord | None: ...

    async def consume(self, token_hash: str, *, at: datetime | None = None) -> None: ...

    async def revoke_outstanding(self, user_id: UserId, *, at: datetime | None = None) -> int: ...

    async def has_usable(self, user_id: UserId, *, at: datetime | None = None) -> bool:
        """Whether this account has a live, unused verification link.

        Lets the resend endpoint skip sending when a working link is already in flight.
        Re-sending unconditionally turns "I did not get it" into a mail flood against
        the learner's own inbox, which is how an address gets rate-limited by its
        provider and stops receiving anything at all.
        """
        ...


class EmailDeliveryError(RuntimeError):
    """Raised when a delivery port could not hand a message to its channel.

    Declared beside the ports rather than inside an adapter because the application layer
    has to catch it, and that catch is a security requirement rather than a nicety: if an
    SMTP outage escapes the forgot-password use case, the client gets a 500 for a real
    account and a different answer for an unknown one, which turns an outage into an
    account-existence oracle.

    Carries no body, no recipient and no token. This exception is logged, and a logged
    exception must not become a credential leak.
    """


@runtime_checkable
class PasswordResetDeliveryPort(Protocol):
    """Outbound channel for a password-reset link.

    A port rather than a direct mail call so the auth use case never depends on a
    provider, and so a test can assert a link was sent without touching a network.

    Implementations must treat ``raw_token`` as a secret: it is the only copy that
    exists anywhere, and it must never reach a log, an error message or an analytics
    event.
    """

    async def send_reset_link(
        self,
        *,
        user_id: UserId,
        email: str,
        raw_token: str,
        expires_at: datetime,
    ) -> None: ...


@runtime_checkable
class EmailVerificationDeliveryPort(Protocol):
    """Outbound channel for an email-verification link.

    Separate from :class:`PasswordResetDeliveryPort` even though both send a token in a
    link, because the two flows have opposite security properties. A reset link is
    requested by whoever knows the address, so delivery must never reveal whether an
    account exists. A verification link is only meaningful to the account owner, so it
    carries no such constraint and can say "this address is not verified yet".

    Same secret-handling rule as the reset port: ``raw_token`` never reaches a log.
    """

    async def send_verification_link(
        self,
        *,
        user_id: UserId,
        email: str,
        raw_token: str,
        expires_at: datetime,
    ) -> None: ...


#: Backwards-compatible alias. The UoW exposes the repository under this key.
UserRepository = UserRepositoryPort
