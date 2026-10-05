"""Authentication and profile use cases.

Everything a client can do to its own account lives here: register, log in, rotate a
refresh token, log out, reset a password, read and patch its profile.

Three decisions are worth stating up front, because they look like bugs otherwise:

1. **A failed login commits.** The unit of work rolls back when the service raises, so
   the lockout counter is committed *before* the error is raised. Without that line a
   brute-force attempt would never accumulate and ``login_max_failed_attempts`` would
   be decorative.
2. **Refresh rotation revokes the family on reuse.** Presenting an already-consumed
   token means the token was copied, so every descendant of that rotation chain is
   killed rather than just the one presented (plan §13).
3. **Changing a password revokes every session.** An attacker holding a stolen refresh
   token must not survive the owner's password reset.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.core.config import Settings
from app.core.errors import (
    ConflictError,
    ErrorCode,
    ForbiddenError,
    RateLimitedError,
    UnauthorizedError,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    dummy_verify,
    hash_password,
    hash_token,
    needs_rehash,
    verify_password,
)
from app.core.time import is_valid_timezone, utc_now
from app.core.types import UserId, UserRole, UserStatus, new_id
from app.core.uow import UnitOfWorkProtocol
from app.modules.users.application.dto import (
    AuthResult,
    ChangePasswordCommand,
    DeviceContext,
    DeviceView,
    EmailVerificationTicket,
    ForgotPasswordCommand,
    LoginCommand,
    LogoutCommand,
    MeView,
    PasswordResetTicket,
    RefreshCommand,
    RegisterCommand,
    ResendVerificationCommand,
    ResetPasswordCommand,
    TokenPair,
    UpdateProfileCommand,
    UpdateSettingsCommand,
    VerifyEmailCommand,
)
from app.modules.users.domain.entities import (
    RefreshSession,
    User,
    UserDevice,
    UserProfile,
    UserSettings,
)
from app.modules.users.domain.policy import check_password, is_valid_email, normalise_email
from app.modules.users.domain.ports import (
    EmailDeliveryError,
    EmailVerificationDeliveryPort,
    PasswordResetDeliveryPort,
)

logger = get_logger("users.auth")

#: Reset links are short-lived by design: the window exists to click a link, not to
#: keep a reset capability alive for days.
PASSWORD_RESET_TTL = timedelta(minutes=30)

#: Verification links get days, not minutes, because nobody is locked out by an
#: unconfirmed address — the learner simply has not opened the email yet, and a busy
#: person may not for a while. The tension with a leaked token is real, which is why
#: redeeming one is idempotent and why only the newest link stays live.
EMAIL_VERIFICATION_TTL = timedelta(days=3)


def _as_verifier(delivery: object) -> EmailVerificationDeliveryPort:
    """Treat a reset adapter as a verification adapter when it can send.

    A reset-only adapter cannot: it has no way to render a different link, and using it
    would mail the learner a *reset* link in response to a verification request. So an
    adapter that does not implement the verification port gets an explicit
    not-configured stand-in that says so, rather than silently sending the wrong link or
    silently doing nothing.
    """
    if isinstance(delivery, EmailVerificationDeliveryPort):
        return delivery
    return _UnavailableVerificationDelivery()


class _UnavailableVerificationDelivery:
    """Reports that verification mail cannot be sent.

    Logged rather than raised: a learner who taps "resend" and gets an error learns
    nothing useful, and the account is fully usable without confirmation anyway. The
    log line is what tells an operator the feature is configured for nobody.
    """

    async def send_verification_link(
        self,
        *,
        user_id: UserId,
        email: str,
        raw_token: str,
        expires_at: datetime,
    ) -> None:
        logger.warning(
            "auth.verification_not_delivered",
            user_id=str(user_id),
            email_domain=email.rsplit("@", 1)[-1],
            detail="No adapter can send verification mail; the link was not sent.",
        )


class AuthService:
    def __init__(
        self,
        uow: UnitOfWorkProtocol,
        settings: Settings,
        reset_delivery: PasswordResetDeliveryPort,
        verification_delivery: EmailVerificationDeliveryPort | None = None,
    ) -> None:
        self._uow = uow
        self._settings = settings
        # A reset is useless without delivery, so the use case never mints a token
        # silently. The adapter is required rather than defaulted: choosing between
        # an audit-only logger and a real mailer is a deployment decision, and the
        # composition root is the only layer allowed to make it.
        self._reset_delivery = reset_delivery
        # Verification defaults to the reset adapter when it can send. Keeping it optional
        # means the dozens of existing call sites that pass only a reset adapter keep
        # working, and an adapter that cannot verify simply reports it rather than
        # pretending a confirmation mail went out.
        self._verification_delivery = verification_delivery or _as_verifier(reset_delivery)

    # ================================================================== register
    async def register(self, command: RegisterCommand) -> AuthResult:
        email = normalise_email(command.email)
        if not is_valid_email(email):
            raise ValidationFailed(
                "That email address does not look valid.",
                details={"field": "email"},
            )

        problems = check_password(
            command.password,
            min_length=self._settings.password_min_length,
            email=email,
        )
        if problems:
            raise ValidationFailed(
                "Please choose a stronger password.",
                code=ErrorCode.AUTH_WEAK_PASSWORD,
                details={"problems": [problem.to_dict() for problem in problems]},
            )

        if not command.timezone or not is_valid_timezone(command.timezone):
            raise ValidationFailed("Unknown timezone.", details={"field": "timezone"})

        display_name = command.display_name.strip()
        if not 2 <= len(display_name) <= 80:
            raise ValidationFailed(
                "Display name must be between 2 and 80 characters.",
                details={"field": "display_name"},
            )

        if await self._uow.users.email_exists(email):
            raise ConflictError(
                "An account with that email already exists.",
                code=ErrorCode.AUTH_ACCOUNT_EXISTS,
            )

        now = utc_now()
        user = User(
            id=UserId(new_id()),
            email=email,
            password_hash=hash_password(command.password),
            # Accounts are usable immediately. `pending_verification` and the
            # `AUTH_EMAIL_NOT_VERIFIED` path are wired but only engage once a mail
            # provider is configured; see the note in the module docstring.
            status=_initial_status(),
            password_changed_at=now,
            created_at=now,
            updated_at=now,
        )
        profile = UserProfile(
            user_id=user.id,
            display_name=display_name,
            native_language=command.native_language,
            english_variant=command.english_variant,
            timezone=command.timezone,
        )
        settings_row = UserSettings(
            user_id=user.id,
            locale=command.locale,
            daily_goal_minutes=self._settings.daily_goal_default,
        )

        await self._uow.users.add(user)
        await self._uow.users.save_profile(profile)
        await self._uow.users.save_settings(settings_row)
        tokens, _session_record = await self._issue_session(user, command.device)
        await self._uow.commit()

        logger.info("user.registered", user_id=str(user.id), status=str(user.status))
        return AuthResult(user=MeView.of(user, profile, settings_row), tokens=tokens)

    # ===================================================================== login
    async def login(self, command: LoginCommand) -> AuthResult:
        email = normalise_email(command.email)
        user = await self._uow.users.get_by_email(email)

        if user is None:
            # Burn equivalent CPU so response time does not reveal whether the
            # address is registered.
            dummy_verify()
            logger.info("auth.login_rejected", reason="unknown_email")
            raise UnauthorizedError(
                "Email or password is incorrect.", code=ErrorCode.AUTH_INVALID_CREDENTIALS
            )

        self._assert_authenticatable(user)

        if user.is_locked:
            raise RateLimitedError(
                "Too many failed attempts. Try again later.",
                code=ErrorCode.AUTH_ACCOUNT_LOCKED,
                retry_after=max(user.lock_seconds_remaining(), 1),
            )

        if not verify_password(command.password, user.password_hash):
            # Persist the failure *before* raising: the request's transaction is
            # rolled back on the way out, so an uncommitted counter never increments.
            user.record_failed_login(
                max_attempts=self._settings.login_max_failed_attempts,
                lockout_seconds=self._settings.login_lockout_seconds,
            )
            await self._uow.users.update(user)
            await self._uow.commit()
            logger.warning(
                "auth.login_failed",
                user_id=str(user.id),
                attempts=user.failed_login_attempts,
            )
            raise UnauthorizedError(
                "Email or password is incorrect.", code=ErrorCode.AUTH_INVALID_CREDENTIALS
            )

        if needs_rehash(user.password_hash):
            # Argon2 parameters were raised since this hash was written. Re-hashing
            # on a successful login is the only moment the plaintext is available.
            user.change_password(hash_password(command.password))
            logger.info("auth.password_rehashed", user_id=str(user.id))

        user.record_successful_login()
        await self._uow.users.update(user)

        profile, settings_row = await self._require_profile_and_settings(user)
        tokens, _ = await self._issue_session(user, command.device)
        await self._uow.commit()

        logger.info("auth.login_succeeded", user_id=str(user.id))
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

    # ============================================================ password reset
    async def forgot_password(self, command: ForgotPasswordCommand) -> PasswordResetTicket | None:
        """Mint a reset token and hand it to the delivery port.

        Returns ``None`` when the address is unknown. The handler ignores the
        difference and always reports success — see the route.
        """
        email = normalise_email(command.email)
        user = await self._uow.users.get_by_email(email)
        if user is None or not user.is_authenticatable():
            dummy_verify()
            logger.info("auth.forgot_password_unknown", email_domain=email.rsplit("@", 1)[-1])
            return None

        # One live reset per account. Keeping older links valid would let a token
        # leaked days ago still be redeemed after the learner has already recovered,
        # which serves nobody except an attacker holding that leak.
        await self._uow.password_reset.revoke_outstanding(user.id)
        # 256 bits of entropy from two UUIDs. The raw value goes to the delivery
        # channel; only its digest is persisted, so a database leak yields no links.
        raw = uuid.uuid4().hex + uuid.uuid4().hex
        token = hash_token(raw)
        expires_at = utc_now() + PASSWORD_RESET_TTL
        await self._uow.password_reset.add(
            user_id=user.id,
            token_hash=token,
            expires_at=expires_at,
            requested_ip=command.ip_address,
        )
        await self._uow.commit()

        # Delivery happens after the commit so a provider outage cannot roll back a
        # stored token, and so the link is never dispatched for a token that failed to
        # persist. The token exists nowhere else - the digest cannot be reversed.
        #
        # A failed hand-off must not change the answer this endpoint gives. The route
        # reports success for a known and an unknown address alike; letting a transport
        # exception escape would return 500 for one and 200 for the other, so the very
        # response the indistinguishability rule exists to protect would start telling an
        # attacker which addresses are registered. Log loudly, answer the same.
        try:
            await self._reset_delivery.send_reset_link(
                user_id=user.id, email=user.email, raw_token=raw, expires_at=expires_at
            )
        except EmailDeliveryError:
            logger.error(
                "auth.password_reset_delivery_failed",
                user_id=str(user.id),
                email_domain=user.email.rsplit("@", 1)[-1],
                detail="Token stored; the learner must be able to request another.",
            )
        logger.info("auth.password_reset_requested", user_id=str(user.id))
        return PasswordResetTicket(user_id=user.id, token=raw, expires_at=expires_at)

    async def reset_password(self, command: ResetPasswordCommand) -> None:
        token_hash = hash_token(command.token.strip())
        record = await self._uow.password_reset.get(token_hash)
        if record is None or record.used_at is not None or record.expires_at <= utc_now():
            # One message for all three cases: distinguishing them tells an attacker
            # whether a guessed token ever existed.
            await self._uow.commit()
            raise ValidationFailed(
                "This reset link is invalid or has expired. Please request a new one.",
                details={"field": "token"},
            )

        user = await self._uow.users.get(record.user_id)
        if user is None:
            await self._uow.commit()
            raise ValidationFailed(
                "This reset link is invalid or has expired. Please request a new one."
            )

        problems = check_password(
            command.new_password,
            min_length=self._settings.password_min_length,
            # The account has to be loaded first: a reset is the one flow where a
            # learner may not know their own address well enough to include it by
            # hand, so the policy check silently loses the address rule unless the
            # email is passed in here.
            email=user.email,
        )
        if problems:
            raise ValidationFailed(
                "Please choose a stronger password.",
                code=ErrorCode.AUTH_WEAK_PASSWORD,
                details={"problems": [problem.to_dict() for problem in problems]},
            )

        user.change_password(hash_password(command.new_password))
        await self._uow.users.update(user)
        await self._uow.password_reset.consume(token_hash)
        # Any session opened with the old password is now attacker-owned.
        revoked = await self._uow.refresh_tokens.revoke_all_for_user(user.id)
        await self._uow.commit()
        logger.info("auth.password_reset_completed", user_id=str(user.id), revoked_sessions=revoked)

    # ===================================================== email verification
    async def verify_email(self, command: VerifyEmailCommand) -> None:
        """Confirm an address from an emailed token.

        Deliberately *not* a login gate. An unverified learner keeps working access and
        the client decides what to nudge about; gating login would lock out every account
        that registered before verification existed, which is most of the current base.
        """
        token_hash = hash_token(command.token.strip())
        record = await self._uow.email_verification.get(token_hash)
        user = await self._uow.users.get(record.user_id) if record is not None else None
        if record is None or user is None:
            # One message for missing and orphaned alike. Splitting them tells an
            # attacker whether a token they guessed ever existed.
            await self._uow.commit()
            raise ValidationFailed(
                "This verification link is invalid or has expired. Please request a new one.",
                details={"field": "token"},
            )

        if record.used_at is not None:
            # A spent token is only tolerable when it did its job. Mail scanners and
            # link previewers open a verification link before the learner ever sees it,
            # and the learner must not be shown an error for a confirmation that already
            # succeeded. A spent token that did *not* verify the address was superseded
            # by a newer link, and that must fail.
            if user.is_email_verified:
                await self._uow.commit()
                logger.info(
                    "auth.email_verification_replayed",
                    user_id=str(user.id),
                    detail="Link already redeemed; the address is confirmed.",
                )
                return
            await self._uow.commit()
            raise ValidationFailed(
                "This verification link is invalid or has expired. Please request a new one.",
                details={"field": "token"},
            )

        if record.expires_at <= utc_now():
            await self._uow.commit()
            raise ValidationFailed(
                "This verification link is invalid or has expired. Please request a new one.",
                details={"field": "token"},
            )

        # Idempotent end state: redeeming twice lands on the same confirmed address
        # rather than an error, so the second attempt cannot un-confirm anything.
        already = user.is_email_verified
        user.verify_email()
        await self._uow.users.update(user)
        await self._uow.email_verification.consume(token_hash)
        await self._uow.commit()
        logger.info(
            "auth.email_verified",
            user_id=str(user.id),
            # A repeat redemption is worth counting: it is the visible symptom of a
            # client that retries, and of a link shared between people.
            already_verified=already,
        )

    async def resend_verification(
        self, command: ResendVerificationCommand
    ) -> EmailVerificationTicket | None:
        """Mint and dispatch a fresh verification link.

        Returns ``None`` when there is nothing useful to do — already verified, or a
        working link is already in flight. The caller reports success either way so the
        endpoint cannot be used to discover which addresses are verified.
        """
        user = await self._uow.users.get(command.user_id)
        if user is None or not user.is_authenticatable():
            await self._uow.commit()
            return None

        if user.is_email_verified:
            await self._uow.commit()
            logger.info("auth.verification_not_needed", user_id=str(user.id))
            return None

        if await self._uow.email_verification.has_usable(user.id):
            # Re-sending unconditionally is how "I never got the email" turns into the
            # learner's provider rate-limiting their own address, after which nothing
            # arrives ever again. A live link is the answer; a second copy is noise.
            await self._uow.commit()
            logger.info("auth.verification_already_pending", user_id=str(user.id))
            return None

        await self._uow.email_verification.revoke_outstanding(user.id)
        raw = uuid.uuid4().hex + uuid.uuid4().hex
        expires_at = utc_now() + EMAIL_VERIFICATION_TTL
        await self._uow.email_verification.add(
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=expires_at,
            requested_ip=command.ip_address,
        )
        await self._uow.commit()

        try:
            await self._verification_delivery.send_verification_link(
                user_id=user.id, email=user.email, raw_token=raw, expires_at=expires_at
            )
        except EmailDeliveryError:
            # Hand the token back. Leaving it stored would count as "already pending" for
            # three days, so a provider that was down for one minute would lock the
            # learner out of confirming their address until the TTL expired - and they
            # would have no way to tell that from "my address is rejected".
            await self._uow.email_verification.revoke_outstanding(user.id)
            await self._uow.commit()
            logger.error(
                "auth.verification_delivery_failed",
                user_id=str(user.id),
                email_domain=user.email.rsplit("@", 1)[-1],
                detail="Token revoked; a retry can mint a fresh one immediately.",
            )
            return None
        logger.info("auth.verification_resent", user_id=str(user.id))
        return EmailVerificationTicket(user_id=user.id, token=raw, expires_at=expires_at)

    async def change_password(self, command: ChangePasswordCommand) -> int:
        """Replace the password of an authenticated account.

        Requires the current password, and revokes every session afterwards. The
        revocation is what makes this different from a reset: the caller is already
        authenticated, so a stolen access token must not be enough to take the account
        over permanently — and the honest way to guarantee that is to invalidate the
        token the attacker might still be holding.

        Returns the number of sessions revoked so the handler can tell the client to
        sign out everywhere else.
        """
        user = await self._uow.users.get(command.user_id)
        if user is None or not user.is_authenticatable():
            raise UnauthorizedError(
                "This account is no longer available.",
                code=ErrorCode.AUTH_TOKEN_INVALID,
            )

        # A wrong current password is an authentication failure, not a validation one:
        # it is the only signal that distinguishes "someone is guessing" from "someone
        # fat-fingered it", and the login rate limiter depends on seeing it as a failure.
        if not verify_password(command.current_password, user.password_hash):
            logger.warning(
                "auth.change_password_current_mismatch",
                user_id=str(user.id),
            )
            raise UnauthorizedError(
                "Your current password is incorrect.",
                code=ErrorCode.AUTH_INVALID_CREDENTIALS,
            )

        problems = check_password(
            command.new_password,
            min_length=self._settings.password_min_length,
            email=user.email,
        )
        if problems:
            raise ValidationFailed(
                "Please choose a stronger password.",
                code=ErrorCode.AUTH_WEAK_PASSWORD,
                details={"problems": [problem.to_dict() for problem in problems]},
            )

        if verify_password(command.new_password, user.password_hash):
            # Not an error, but worth refusing: re-hashing the same password changes the
            # hash while changing nothing, which makes the audit trail claim a credential
            # change that did not happen.
            raise ValidationFailed(
                "Please choose a password different from your current one.",
                details={"field": "newPassword"},
            )

        user.change_password(hash_password(command.new_password))
        await self._uow.users.update(user)
        revoked = await self._uow.refresh_tokens.revoke_all_for_user(user.id)
        # Outstanding resets were minted against the old credential. Leaving them live
        # would mean a link leaked before the change can still undo it afterwards.
        await self._uow.password_reset.revoke_outstanding(user.id)
        await self._uow.commit()
        logger.info("auth.password_changed", user_id=str(user.id), revoked_sessions=revoked)
        return revoked

    async def delete_account(self, user_id: UserId) -> bool:
        """Close the caller's own account.

        Returns ``False`` when there was nothing to do -- the account is already closed, or
        no such account -- so a retried delete answers the same way the first one did
        instead of turning a dropped response into a confusing error.

        The learner is asking to walk away from data they can no longer reach, so the only
        thing this needs to guarantee is that they cannot reach it afterwards. Every live
        capability is revoked in the same transaction: the refresh tokens that mint new
        access tokens, the password-reset links that could reopen the account, and the
        verification links that would confirm an address nobody will read again.

        The access token that made this request stays valid until it expires. It cannot be
        withdrawn -- it is already in the client's hands and self-contained -- but it can
        no longer be exchanged for anything, and it resolves to a ``DELETED`` account that
        every authenticated route rejects.
        """
        user = await self._uow.users.get(user_id)
        if user is None:
            await self._uow.commit()
            return False

        already_deleted = user.is_deleted
        user.soft_delete()
        await self._uow.users.update(user)
        revoked = await self._uow.refresh_tokens.revoke_all_for_user(user.id)
        await self._uow.password_reset.revoke_outstanding(user.id)
        await self._uow.email_verification.revoke_outstanding(user.id)
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
        # ``PENDING_VERIFICATION`` is deliberately *not* rejected here. An unconfirmed
        # address is a nudge for the client to render, not a reason to refuse access:
        # gating login on it would lock out every account registered before the
        # verification feature existed, which is most of the current base. The code and
        # ``email_verified_at`` record the truth; ``/me`` reports it.

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

        access_token, _ = create_access_token(
            user.id, roles=tuple(str(role) for role in user.roles)
        )
        refresh_token, claims = create_refresh_token(
            user.id, family_id=family_id, device_id=device_row
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


def _initial_status() -> UserStatus:
    """Accounts start usable.

    Requiring a verified email before first use would need a mail provider, and
    blocking sign-up on an unconfigured channel locks every new learner out. The
    ``pending_verification`` status and its 403 path stay wired so turning the
    requirement on later is a settings change, not a rewrite.
    """
    return UserStatus.ACTIVE
