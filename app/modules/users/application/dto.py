"""Application-layer data transfer objects for the users module.

Commands and results are separate types on purpose. A handler receives a validated
command, the service returns a result, and neither leaks an ORM row or a FastAPI model
into the domain.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.types import (
    CEFRLevel,
    ContentLanguage,
    EnglishVariant,
    Locale,
    UserId,
)
from app.modules.users.domain.entities import NotificationPreferences, User
from app.modules.users.domain.identity import IdentityProvider


@dataclass(frozen=True, slots=True)
class DeviceContext:
    """Where a request came from. Recorded against sessions so a learner can see
    and revoke individual devices later."""

    platform: str = "web"
    push_token: str | None = None
    app_version: str | None = None
    user_agent: str | None = None
    ip_address: str | None = None


@dataclass(frozen=True, slots=True)
class SocialLoginCommand:
    """Sign in with a provider ID token, creating the account on first sight.

    ``id_token`` is the raw JWT the client obtained from the Google or Apple SDK. It is
    a credential: it is verified and then discarded, and must never be logged or stored.

    The profile fields are only consulted when a brand-new account is created. A
    returning learner keeps whatever they set, so a stale value on the device cannot
    overwrite a preference the learner changed elsewhere.
    """

    provider: IdentityProvider
    id_token: str
    device: DeviceContext = field(default_factory=DeviceContext)
    display_name: str | None = None
    native_language: ContentLanguage = ContentLanguage.AR
    english_variant: EnglishVariant = EnglishVariant.BRITISH
    timezone: str = "Asia/Baghdad"
    locale: Locale = Locale.EN_GB


@dataclass(frozen=True, slots=True)
class RefreshCommand:
    refresh_token: str
    device: DeviceContext = field(default_factory=DeviceContext)


@dataclass(frozen=True, slots=True)
class LogoutCommand:
    refresh_token: str
    #: Revoke every session for the account, not just this device's chain.
    all_devices: bool = False


@dataclass(frozen=True, slots=True)
class UpdateProfileCommand:
    """All fields optional: PATCH semantics. ``None`` means "leave unchanged"."""

    display_name: str | None = None
    avatar_key: str | None = None
    native_language: ContentLanguage | None = None
    english_variant: EnglishVariant | None = None
    timezone: str | None = None
    current_level: CEFRLevel | None = None


@dataclass(frozen=True, slots=True)
class UpdateSettingsCommand:
    locale: Locale | None = None
    explanation_language: ContentLanguage | None = None
    #: A *partial* preference map, not a whole object. The service merges it onto the
    #: stored value inside its own transaction; pre-merging in the caller would need a
    #: separate read, which races any concurrent PATCH and can lose an update.
    notification_preferences: Mapping[str, Any] | None = None
    daily_goal_minutes: int | None = None
    daily_goal_new_words: int | None = None


# ------------------------------------------------------------------------ results
@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int = 0
    refresh_expires_in: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "refresh_expires_in": self.refresh_expires_in,
        }


@dataclass(frozen=True, slots=True)
class UserProfileView:
    display_name: str
    avatar_key: str | None
    native_language: str
    english_variant: str
    timezone: str
    current_level: str

    @classmethod
    def of(cls, profile: Any) -> UserProfileView:
        return cls(
            display_name=profile.display_name,
            avatar_key=profile.avatar_key,
            native_language=str(profile.native_language),
            english_variant=str(profile.english_variant),
            timezone=profile.timezone,
            current_level=str(profile.current_level),
        )


@dataclass(frozen=True, slots=True)
class NotificationPreferencesView:
    email: bool
    push: bool
    daily_reminder: bool
    streak_warning: bool
    review_due: bool
    weekly_report: bool
    quiet_hours_start: int
    quiet_hours_end: int

    @classmethod
    def of(cls, prefs: NotificationPreferences) -> NotificationPreferencesView:
        return cls(**prefs.to_dict())

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


@dataclass(frozen=True, slots=True)
class UserSettingsView:
    locale: str
    explanation_language: str
    notification_preferences: NotificationPreferencesView
    daily_goal_minutes: int
    daily_goal_new_words: int

    @classmethod
    def of(cls, settings: Any) -> UserSettingsView:
        return cls(
            locale=str(settings.locale),
            explanation_language=str(settings.explanation_language),
            notification_preferences=NotificationPreferencesView.of(
                settings.notification_preferences
            ),
            daily_goal_minutes=settings.daily_goal_minutes,
            daily_goal_new_words=settings.daily_goal_new_words,
        )


@dataclass(frozen=True, slots=True)
class MeView:
    """The authenticated learner's own account. Never contains another user's data."""

    id: UserId
    email: str
    status: str
    roles: tuple[str, ...]
    email_verified: bool
    created_at: datetime
    profile: UserProfileView
    settings: UserSettingsView

    @classmethod
    def of(cls, user: User, profile: Any, settings: Any) -> MeView:
        return cls(
            id=user.id,
            email=user.email,
            status=str(user.status),
            roles=tuple(str(role) for role in user.roles),
            email_verified=user.is_email_verified,
            created_at=user.created_at,
            profile=UserProfileView.of(profile),
            settings=UserSettingsView.of(settings),
        )


@dataclass(frozen=True, slots=True)
class AuthResult:
    """What every successful auth endpoint returns."""

    user: MeView
    tokens: TokenPair

    @property
    def user_id(self) -> UserId:
        return self.user.id


@dataclass(frozen=True, slots=True)
class DeviceView:
    id: uuid.UUID
    platform: str
    app_version: str | None
    last_seen_at: datetime | None
    push_enabled: bool

    @classmethod
    def of(cls, device: Any) -> DeviceView:
        return cls(
            id=device.id,
            platform=device.platform,
            app_version=device.app_version,
            last_seen_at=device.last_seen_at,
            push_enabled=bool(device.push_token) and device.is_active,
        )


__all__ = [
    "AuthResult",
    "DeviceContext",
    "DeviceView",
    "LogoutCommand",
    "MeView",
    "NotificationPreferencesView",
    "RefreshCommand",
    "SocialLoginCommand",
    "TokenPair",
    "UpdateProfileCommand",
    "UpdateSettingsCommand",
    "UserProfileView",
    "UserSettingsView",
]
