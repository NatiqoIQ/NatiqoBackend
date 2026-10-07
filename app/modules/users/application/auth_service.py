"""Authentication and profile use cases.

Everything a client can do to its own account lives here: sign in with Google or Apple,
rotate a refresh token, log out, and read and patch its profile.

Three decisions are worth stating up front, because they look like bugs otherwise:

1. **Sign-in is delegated.** There is no password anywhere in this module. The learner
   proves who they are with an ID token from Google or Apple, which is verified by an
   :class:`~app.modules.users.domain.identity.IdentityVerifierPort` before anything is
   read or written. The application never sees a credential it could store, reset or
   leak.
2. **Identity is keyed on ``(provider, subject)``.** The subject is the provider's
   stable, opaque user id. An email is only ever used to *link* a newly seen provider
   account to an existing one, and only when the provider asserts the address is
   verified — otherwise a provider account carrying someone else's unverified address
   could be used to take that account over.
3. **Refresh rotation revokes the family on reuse.** Presenting an already-consumed
   token means the token was copied, so every descendant of that rotation chain is
   killed rather than just the one presented (plan §13).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from app.core.config import Settings
from app.core.errors import (
    ErrorCode,
    ForbiddenError,
    UnauthorizedError,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_token,
)
from app.core.time import is_valid_timezone, utc_now
from app.core.types import UserId, UserRole, UserStatus, new_id
from app.core.uow import UnitOfWorkProtocol
from app.modules.users.application.dto import (
    AuthResult,
    DeviceContext,
    DeviceView,
    LogoutCommand,
    MeView,
    RefreshCommand,
    SocialLoginCommand,
    TokenPair,
    UpdateProfileCommand,
    UpdateSettingsCommand,
)
from app.modules.users.domain.entities import (
    ExternalIdentity,
    RefreshSession,
    User,
    UserDevice,
    UserProfile,
    UserSettings,
)
from app.modules.users.domain.identity import (
    IdentityProvider,
    IdentityVerificationError,
    IdentityVerifierPort,
    VerifiedIdentity,
)
from app.modules.users.domain.policy import is_valid_email, normalise_email

logger = get_logger("users.auth")


class AuthService:
    def __init__(
        self,
        uow: UnitOfWorkProtocol,
        settings: Settings,
        verifiers: Mapping[IdentityProvider, IdentityVerifierPort],
    ) -> None:
        self._uow = uow
        self._settings = settings
        # A mapping rather than one verifier: the two providers verify different issuers
        # against different key sets, and the route decides which one to use before the
        # service ever sees the token. A provider absent from the map is simply not
        # configured, which the use case reports rather than treating as a bad token.
        self._verifiers = verifiers

    # ================================================================= sign in
    async def login_with_identity(self, command: SocialLoginCommand) -> AuthResult:
        """Sign in, creating the account the first time a provider identity is seen."""
        verifier = self._verifiers.get(command.provider)
        if verifier is None:
            raise UnauthorizedError(
                f"{command.provider.value.title()} sign-in is not enabled.",
                code=ErrorCode.AUTH_PROVIDER_UNAVAILABLE,
            )

        try:
            identity = await verifier.verify(command.id_token)
        except IdentityVerificationError as exc:
            # The reason is logged and never returned: telling a caller *why* a token
            # failed is a hint about how to forge one.
            logger.warning(
                "auth.identity_rejected",
                provider=str(command.provider),
                reason=exc.reason,
            )
            raise UnauthorizedError(
                "That sign-in could not be verified. Please try again.",
                code=ErrorCode.AUTH_IDENTITY_INVALID,
            ) from exc

        stored = await self._uow.identities.find(str(identity.provider), identity.subject)
        if stored is not None:
            return await self._sign_in_known(stored, identity, command)

        return await self._sign_in_new(identity, command)

    async def _sign_in_known(
        self,
        stored: ExternalIdentity,
        identity: VerifiedIdentity,
        command: SocialLoginCommand,
    ) -> AuthResult:
        """A provider identity this service has already linked to an account."""
        user = await self._uow.users.get(stored.user_id)
        if user is None:
            # The identity row outlived its account. Not a client error the learner can
            # act on, but it must not be papered over by creating a second account.
            logger.error(
                "auth.identity_orphaned",
                identity_id=str(stored.id),
                user_id=str(stored.user_id),
            )
            raise UnauthorizedError(
                "This sign-in is no longer linked to an account.",
                code=ErrorCode.AUTH_IDENTITY_INVALID,
            )
        self._assert_authenticatable(user)
        await self._uow.identities.touch_login(str(stored.provider), stored.subject)
        if identity.email_verified and not user.is_email_verified:
            # An account first seen through an unverified assertion is corrected the
            # moment the provider vouches for the mailbox, so the flag tracks the
            # provider's current answer instead of the one given at creation.
            user.verify_email(utc_now())
            await self._uow.users.update(user)
        return await self._complete_sign_in(user, command.device)

    async def _sign_in_new(
        self, identity: VerifiedIdentity, command: SocialLoginCommand
    ) -> AuthResult:
        """First time this provider identity has been seen."""
        email = self._provider_email(identity)
        now = utc_now()
        created = False

        user: User | None = None
        if email is not None:
            user = await self._uow.users.get_by_email(email)
            if user is not None and not identity.email_verified:
                # Linking an unverified provider address to an existing account would let
                # whoever controls that provider account seize the learner's data. Refuse
                # and let them use the provider they originally registered with.
                logger.warning(
                    "auth.identity_email_unverified_link_refused",
                    provider=str(identity.provider),
                    user_id=str(user.id),
                )
                raise UnauthorizedError(
                    "That email is already registered. Please sign in the way you did before.",
                    code=ErrorCode.AUTH_IDENTITY_CONFLICT,
                )

        # The create branch keeps the rows it just built; the link branch reads them.
        # They must not be mixed: with ``autoflush`` off, a row this unit of work has
        # pending is invisible to a SELECT, so asking the repository for a profile that
        # was written a line earlier would insert a second one and fail the unique
        # constraint on ``user_profiles.user_id``.
        profile: UserProfile
        settings_row: UserSettings
        if user is None:
            if email is None:
                raise UnauthorizedError(
                    "The provider did not share an email address for this account.",
                    code=ErrorCode.AUTH_IDENTITY_INCOMPLETE,
                )
            user = self._build_user(email, identity, now)
            profile = self._build_profile(user, command, identity)
            settings_row = UserSettings(
                user_id=user.id,
                locale=command.locale,
                daily_goal_minutes=self._settings.daily_goal_default,
            )
            await self._uow.users.add(user)
            await self._uow.users.save_profile(profile)
            await self._uow.users.save_settings(settings_row)
            created = True
        else:
            self._assert_authenticatable(user)
            profile, settings_row = await self._require_profile_and_settings(user)

        if identity.email_verified and not user.is_email_verified:
            # The provider is the authority on the mailbox, so a verified assertion is
            # what flips the flag.
            user.verify_email(now)
            await self._uow.users.update(user)

        await self._uow.identities.add(
            ExternalIdentity(
                id=new_id(),
                user_id=user.id,
                provider=str(identity.provider),
                subject=identity.subject,
                email=email,
                email_verified=identity.email_verified,
                display_name=identity.display_name,
                picture_url=identity.picture_url,
                created_at=now,
                last_login_at=now,
            )
        )

        tokens, _ = await self._issue_session(user, command.device)
        await self._uow.commit()

        logger.info(
            "auth.signed_in",
            user_id=str(user.id),
            provider=str(identity.provider),
            created=created,
        )
        return AuthResult(user=MeView.of(user, profile, settings_row), tokens=tokens)

    async def _complete_sign_in(self, user: User, device: DeviceContext) -> AuthResult:
        profile, settings_row = await self._require_profile_and_settings(user)
        tokens, _ = await self._issue_session(user, device)
        await self._uow.commit()
        logger.info("auth.signed_in", user_id=str(user.id))
        return AuthResult(user=MeView.of(user, profile, settings_row), tokens=tokens)

    # =================================================================== refresh
    async def refresh(self, command: RefreshCommand) -> AuthResult:
        claims = decode_token(command.refresh_token, "refresh")
        jti_hash = hash_token(claims.jti)
        stored = await self._uow.refresh_tokens.get_by_jti_hash(jti_hash)

        if stored is None:
            raise UnauthorizedError(
                "This session is no longer valid. Please sign in again.",
                code=ErrorCode.AUTH_REFRESH_INVALID,
            )

        if stored.is_revoked:
            # The token worked once and has been rotated. Presenting it again means
            # the token was replayed, so the whole chain is compromised.
            revoked = await self._uow.refresh_tokens.revoke_family(stored.family_id)
            await self._uow.commit()
            logger.error(
                "auth.refresh_reuse_detected",
                family_id=stored.family_id,
                user_id=str(stored.user_id),
                revoked=revoked,
            )
            raise UnauthorizedError(
                "For your security this session has been ended. Please sign in again.",
                code=ErrorCode.AUTH_REFRESH_REUSED,
            )

        if stored.is_expired():
            await self._uow.commit()
            raise UnauthorizedError(
                "Your session has expired. Please sign in again.",
                code=ErrorCode.AUTH_REFRESH_INVALID,
            )

        user = await self._uow.users.get(stored.user_id)
        if user is None:
            await self._uow.commit()
            raise UnauthorizedError(
                "This account no longer exists.", code=ErrorCode.AUTH_REFRESH_INVALID
            )
        self._assert_authenticatable(user)

        device = command.device
        if device.platform == "web" and stored.device_id is not None:
            device = DeviceContext(
                platform=device.platform,
                push_token=device.push_token,
                app_version=device.app_version,
                user_agent=device.user_agent or stored.user_agent,
                ip_address=device.ip_address or stored.ip_address,
            )

        tokens, replacement = await self._issue_session(
            user, device, family_id=stored.family_id, device_id=stored.device_id
        )
        await self._uow.refresh_tokens.mark_replaced(stored, replaced_by_hash=replacement.jti_hash)
        # Before the commit, not after: this is the only path that can be the first to
        # touch an account that predates these tables, and any defaults it creates are
        # part of the same unit of work as the rotation.
        profile, settings_row = await self._require_profile_and_settings(user)
        await self._uow.commit()
        logger.info("auth.refresh_succeeded", user_id=str(user.id))
        return AuthResult(user=MeView.of(user, profile, settings_row), tokens=tokens)

    # ==================================================================== logout
    async def logout(self, command: LogoutCommand) -> None:
        try:
            claims = decode_token(command.refresh_token, "refresh")
        except UnauthorizedError:
            # An unparseable or expired token is already unusable. Reporting success
            # keeps the endpoint idempotent for a client that retries after a network
            # drop instead of surfacing an error it cannot act on.
            logger.info("auth.logout_noop", reason="unparseable_token")
            return

        stored = await self._uow.refresh_tokens.get_by_jti_hash(hash_token(claims.jti))
        if stored is None:
            await self._uow.commit()
            return

        if command.all_devices:
            revoked = await self._uow.refresh_tokens.revoke_all_for_user(stored.user_id)
        else:
            revoked = await self._uow.refresh_tokens.revoke_family(stored.family_id)
        await self._uow.commit()
        logger.info("auth.logout", user_id=str(stored.user_id), revoked=revoked)

    async def delete_account(self, user_id: UserId) -> bool:
        """Close the caller's own account.

        Returns ``False`` when there was nothing to do -- the account is already closed, or
        no such account -- so a retried delete answers the same way the first one did
        instead of turning a dropped response into a confusing error.

        The learner is asking to walk away from data they can no longer reach, so the only
        thing this needs to guarantee is that they cannot reach it afterwards. The refresh
        tokens that mint new access tokens are revoked in the same transaction as the status
        change, so the account stops being renewable immediately rather than at token expiry.

        The access token that made this request stays valid until it expires. It cannot be
        withdrawn -- it is already in the client's hands and self-contained -- but it can no
        longer be exchanged for anything, and it resolves to a ``DELETED`` account that every
        authenticated route rejects.
        """
        user = await self._uow.users.get(user_id)
        if user is None:
            await self._uow.commit()
            return False

        already_deleted = user.is_deleted
        user.soft_delete()
        await self._uow.users.update(user)
        revoked = await self._uow.refresh_tokens.revoke_all_for_user(user.id)
        await self._uow.commit()

        logger.info(
            "auth.account_deleted",
            user_id=str(user.id),
            detail="already_deleted" if already_deleted else "closed",
            revoked_sessions=revoked,
        )
        return not already_deleted

    # ============================================================== authorisation
    async def authenticate(self, access_token: str) -> User:
        """Resolve a bearer token to a live account.

        Called on every authenticated request, so it stays to two indexed reads: the
        JWT signature (no network) and one primary-key lookup.
        """
        claims = decode_token(access_token, "access")
        user = await self._uow.users.get(UserId(claims.subject))
        if user is None:
            raise UnauthorizedError(
                "Authentication is required.", code=ErrorCode.AUTH_TOKEN_INVALID
            )
        self._assert_authenticatable(user)
        return user

    async def require_role(self, user: User, minimum: UserRole) -> User:
        if not user.has_role(minimum):
            raise ForbiddenError(
                "You do not have permission to perform this action.",
                code=ErrorCode.AUTH_INSUFFICIENT_ROLE,
                details={"required_role": str(minimum)},
            )
        return user

    # ======================================================================= me
    async def get_me(self, user_id: UserId) -> MeView:
        user = await self._uow.users.get(user_id)
        if user is None:
            raise UnauthorizedError("Authentication is required.")
        profile, settings_row = await self._require_profile_and_settings(user)
        return MeView.of(user, profile, settings_row)

    async def update_me(
        self,
        user_id: UserId,
        *,
        profile_changes: UpdateProfileCommand | None = None,
        settings_changes: UpdateSettingsCommand | None = None,
    ) -> MeView:
        user = await self._uow.users.get(user_id)
        if user is None:
            raise UnauthorizedError("Authentication is required.")

        if profile_changes is not None and profile_changes.timezone is not None:
            if not is_valid_timezone(profile_changes.timezone):
                raise ValidationFailed("Unknown timezone.", details={"field": "timezone"})

        profile, settings_row = await self._require_profile_and_settings(user)
        self._apply_profile(profile, profile_changes)
        self._apply_settings(settings_row, settings_changes)
        await self._uow.users.save_profile(profile)
        await self._uow.users.save_settings(settings_row)
        await self._uow.commit()
        return MeView.of(user, profile, settings_row)

    async def list_devices(self, user_id: UserId) -> list[DeviceView]:
        devices = await self._uow.user_devices.list_active(user_id)
        return [DeviceView.of(device) for device in devices]

    async def revoke_device(self, user_id: UserId, device_id: uuid.UUID) -> None:
        """Sign a device out and stop its push delivery.

        The sessions are revoked alongside the device row because that is what a
        learner means by "sign this phone out": revoking push alone leaves an active
        token on a device they believe is gone.
        """
        device = await self._uow.user_devices.get(device_id)
        if device is None or device.user_id != user_id:
            # Never reveal whether another user's device id exists.
            raise ValidationFailed("Unknown device.", details={"field": "device_id"})
        await self._uow.user_devices.revoke(device_id)
        revoked = await self._uow.refresh_tokens.revoke_for_device(user_id, device_id)
        await self._uow.commit()
        logger.info(
            "user.device_revoked",
            user_id=str(user_id),
            device_id=str(device_id),
            revoked_sessions=revoked,
        )

    # ================================================================= internals
    def _assert_authenticatable(self, user: User) -> None:
        if user.status is UserStatus.DELETED:
            raise UnauthorizedError("This account has been deleted.", code=ErrorCode.USER_DELETED)
        if user.status is UserStatus.SUSPENDED:
            raise ForbiddenError(
                "This account has been suspended. Contact support for help.",
                code=ErrorCode.AUTH_ACCOUNT_DISABLED,
            )

    @staticmethod
    def _provider_email(identity: VerifiedIdentity) -> str | None:
        """The address to key a new account on, or ``None`` when it is unusable.

        A malformed address is treated as absent rather than rejected: the learner did
        nothing wrong, and the provider remains the source of truth for their identity.
        """
        if not identity.email:
            return None
        email = normalise_email(identity.email)
        return email if is_valid_email(email) else None

    def _build_user(self, email: str, identity: VerifiedIdentity, now: datetime) -> User:
        user = User(
            id=UserId(new_id()),
            email=email,
            status=UserStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        if identity.email_verified:
            user.verify_email(now)
        return user

    def _build_profile(
        self, user: User, command: SocialLoginCommand, identity: VerifiedIdentity
    ) -> UserProfile:
        display_name = (command.display_name or identity.display_name or "").strip()
        if not 2 <= len(display_name) <= 80:
            # A provider often gives only one name, or none at all, so fall back to the
            # address rather than failing a sign-in over a cosmetic field.
            display_name = user.email.split("@")[0][:80]
        if not is_valid_timezone(command.timezone):
            raise ValidationFailed("Unknown timezone.", details={"field": "timezone"})
        return UserProfile(
            user_id=user.id,
            display_name=display_name,
            avatar_key=None,
            native_language=command.native_language,
            english_variant=command.english_variant,
            timezone=command.timezone,
        )

    async def _require_profile_and_settings(self, user: User) -> tuple[UserProfile, UserSettings]:
        """Load the profile/settings rows, creating defaults if a row is missing.

        Tolerating a missing row matters for accounts created by a seed script or a
        future admin action that predates these tables.
        """
        profile = await self._uow.users.get_profile(user.id)
        if profile is None:
            profile = UserProfile(user_id=user.id, display_name=user.email.split("@")[0])
            await self._uow.users.save_profile(profile)
        settings_row = await self._uow.users.get_settings(user.id)
        if settings_row is None:
            settings_row = UserSettings(
                user_id=user.id, daily_goal_minutes=self._settings.daily_goal_default
            )
            await self._uow.users.save_settings(settings_row)
        return profile, settings_row

    async def _issue_session(
        self,
        user: User,
        device: DeviceContext,
        *,
        family_id: str | None = None,
        device_id: uuid.UUID | None = None,
    ) -> tuple[TokenPair, RefreshSession]:
        device_row: uuid.UUID | None = device_id
        if device.push_token or device.app_version:
            row = UserDevice(
                id=new_id(),
                user_id=user.id,
                platform=device.platform,
                push_token=device.push_token,
                app_version=device.app_version,
                created_at=utc_now(),
                last_seen_at=utc_now(),
            )
            saved = await self._uow.user_devices.upsert_for_token(row)
            device_row = saved.id

        # The TTL comes from this service's own settings rather than the process-wide
        # defaults, so a service built with a modified Settings (the development
        # sign-in issues long-lived sessions) mints tokens that match what it reports
        # as ``expires_in`` below. For every normal sign-in the two are the same
        # values, so this changes nothing about production behaviour.
        access_token, _ = create_access_token(
            user.id,
            roles=tuple(str(role) for role in user.roles),
            ttl_seconds=self._settings.access_token_ttl_seconds,
        )
        refresh_token, claims = create_refresh_token(
            user.id,
            family_id=family_id,
            device_id=device_row,
            ttl_seconds=self._settings.refresh_token_ttl_seconds,
        )
        record = RefreshSession(
            id=new_id(),
            user_id=user.id,
            jti_hash=hash_token(claims.jti),
            family_id=claims.family_id or uuid.uuid4().hex,
            device_id=device_row,
            expires_at=claims.expires_at,
            created_at=utc_now(),
            user_agent=(device.user_agent or None),
            ip_address=device.ip_address,
        )
        await self._uow.refresh_tokens.add(record)
        tokens = TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self._settings.access_token_ttl_seconds,
            refresh_expires_in=self._settings.refresh_token_ttl_seconds,
        )
        return tokens, record

    @staticmethod
    def _apply_profile(profile: UserProfile, changes: UpdateProfileCommand | None) -> None:
        if changes is None:
            return
        if changes.display_name is not None:
            cleaned = changes.display_name.strip()
            if not 2 <= len(cleaned) <= 80:
                raise ValidationFailed(
                    "Display name must be between 2 and 80 characters.",
                    details={"field": "display_name"},
                )
            profile.display_name = cleaned
        if changes.avatar_key is not None:
            profile.avatar_key = changes.avatar_key or None
        if changes.native_language is not None:
            profile.native_language = changes.native_language
        if changes.english_variant is not None:
            profile.english_variant = changes.english_variant
        if changes.timezone is not None:
            profile.timezone = changes.timezone
        if changes.current_level is not None:
            # A learner cannot self-assign a level; only the placement test or a
            # moderator moves it. The API layer rejects this, and the domain refuses
            # to pretend otherwise.
            raise ForbiddenError(
                "Your level is set by the placement test.",
                code=ErrorCode.AUTH_FORBIDDEN,
                details={"field": "current_level"},
            )

    @staticmethod
    def _apply_settings(settings: UserSettings, changes: UpdateSettingsCommand | None) -> None:
        if changes is None:
            return
        if changes.locale is not None:
            settings.locale = changes.locale
        if changes.explanation_language is not None:
            settings.explanation_language = changes.explanation_language
        if changes.notification_preferences is not None:
            settings.notification_preferences = settings.notification_preferences.merged_with(
                changes.notification_preferences
            )
        if changes.daily_goal_minutes is not None:
            if not 5 <= changes.daily_goal_minutes <= 600:
                raise ValidationFailed(
                    "Daily goal must be between 5 and 600 minutes.",
                    details={"field": "daily_goal_minutes"},
                )
            settings.daily_goal_minutes = changes.daily_goal_minutes
        if changes.daily_goal_new_words is not None:
            if not 1 <= changes.daily_goal_new_words <= 200:
                raise ValidationFailed(
                    "Daily goal must be between 1 and 200 new words.",
                    details={"field": "daily_goal_new_words"},
                )
            settings.daily_goal_new_words = changes.daily_goal_new_words
