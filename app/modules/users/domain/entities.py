"""Users and authentication aggregates.

Two rules drive the shapes here:

* **A learner's calendar and language preferences belong to the user, not the
  request.** ``UserProfile.timezone`` decides when a streak day rolls over, and
  ``UserSettings.explanation_language`` decides which explanation a learner sees. The
  client may *suggest* a value; the server validates and stores it.
* **Authentication state is data, not a JWT claim.** Token revocation must survive a
  restart and be immediately revocable, so refresh-token families live in rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.time import DEFAULT_TIMEZONE, ensure_aware, utc_now
from app.core.types import (
    CEFRLevel,
    ContentLanguage,
    EnglishVariant,
    Locale,
    UserId,
    UserRole,
    UserStatus,
)


@dataclass(frozen=True, slots=True)
class NotificationPreferences:
    """Opt-in switches for each delivery channel.

    Stored as JSON so adding a channel later is a data change, not a migration.
    """

    email: bool = True
    push: bool = True
    daily_reminder: bool = True
    streak_warning: bool = True
    review_due: bool = True
    weekly_report: bool = False
    quiet_hours_start: int = 22
    quiet_hours_end: int = 8

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "push": self.push,
            "daily_reminder": self.daily_reminder,
            "streak_warning": self.streak_warning,
            "review_due": self.review_due,
            "weekly_report": self.weekly_report,
            "quiet_hours_start": self.quiet_hours_start,
            "quiet_hours_end": self.quiet_hours_end,
        }

    def merged_with(self, overrides: Mapping[str, Any]) -> NotificationPreferences:
        """Return a copy with only the named switches replaced.

        PATCH means "leave everything I did not mention alone". Rebuilding from
        dataclass defaults instead would silently re-enable a channel the learner had
        turned off, which for opt-out settings is the worst possible default.

        ``None`` is treated as "not mentioned" rather than as a value, so a client that
        serialises unset fields explicitly still cannot blank a switch by accident.
        Unknown keys are rejected instead of ignored: a typo in a channel name would
        otherwise be accepted and discarded, leaving the user with no way to tell.
        """
        unknown = set(overrides) - set(self.to_dict())
        if unknown:
            raise ValueError(f"Unknown notification preference(s): {sorted(unknown)}")
        provided = {key: value for key, value in overrides.items() if value is not None}
        return NotificationPreferences.from_dict({**self.to_dict(), **provided})

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> NotificationPreferences:
        if not raw:
            return cls()
        defaults = cls()
        return cls(
            email=bool(raw.get("email", defaults.email)),
            push=bool(raw.get("push", defaults.push)),
            daily_reminder=bool(raw.get("daily_reminder", defaults.daily_reminder)),
            streak_warning=bool(raw.get("streak_warning", defaults.streak_warning)),
            review_due=bool(raw.get("review_due", defaults.review_due)),
            weekly_report=bool(raw.get("weekly_report", defaults.weekly_report)),
            quiet_hours_start=int(raw.get("quiet_hours_start", defaults.quiet_hours_start)),
            quiet_hours_end=int(raw.get("quiet_hours_end", defaults.quiet_hours_end)),
        )


@dataclass(slots=True)
class User:
    """The authentication root: identity and account lifecycle.

    There is no credential here by design. A learner proves who they are through a
    Google or Apple ID token, so the account holds no password to store, hash, reset or
    leak; the provider owns that problem.
    """

    id: UserId
    email: str
    status: UserStatus = UserStatus.ACTIVE
    roles: tuple[UserRole, ...] = (UserRole.USER,)
    email_verified_at: datetime | None = None
    deleted_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    # ------------------------------------------------------------- lifecycle
    @property
    def is_email_verified(self) -> bool:
        return self.email_verified_at is not None

    @property
    def is_deleted(self) -> bool:
        return self.status is UserStatus.DELETED

    def verify_email(self, at: datetime | None = None) -> None:
        self.email_verified_at = at or utc_now()

    def soft_delete(self, at: datetime | None = None) -> None:
        """Close the account without destroying the row.

        The row survives because plenty of legitimate history hangs off it -- quiz attempts,
        SRS cards, streak days -- and a hard delete would either cascade that away or leave
        it dangling. What must not survive is *access*, and that is carried by the status:
        ``DELETED`` fails ``is_authenticatable`` everywhere credentials are checked, so
        login, refresh and every authenticated route stop admitting this account.

        Idempotent on purpose. A learner who taps the button twice, or retries after a
        dropped response, must not get an error for something that already succeeded.
        """
        if self.status is UserStatus.DELETED:
            return
        self.status = UserStatus.DELETED
        self.deleted_at = at or utc_now()
        self.updated_at = self.deleted_at

    def suspend(self) -> None:
        self.status = UserStatus.SUSPENDED

    def is_authenticatable(self) -> bool:
        return self.status in (UserStatus.ACTIVE, UserStatus.PENDING_VERIFICATION)

    def has_role(self, minimum: UserRole) -> bool:
        return any(role.at_least(minimum) for role in self.roles)


@dataclass(slots=True)
class UserProfile:
    """Learning identity. Separate from :class:`User` so a profile edit never
    touches credential rows, and credential changes never touch profile rows."""

    user_id: UserId
    display_name: str
    avatar_key: str | None = None
    native_language: ContentLanguage = ContentLanguage.AR
    english_variant: EnglishVariant = EnglishVariant.BRITISH
    timezone: str = DEFAULT_TIMEZONE
    current_level: CEFRLevel = CEFRLevel.A1
    placement_test_completed_at: datetime | None = None

    @property
    def initial(self) -> str:
        return self.display_name.strip()[:1].upper() or "?"


@dataclass(slots=True)
class UserSettings:
    """Per-user behaviour switches."""

    user_id: UserId
    locale: Locale = Locale.EN_GB
    explanation_language: ContentLanguage = ContentLanguage.EN
    notification_preferences: NotificationPreferences = field(
        default_factory=NotificationPreferences
    )
    daily_goal_minutes: int = 30
    daily_goal_new_words: int = 10


@dataclass(frozen=True, slots=True)
class RefreshSession:
    """A persisted refresh token record.

    Only the SHA-256 hash of the ``jti`` is stored, so a database dump cannot be
    replayed. ``family_id`` chains rotations: replaying any consumed token revokes the
    whole chain, which is how token theft is detected (plan §13).
    """

    id: uuid.UUID
    user_id: UserId
    jti_hash: str
    family_id: str
    device_id: uuid.UUID | None
    expires_at: datetime
    created_at: datetime
    revoked_at: datetime | None = None
    replaced_by_hash: str | None = None
    user_agent: str | None = None
    ip_address: str | None = None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, at: datetime | None = None) -> bool:
        return ensure_aware(self.expires_at) <= (at or utc_now())

    def is_usable(self, at: datetime | None = None) -> bool:
        return not self.is_revoked and not self.is_expired(at)


@dataclass(frozen=True, slots=True)
class UserDevice:
    """A device the learner installed the app on, used for push delivery and for
    revoking sessions by device."""

    id: uuid.UUID
    user_id: UserId
    platform: str
    created_at: datetime = field(default_factory=utc_now)
    push_token: str | None = None
    app_version: str | None = None
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """A sign-in account at a provider, linked to exactly one :class:`User`.

    ``(provider, subject)`` is unique and is how an account is recognised on the next
    sign-in: the subject is the provider's stable, opaque user id, which unlike an email
    never changes and is never reassigned to a different person. One learner may hold
    several rows — a Google identity and an Apple identity for the same human — which is
    why this is its own table rather than columns on ``users``.
    """

    id: uuid.UUID
    user_id: UserId
    provider: str
    subject: str
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None
    picture_url: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    last_login_at: datetime = field(default_factory=utc_now)
