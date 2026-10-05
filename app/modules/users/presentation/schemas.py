"""Request and response schemas for the auth and profile endpoints.

Two conventions are enforced here rather than in each handler:

* **No field is trusted because the client sent it.** ``current_level`` is not
  accepted on ``PATCH /me`` at all; the placement test owns it.
* **Enum and length validation happens at the edge** so the service layer receives
  already-typed values and never re-parses strings.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, EmailStr, Field

from app.core.responses import WireModel
from app.core.types import ContentLanguage, EnglishVariant, Locale

DisplayName = Annotated[str, Field(min_length=2, max_length=80, examples=["Ali"])]
Password = Annotated[str, Field(min_length=1, max_length=256)]
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
class RegisterRequest(WireModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr
    password: Password
    display_name: DisplayName
    native_language: ContentLanguage = ContentLanguage.AR
    english_variant: EnglishVariant = EnglishVariant.BRITISH
    timezone: IanaTimezone = "Asia/Baghdad"
    locale: Locale = Locale.EN_GB
    device: DevicePayload = Field(default_factory=DevicePayload)


class LoginRequest(WireModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr
    password: Password
    device: DevicePayload = Field(default_factory=DevicePayload)


class RefreshRequest(WireModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: Annotated[str, Field(min_length=16, max_length=4096)]
    device: DevicePayload = Field(default_factory=DevicePayload)


class LogoutRequest(WireModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: Annotated[str, Field(min_length=16, max_length=4096)]
    all_devices: bool = False


class ForgotPasswordRequest(WireModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr


class ResetPasswordRequest(WireModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    token: Annotated[str, Field(min_length=16, max_length=512)]
    new_password: Password


class VerifyEmailRequest(WireModel):
    """Body of ``POST /auth/verify-email``.

    A ``POST`` rather than a ``GET`` even though it only changes a timestamp: a link is
    followed by clients, prefetchers and mail scanners, and a ``GET`` that mutates is
    consumed by all three without the learner ever seeing the confirmation.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    token: Annotated[str, Field(min_length=16, max_length=512)]


class ChangePasswordRequest(WireModel):
    """Body of ``POST /auth/change-password``.

    ``currentPassword`` is required rather than optional. An authenticated caller who
    cannot supply it does not need this endpoint — they need ``/auth/forgot-password``,
    which proves ownership of the mailbox instead.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    current_password: Password
    new_password: Password


class ChangePasswordResponse(WireModel):
    """Signals that every session was revoked, so the client must re-authenticate.

    Returned instead of a plain message because the client has a concrete action to take:
    dropping its tokens and sending the learner back to the sign-in screen. A learner left
    on a screen that looks signed-in, with a session that no longer works, is a support
    ticket every time.
    """

    detail: str
    sessions_revoked: int


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
    roles: list[str]
    email_verified: bool
    created_at: datetime
    profile: UserProfileResponse
    settings: UserSettingsResponse


class AuthResponse(WireModel):
    user: MeResponse
    tokens: TokenPairResponse


class MessageResponse(WireModel):
    """Used by endpoints that must not disclose whether an account exists."""

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


# The error envelope is not declared here. It is owned by
# ``app.core.responses.ErrorResponse`` (``{"error": {"code", "message",
# "details", "request_id"}}``), is what the exception handlers actually emit, and is
# what ``ERROR_RESPONSES`` publishes in the OpenAPI document. Declaring a second,
# differently-shaped model here would have been a lie the spec published to clients.

__all__ = [
    "AuthResponse",
    "DevicePayload",
    "DeviceResponse",
    "ForgotPasswordRequest",
    "LoginRequest",
    "LogoutRequest",
    "MeResponse",
    "MessageResponse",
    "NotificationPreferencesPatch",
    "NotificationPreferencesResponse",
    "RefreshRequest",
    "RegisterRequest",
    "ResetPasswordRequest",
    "TokenPairResponse",
    "UpdateMeRequest",
    "UserProfileResponse",
    "UserSettingsResponse",
]
