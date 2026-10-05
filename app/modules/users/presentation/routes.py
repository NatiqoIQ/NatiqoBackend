"""Presentation layer for authentication and the current user's profile.

Handlers are thin on purpose: parse, delegate to :class:`AuthService`, render. Any
branching about *what a user may do* belongs in the service, where it can be tested
without a request object.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import AuthRateLimitDep, SettingsDep, UowDep, client_ip
from app.core.errors import UnauthorizedError, ValidationFailed
from app.core.types import UserRole
from app.modules.users.application.auth_service import AuthService
from app.modules.users.application.dto import (
    AuthResult,
    ChangePasswordCommand,
    DeviceContext,
    DeviceView,
    ForgotPasswordCommand,
    LoginCommand,
    LogoutCommand,
    MeView,
    RefreshCommand,
    RegisterCommand,
    ResendVerificationCommand,
    ResetPasswordCommand,
    UpdateProfileCommand,
    UpdateSettingsCommand,
    VerifyEmailCommand,
)
from app.modules.users.domain.entities import User
from app.modules.users.infrastructure.delivery import build_delivery
from app.modules.users.infrastructure.email import build_smtp_sender
from app.modules.users.presentation.schemas import (
    AuthResponse,
    ChangePasswordRequest,
    ChangePasswordResponse,
    DevicePayload,
    DeviceResponse,
    ForgotPasswordRequest,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    MessageResponse,
    NotificationPreferencesResponse,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenPairResponse,
    UpdateMeRequest,
    UserProfileResponse,
    UserSettingsResponse,
    VerifyEmailRequest,
)

router = APIRouter(tags=["auth"])

#: ``auto_error=False`` so a missing header produces this module's own 401 payload
#: rather than FastAPI's bare ``{"detail": "Not authenticated"}``, which breaks the
#: stable error-code contract every client relies on.
bearer_scheme = HTTPBearer(auto_error=False)


def auth_service(uow: UowDep, settings: SettingsDep) -> AuthService:
    # Reset delivery defaults to the audit-only adapter when SMTP is unconfigured, so a
    # local run is honest about not sending mail. When SMTP_HOST is set, the link is
    # actually emailed. Tests and local flows override with a collector so they can read
    # the link the use case dispatched.
    return AuthService(
        uow,
        settings,
        reset_delivery=build_delivery(
            production=settings.is_production,
            smtp_sender=build_smtp_sender(
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user,
                password=settings.smtp_password.get_secret_value()
                if settings.smtp_password
                else None,
                sender=settings.smtp_from,
                use_tls=settings.smtp_use_tls,
            ),
            public_web_base_url=settings.public_web_base_url,
        ),
    )


AuthServiceDep = Annotated[AuthService, Depends(auth_service)]


def _device(payload: DevicePayload, request: Request) -> DeviceContext:
    return DeviceContext(
        platform=payload.platform,
        push_token=payload.push_token,
        app_version=payload.app_version,
        user_agent=payload.user_agent or request.headers.get("user-agent"),
        ip_address=client_ip(request),
    )


def _auth_response(result: AuthResult) -> AuthResponse:
    return AuthResponse(
        user=_me_response(result.user), tokens=TokenPairResponse(**result.tokens.to_dict())
    )


def _me_response(view: MeView) -> MeResponse:
    return MeResponse(
        id=str(view.id),
        email=view.email,
        status=view.status,
        roles=list(view.roles),
        email_verified=view.email_verified,
        created_at=view.created_at,
        profile=UserProfileResponse(
            display_name=view.profile.display_name,
            avatar_key=view.profile.avatar_key,
            native_language=view.profile.native_language,
            english_variant=view.profile.english_variant,
            timezone=view.profile.timezone,
            current_level=view.profile.current_level,
        ),
        settings=UserSettingsResponse(
            locale=view.settings.locale,
            explanation_language=view.settings.explanation_language,
            notification_preferences=NotificationPreferencesResponse(
                **view.settings.notification_preferences.to_dict()
            ),
            daily_goal_minutes=view.settings.daily_goal_minutes,
            daily_goal_new_words=view.settings.daily_goal_new_words,
        ),
    )


# ==================================================================== endpoints
@router.post(
    "/auth/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
async def register(
    payload: RegisterRequest, request: Request, service: AuthServiceDep
) -> AuthResponse:
    """Sign up and immediately receive a session.

    Registration is a login: making a new learner call a second endpoint before they
    can start a lesson is friction with no security benefit — the refresh token is
    stored hashed and is revocable exactly like one from a login.
    """
    result = await service.register(
        RegisterCommand(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            native_language=payload.native_language,
            english_variant=payload.english_variant,
            timezone=payload.timezone,
            locale=payload.locale,
            device=_device(payload.device, request),
        )
    )
    return _auth_response(result)


@router.post("/auth/login", response_model=AuthResponse, summary="Exchange credentials for tokens")
async def login(payload: LoginRequest, request: Request, service: AuthServiceDep) -> AuthResponse:
    result = await service.login(
        LoginCommand(
            email=payload.email,
            password=payload.password,
            device=_device(payload.device, request),
        )
    )
    return _auth_response(result)


@router.post("/auth/refresh", response_model=AuthResponse, summary="Rotate a refresh token")
async def refresh(
    payload: RefreshRequest, request: Request, service: AuthServiceDep
) -> AuthResponse:
    result = await service.refresh(
        RefreshCommand(
            refresh_token=payload.refresh_token,
            device=_device(payload.device, request),
        )
    )
    return _auth_response(result)


@router.post(
    "/auth/logout",
    response_model=MessageResponse,
    summary="Revoke the presented session, or every session",
)
async def logout(payload: LogoutRequest, service: AuthServiceDep) -> MessageResponse:
    """Idempotent by design.

    A client that retries after a dropped response must not be told the logout
    "failed" when the server already revoked the token.
    """
    await service.logout(
        LogoutCommand(refresh_token=payload.refresh_token, all_devices=payload.all_devices)
    )
    return MessageResponse(detail="Signed out.")


@router.post(
    "/auth/forgot-password",
    response_model=MessageResponse,
    summary="Request a password-reset link",
)
async def forgot_password(
    payload: ForgotPasswordRequest, request: Request, service: AuthServiceDep
) -> MessageResponse:
    """Always reports success.

    Whether an address is registered is account state an unauthenticated caller has no
    business learning. The same response and status are returned either way, and the
    response body is byte-identical so even a size comparison reveals nothing.
    """
    # The token is handed to the delivery port inside the use case. It is deliberately
    # not returned here and not logged: a reset link in a log or an HTTP body is a
    # credential sitting in plaintext somewhere it cannot be rotated.
    await service.forgot_password(
        ForgotPasswordCommand(email=payload.email, ip_address=client_ip(request))
    )
    return MessageResponse(detail="If that address has an account, a reset link is on its way.")


@router.post(
    "/auth/reset-password",
    response_model=MessageResponse,
    summary="Complete a password reset",
)
async def reset_password(payload: ResetPasswordRequest, service: AuthServiceDep) -> MessageResponse:
    """Revokes every existing session as a side effect.

    A reset is the owner's statement that a password was lost or stolen, so any
    session opened with the old one is treated as hostile.
    """
    await service.reset_password(
        ResetPasswordCommand(token=payload.token, new_password=payload.new_password)
    )
    return MessageResponse(detail="Your password has been updated. Please sign in.")


@router.post(
    "/auth/verify-email",
    response_model=MessageResponse,
    summary="Confirm an email address from an emailed token",
)
async def verify_email(payload: VerifyEmailRequest, service: AuthServiceDep) -> MessageResponse:
    """Confirmation only — it does not grant or restore access.

    An unverified learner keeps working exactly as before, so this endpoint changes a
    flag the client can display, not a permission. Redeeming the same link twice
    succeeds both times: a mail scanner may open it before the learner does.
    """
    await service.verify_email(VerifyEmailCommand(token=payload.token))
    return MessageResponse(detail="Your email address is confirmed.")


@router.post(
    "/auth/resend-verification",
    response_model=MessageResponse,
    summary="Send another verification email",
)
async def resend_verification(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: AuthServiceDep,
    _limit: AuthRateLimitDep,
) -> MessageResponse:
    """Always reports success.

    Authenticated, unlike ``forgot-password``: an unauthenticated resend keyed on an
    address would be a way to flood somebody else's inbox. The response still does not
    say whether anything was sent, because "already confirmed" is state the client
    already has from ``/me`` and does not need the server to confirm.
    """
    user = await _authenticate(credentials, service)
    await service.resend_verification(
        ResendVerificationCommand(user_id=user.id, ip_address=client_ip(request))
    )
    return MessageResponse(
        detail="If your address still needs confirming, a new link is on its way."
    )


@router.post(
    "/auth/change-password",
    response_model=ChangePasswordResponse,
    summary="Change the password of the signed-in account",
)
async def change_password(
    payload: ChangePasswordRequest,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: AuthServiceDep,
    _limit: AuthRateLimitDep,
) -> ChangePasswordResponse:
    """Revokes every session, including the one making this request.

    That is intentional rather than punitive: the point of changing a password is to
    make the old credential useless everywhere, and the caller's own session is not an
    exception. The count comes back so the client can say something more useful than
    "done" before it drops the tokens.
    """
    user = await _authenticate(credentials, service)
    revoked = await service.change_password(
        ChangePasswordCommand(
            user_id=user.id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    )
    return ChangePasswordResponse(
        detail="Your password has been updated. Please sign in again.",
        sessions_revoked=revoked,
    )


# ============================================================================ me
@router.get("/me", response_model=MeResponse, summary="The signed-in learner")
async def get_me(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: AuthServiceDep,
) -> MeResponse:
    user = await _authenticate(credentials, service)
    return _me_response(await service.get_me(user.id))


@router.patch("/me", response_model=MeResponse, summary="Update profile and settings")
async def update_me(
    payload: UpdateMeRequest,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: AuthServiceDep,
) -> MeResponse:
    user = await _authenticate(credentials, service)

    view = await service.update_me(
        user.id,
        profile_changes=UpdateProfileCommand(
            display_name=payload.display_name,
            avatar_key=payload.avatar_key,
            native_language=payload.native_language,
            english_variant=payload.english_variant,
            timezone=payload.timezone,
        ),
        settings_changes=UpdateSettingsCommand(
            locale=payload.locale,
            explanation_language=payload.explanation_language,
            # Passed through as sent. The service merges it onto the stored value in the
            # same transaction that writes it, so an absent field stays untouched without
            # this layer reading the current preferences first.
            #
            # ``by_alias=False`` is load-bearing. The domain entity is keyed by Python names
            # and rejects anything else by design — a typo must be an error, not a silently
            # discarded setting — so dumping aliases here would hand it ``dailyReminder``
            # and fail a request the client sent correctly. The wire spelling is this
            # layer's business and stops at this layer.
            notification_preferences=(
                None
                if payload.notification_preferences is None
                else payload.notification_preferences.model_dump(by_alias=False, exclude_unset=True)
            ),
            daily_goal_minutes=payload.daily_goal_minutes,
            daily_goal_new_words=payload.daily_goal_new_words,
        ),
    )
    return _me_response(view)


@router.delete("/me", status_code=204, summary="Close the signed-in learner's account")
async def delete_me(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: AuthServiceDep,
    _limit: AuthRateLimitDep,
) -> Response:
    """Close the account, keeping the record.

    204 with no body either way: the caller's own account is already closed, so there is
    nothing useful to tell them and no reason to make a retried request fail. Every session,
    reset link and verification link is revoked in the same transaction as the status
    change, so the account stops being reachable immediately rather than at token expiry.
    """
    user = await _authenticate(credentials, service)
    await service.delete_account(user.id)
    return Response(status_code=204)


@router.get("/me/devices", response_model=list[DeviceResponse], summary="List signed-in devices")
async def list_devices(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: AuthServiceDep,
) -> list[DeviceResponse]:
    """Scoped to the caller's own account: a device id from another user is not
    reachable from this route, so there is no cross-tenant read."""
    user = await _authenticate(credentials, service)
    devices: list[DeviceView] = await service.list_devices(user.id)
    return [
        DeviceResponse(
            id=str(device.id),
            platform=device.platform,
            app_version=device.app_version,
            last_seen_at=device.last_seen_at,
            push_enabled=device.push_enabled,
        )
        for device in devices
    ]


@router.delete(
    "/me/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign a device out and stop its push notifications",
)
async def revoke_device(
    device_id: str,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: AuthServiceDep,
) -> Response:
    user = await _authenticate(credentials, service)
    try:
        parsed = uuid.UUID(device_id)
    except ValueError as exc:
        raise ValidationFailed("Unknown device.", details={"field": "device_id"}) from exc
    await service.revoke_device(user.id, parsed)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ================================================================== dependencies
async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    service: AuthServiceDep,
) -> User:
    """Resolve the caller. Use as a route dependency: ``user: CurrentUser``."""
    return await _authenticate(credentials, service)


CurrentUser = Annotated[User, Depends(current_user)]


def require_role(minimum: UserRole) -> Callable[..., Awaitable[User]]:
    """Build a dependency enforcing a minimum role.

    Role checks read the live account, not the ``roles`` claim in the access token.
    A token minted before a demotion stays valid for up to its full TTL otherwise, and
    revoking a role is supposed to take effect immediately.
    """

    async def _dependency(user: CurrentUser, service: AuthServiceDep) -> User:
        return await service.require_role(user, minimum)

    return _dependency


async def _authenticate(
    credentials: HTTPAuthorizationCredentials | None, service: AuthService
) -> User:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Authentication is required.")
    if credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Only bearer tokens are accepted.")
    return await service.authenticate(credentials.credentials)


__all__ = ["AuthServiceDep", "CurrentUser", "current_user", "require_role", "router"]
