"""Request and response schemas for the auth and profile endpoints.

Two conventions are enforced here rather than in each handler:

* **No field is trusted because the client sent it.** ``current_level`` is not
  accepted on ``PATCH /me`` at all; the placement test owns it.
* **Enum and length validation happens at the edge** so the service layer receives
  already-typed values and never re-parses strings.

There is no credential schema because there is no credential: a learner signs in with an
ID token from Google or Apple, which the server verifies before anything else happens.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from app.core.responses import WireModel
from app.core.types import ContentLanguage, EnglishVariant, Locale

DisplayName = Annotated[str, Field(min_length=2, max_length=80, examples=["Ali"])]
#: An ID token is a signed JWT, so it is a few hundred characters in practice. The upper
#: bound is generous but finite: one that is 8 KiB is an attack, not a credential.
IdToken = Annotated[str, Field(min_length=16, max_length=8192)]
PushToken = Annotated[str, Field(min_length=8, max_length=512)]
IanaTimezone = Annotated[
    str,
    Field(min_length=1, max_length=64, examples=["Asia/Baghdad"]),
]


class DevicePayload(WireModel):
    """Device hints recorded against a session.

    Optional on every endpoint: a web client may send nothing, and the server must
    still issue a usable session.
    """

    model_config = ConfigDict(extra="forbid")

    platform: Literal["web", "ios", "android"] = "web"
    push_token: PushToken | None = None
    app_version: Annotated[str, Field(max_length=32)] | None = None
    user_agent: Annotated[str, Field(max_length=255)] | None = None


# ---------------------------------------------------------------------- auth
class SocialLoginRequest(WireModel):
    """Body of ``POST /auth/google`` and ``POST /auth/apple``.

    The profile fields are advisory: they are applied only when the request creates a
    brand-new account. A returning learner keeps the preferences they already have, so a
    stale value on a device cannot silently reset a setting changed elsewhere.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id_token: IdToken
    device: DevicePayload = Field(default_factory=DevicePayload)
    display_name: DisplayName | None = None
    native_language: ContentLanguage = ContentLanguage.AR
    english_variant: EnglishVariant = EnglishVariant.BRITISH
    timezone: IanaTimezone = "Asia/Baghdad"
    locale: Locale = Locale.EN_GB


class DevLoginRequest(WireModel):
    """Body of ``POST /auth/dev-login``.

    The credential check is the only one in the service: everywhere else the provider
    owns the password and the server never sees one. That is exactly why this route is
    flag-guarded (``DEV_LOGIN_ENABLED``, off by default and refused in production)
    rather than always present.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(min_length=1, max_length=128)]
    device: DevicePayload = Field(default_factory=DevicePayload)


class RefreshRequest(WireModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: Annotated[str, Field(min_length=16, max_length=4096)]
    device: DevicePayload = Field(default_factory=DevicePayload)


class LogoutRequest(WireModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: Annotated[str, Field(min_length=16, max_length=4096)]
    all_devices: bool = False


class TokenPairResponse(WireModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int
    refresh_expires_in: int


class UserProfileResponse(WireModel):
    display_name: str
    avatar_key: str | None
    native_language: str
    english_variant: str
    timezone: str
    current_level: str


class NotificationPreferencesResponse(WireModel):
    email: bool
    push: bool
    daily_reminder: bool
    streak_warning: bool
    review_due: bool
    weekly_report: bool
    quiet_hours_start: int = Field(ge=0, le=23)
    quiet_hours_end: int = Field(ge=0, le=23)


class UserSettingsResponse(WireModel):
    locale: str
    explanation_language: str
    notification_preferences: NotificationPreferencesResponse
    daily_goal_minutes: int
    daily_goal_new_words: int


class MeResponse(WireModel):
    id: str
    email: str
    status: str
    is_admin: bool
    email_verified: bool
    created_at: datetime
    #: Whole days since the account was created. The app's "days using Natiqo" figure,
    #: derived server-side from ``created_at`` so every device agrees on it and the
    #: client never has to trust its own clock.
    days_since_signup: int
    profile: UserProfileResponse
    settings: UserSettingsResponse


class AuthResponse(WireModel):
    user: MeResponse
    tokens: TokenPairResponse


class MessageResponse(WireModel):
    """Used by endpoints that report an outcome without returning a resource."""

    detail: str


class DeviceResponse(WireModel):
    id: str
    platform: str
    app_version: str | None
    last_seen_at: datetime | None
    push_enabled: bool


# ------------------------------------------------------------------ patch /me
class NotificationPreferencesPatch(WireModel):
    model_config = ConfigDict(extra="forbid")

    email: bool | None = None
    push: bool | None = None
    daily_reminder: bool | None = None
    streak_warning: bool | None = None
    review_due: bool | None = None
    weekly_report: bool | None = None
    quiet_hours_start: Annotated[int, Field(ge=0, le=23)] | None = None
    quiet_hours_end: Annotated[int, Field(ge=0, le=23)] | None = None


class UpdateMeRequest(WireModel):
    """PATCH semantics: an absent field is left untouched.

    ``current_level`` is intentionally absent. A learner choosing their own level
    would bypass the placement test that every later difficulty decision assumes.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: DisplayName | None = None
    avatar_key: Annotated[str, Field(max_length=255)] | None = None
    native_language: ContentLanguage | None = None
    english_variant: EnglishVariant | None = None
    timezone: IanaTimezone | None = None
    locale: Locale | None = None
    explanation_language: ContentLanguage | None = None
    notification_preferences: NotificationPreferencesPatch | None = None
    daily_goal_minutes: Annotated[int, Field(ge=5, le=600)] | None = None
    daily_goal_new_words: Annotated[int, Field(ge=1, le=200)] | None = None


#: PATCH /me/daily-challenge body
class UpdateDailyChallengePayload(WireModel):
    """Update daily challenge progress.

    Increment lessons completed and/or words learned for today. Both fields are optional:
    sending only one updates just that counter; the other stays as-is.
    """

    model_config = ConfigDict(extra="forbid")

    lessons_completed_today: Annotated[int, Field(ge=0)] | None = None
    words_learned_today: Annotated[int, Field(ge=0)] | None = None


# GET/PATCH/POST /me/daily-challenge response
class DailyChallengeResponse(WireModel):
    """One calendar day's challenge record for the signed-in learner.

    Serialised like every other wire model (camelCase) rather than as the domain
    dataclass, whose snake_case field names would be the one place in the API that
    contradicted the naming contract the Flutter client generates types from.

    ``challengeDate`` is the learner's own calendar date (from their profile timezone),
    so it can legitimately be a day behind the server's UTC date near midnight.
    """

    challenge_date: str
    lessons_completed_today: int
    words_learned_today: int
    claimed: bool


# The error envelope is not declared here. It is owned by
# ``app.core.responses.ErrorResponse`` (``{"error": {"code", "message",
# "details", "request_id"}}``), is what the exception handlers actually emit, and is
# what ``ERROR_RESPONSES`` publishes in the OpenAPI document. Declaring a second,
# differently-shaped model here would have been a lie the spec published to clients.

__all__ = [
    "AuthResponse",
    "DailyChallengeResponse",
    "DevicePayload",
    "DeviceResponse",
    "LogoutRequest",
    "MeResponse",
    "MessageResponse",
    "NotificationPreferencesPatch",
    "NotificationPreferencesResponse",
    "RefreshRequest",
    "SocialLoginRequest",
    "TokenPairResponse",
    "UpdateDailyChallengePayload",
    "UpdateMeRequest",
    "UserProfileResponse",
    "UserSettingsResponse",
]
