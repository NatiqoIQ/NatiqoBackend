"""Presentation layer for authentication and the current user's profile.

Handlers are thin on purpose: parse, delegate to :class:`AuthService`, render. Any
branching about *what a user may do* belongs in the service, where it can be tested
without a request object.

There is no sign-up endpoint distinct from sign-in. The first verified ID token from a
provider *is* the registration — asking a learner to press "create account" before an
account exists would add a step that changes nothing about what the server writes.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps import (
    AuthRateLimitDep,
    SettingsDep,
    UowDep,
    client_ip,
    get_identity_verifiers,
)
from app.core.errors import ErrorCode, NotFoundError, UnauthorizedError, ValidationFailed
from app.core.types import UserRole
from app.modules.users.application.auth_service import AuthService
from app.modules.users.application.dto import (
    AuthResult,
    DeviceContext,
    DeviceView,
    LogoutCommand,
    MeView,
    RefreshCommand,
    SocialLoginCommand,
    UpdateProfileCommand,
    UpdateSettingsCommand,
)
from app.modules.users.domain.entities import User
from app.modules.users.domain.identity import IdentityProvider, VerifiedIdentity
from app.modules.users.presentation.schemas import (
    AuthResponse,
    DevicePayload,
    DeviceResponse,
    DevLoginRequest,
    LogoutRequest,
    MeResponse,
    MessageResponse,
    NotificationPreferencesResponse,
    RefreshRequest,
    SocialLoginRequest,
    TokenPairResponse,
    UpdateMeRequest,
    UserProfileResponse,
    UserSettingsResponse,
)

router = APIRouter(tags=["auth"])

#: ``auto_error=False`` so a missing header produces this module's own 401 payload
#: rather than FastAPI's bare ``{"detail": "Not authenticated"}``, which breaks the
#: stable error-code contract every client relies on.
bearer_scheme = HTTPBearer(auto_error=False)


def auth_service(uow: UowDep, settings: SettingsDep) -> AuthService:
    # The verifier set is derived from configuration and cached for the life of the
    # process: a provider with no registered client ids gets no verifier, so its endpoint
    # reports that sign-in is not enabled instead of accepting a token nothing can check,
    # and each verifier keeps one JWK-set cache rather than refetching per request.
    return AuthService(uow, settings, verifiers=get_identity_verifiers())


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


async def _sign_in(
    provider: IdentityProvider,
    payload: SocialLoginRequest,
    request: Request,
    service: AuthService,
) -> AuthResponse:
    result = await service.login_with_identity(
        SocialLoginCommand(
            provider=provider,
            id_token=payload.id_token,
            device=_device(payload.device, request),
            display_name=payload.display_name,
            native_language=payload.native_language,
            english_variant=payload.english_variant,
            timezone=payload.timezone,
            locale=payload.locale,
        )
    )
    return _auth_response(result)


# ==================================================================== endpoints
@router.post(
    "/auth/google",
    response_model=AuthResponse,
    summary="Sign in with a Google ID token",
)
async def sign_in_with_google(
    payload: SocialLoginRequest,
    request: Request,
    service: AuthServiceDep,
    _limit: AuthRateLimitDep,
) -> AuthResponse:
    """Exchange a Google ID token for a session, creating the account on first sight.

    The token comes from the Google Sign-In SDK on the device. It is verified here —
    signature, audience, expiry, issuer — and never stored: the server keeps only the
    provider's stable subject id, which is what the next sign-in is matched on.
    """
    return await _sign_in(IdentityProvider.GOOGLE, payload, request, service)


@router.post(
    "/auth/apple",
    response_model=AuthResponse,
    summary="Sign in with an Apple ID token",
)
async def sign_in_with_apple(
    payload: SocialLoginRequest,
    request: Request,
    service: AuthServiceDep,
    _limit: AuthRateLimitDep,
) -> AuthResponse:
    """Exchange an Apple ID token for a session, creating the account on first sight.

    Apple shares the learner's name only on the first authorization and delivers it to
    the client rather than inside the token, so ``display_name`` may be supplied here —
    it is used exactly once, when the account is created.
    """
    return await _sign_in(IdentityProvider.APPLE, payload, request, service)


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


# ======================================================== development sign-in
class _DevVerifier:
    """A verifier that vouches for the one built-in development identity.

    Same trick as the seeder's: sign in through the real use case so account creation,
    the identity row and the refresh chain all travel the production code path, with
    no provider round-trip. The router that exposes it is mounted only when
    ``DEV_LOGIN_ENABLED`` is set (on by default in every environment), so clearing the
    flag keeps the route out of the app entirely.
    """

    provider = IdentityProvider.GOOGLE

    def __init__(self, identity: VerifiedIdentity) -> None:
        self._identity = identity

    async def verify(self, id_token: str) -> VerifiedIdentity:
        return self._identity


#: The fixed provider subject behind the development account. Deterministic so the
#: second sign-in finds the identity the first one created instead of a new account.
_DEV_SUBJECT = "dev:admin"
_DEV_EMAIL = "admin@natiqo.dev"

dev_router = APIRouter(tags=["auth"])


@dev_router.post(
    "/auth/dev-login",
    response_model=AuthResponse,
    summary="Sign in with the built-in development account (admin/admin)",
)
async def dev_login(
    payload: DevLoginRequest,
    request: Request,
    uow: UowDep,
    settings: SettingsDep,
    _limit: AuthRateLimitDep,
) -> AuthResponse:
    """The one username/password sign-in this service has, for development and QA.

    Every other sign-in delegates the credential check to a provider; this one cannot,
    so it is guarded by a flag instead of a provider. The flag defaults to on in every
    environment, production included, and the route is not mounted at all when the flag
    is false — the guard is the server's, not the caller's.

    Sessions issued here live ``DEV_LOGIN_TOKEN_TTL_SECONDS`` (10 years by default)
    so a test run is never cut short by a 15-minute expiry. Every other sign-in keeps
    the normal token TTLs.
    """
    # Belt and braces: the router is mounted only when the flag was set at app
    # creation, so this branch exists for a process whose settings changed afterwards
    # — it makes the route behave as absent in every state, not just the usual one.
    if not settings.dev_login_enabled:
        raise NotFoundError("Not found.")

    # Both comparisons run even when the first fails: a short-circuit here would leak,
    # through timing, whether the username exists before the password is checked.
    username_ok = secrets.compare_digest(
        payload.username.encode("utf-8"), settings.dev_admin_user.encode("utf-8")
    )
    password_ok = secrets.compare_digest(
        payload.password.encode("utf-8"),
        settings.dev_admin_password.get_secret_value().encode("utf-8"),
    )
    if not (username_ok and password_ok):
        raise UnauthorizedError(
            "Invalid username or password.", code=ErrorCode.AUTH_INVALID_CREDENTIALS
        )

    dev_settings = settings.model_copy(
        update={
            "access_token_ttl_seconds": settings.dev_login_token_ttl_seconds,
            "refresh_token_ttl_seconds": settings.dev_login_token_ttl_seconds,
        }
    )
    identity = VerifiedIdentity(
        provider=IdentityProvider.GOOGLE,
        subject=_DEV_SUBJECT,
        email=_DEV_EMAIL,
        email_verified=True,
        display_name="Dev Admin",
    )
    service = AuthService(
        uow, dev_settings, verifiers={IdentityProvider.GOOGLE: _DevVerifier(identity)}
    )
    command = SocialLoginCommand(
        provider=IdentityProvider.GOOGLE,
        id_token=_DEV_SUBJECT,
        device=_device(payload.device, request),
        display_name="Dev Admin",
    )
    result = await service.login_with_identity(command)

    required = {UserRole.ADMIN}
    if not required <= {UserRole(role) for role in result.user.roles}:
        # First sight of this account: it was created with the default role, so the
        # tokens minted a line ago carry no admin claims. Grant the roles, revoke that
        # throwaway session, and sign in once more — the pair handed to the client is
        # then minted from the account its own responses describe. Later sign-ins take
        # this branch never and cost one query fewer.
        user = await uow.users.get(result.user.id)
        if user is None:  # pragma: no cover - sign-in above just loaded it
            raise UnauthorizedError(
                "This sign-in is no longer linked to an account.",
                code=ErrorCode.AUTH_IDENTITY_INVALID,
            )
        user.roles = (UserRole.ADMIN,)
        await uow.users.update(user)
        await uow.commit()
        await service.logout(LogoutCommand(refresh_token=result.tokens.refresh_token))
        result = await service.login_with_identity(command)

    return _auth_response(result)


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
    nothing useful to tell them and no reason to make a retried request fail. Every live
    refresh chain is revoked in the same transaction as the status change, so the account
    stops being renewable immediately rather than at token expiry.
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


__all__ = [
    "AuthServiceDep",
    "CurrentUser",
    "current_user",
    "dev_router",
    "require_role",
    "router",
]
