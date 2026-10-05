"""Users/auth service tests.

These run against a real SQLite schema with the real repositories. The point is to
exercise the parts that only break against a real database — unique constraints,
foreign keys, the rotation UPDATE, the two-rows-per-registration shape — which a
hand-written fake would have asserted away.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.errors import (
    ConflictError,
    ErrorCode,
    ForbiddenError,
    RateLimitedError,
    UnauthorizedError,
    ValidationFailed,
)
from app.core.registry import import_all_models
from app.core.security import create_refresh_token, decode_token, hash_token
from app.core.types import UserStatus
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.users.application.auth_service import AuthService
from app.modules.users.application.dto import (
    DeviceContext,
    ForgotPasswordCommand,
    LoginCommand,
    LogoutCommand,
    RefreshCommand,
    RegisterCommand,
    ResetPasswordCommand,
    UpdateProfileCommand,
    UpdateSettingsCommand,
)
from app.modules.users.infrastructure.delivery import InMemoryPasswordResetDelivery
from app.modules.users.infrastructure.models import (
    PasswordResetTokenModel,
    RefreshTokenModel,
    UserModel,
    UserProfileModel,
    UserSettingsModel,
)

pytestmark = pytest.mark.integration

GOOD_PASSWORD = "Baghdad-learns-2026"


@pytest.fixture
def delivery() -> InMemoryPasswordResetDelivery:
    return InMemoryPasswordResetDelivery()


@pytest_asyncio.fixture
async def uow():
    """An *open* unit of work spanning the whole test.

    The UoW owns its session, and the service calls ``commit`` on it, so it has to be
    entered for the life of the test rather than constructed and passed around. Leaving
    the block without a commit rolls back, which is exactly the behaviour the auth code
    relies on when it records a failed login before raising.
    """
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest.fixture
def service(uow, settings, delivery) -> AuthService:
    return AuthService(uow, settings, reset_delivery=delivery)


@pytest.fixture
def session(uow) -> AsyncSession:
    return uow.session


def registration(email: str = "ali@example.com", **overrides: Any) -> RegisterCommand:
    """A valid registration, with any field overridable.

    ``overrides`` wins over the defaults, so a test can pass ``password=...`` without
    also supplying a display name.
    """
    return RegisterCommand(
        **{
            "email": email,
            "password": GOOD_PASSWORD,
            "display_name": "Ali",
            **overrides,
        }
    )


# ====================================================================== register
class TestRegister:
    async def test_creates_account_profile_settings_and_session(self, service, session):
        result = await service.register(registration())

        assert result.user.email == "ali@example.com"
        assert result.user.status == str(UserStatus.ACTIVE)
        # Email is unverified but the account is usable; see `_initial_status`.
        assert result.user.email_verified is False
        assert result.tokens.access_token and result.tokens.refresh_token

        assert (await session.scalar(select(func.count()).select_from(UserModel))) == 1
        assert (await session.scalar(select(func.count()).select_from(UserProfileModel))) == 1
        assert (await session.scalar(select(func.count()).select_from(UserSettingsModel))) == 1
        assert (await session.scalar(select(func.count()).select_from(RefreshTokenModel))) == 1

    async def test_password_is_hashed_never_stored_plain(self, service, session):
        await service.register(registration())
        stored = (await session.execute(select(UserModel))).scalar_one()

        assert stored.password_hash != GOOD_PASSWORD
        assert stored.password_hash.startswith("$argon2")

    async def test_email_is_normalised_before_the_unique_check(self, service):
        await service.register(registration(email="Ali@Example.COM"))

        with pytest.raises(ConflictError) as caught:
            await service.register(registration(email="  ALI@example.com  "))
        assert caught.value.code == ErrorCode.AUTH_ACCOUNT_EXISTS

    async def test_rejects_weak_password(self, service):
        with pytest.raises(ValidationFailed) as caught:
            await service.register(registration(password="password"))

        assert caught.value.code == ErrorCode.AUTH_WEAK_PASSWORD
        # Every failing rule is reported, not just the first, so the client can render a
        # complete checklist.
        assert {p.get("code") for p in caught.value.details["problems"]} >= {
            "TOO_SHORT",
            "NO_UPPERCASE",
            "NO_DIGIT",
        }

    async def test_short_email_local_part_is_not_treated_as_a_password_rule(self, service):
        """A one-to-three character local part is too weak a signal to enforce.

        Banning "ali" would reject most of the alphabet for anyone with a short
        address, which is a worse failure than the rule it is trying to prevent.
        """
        result = await service.register(
            registration(email="ali@example.com", password="Ali-Example-2026")
        )

        assert result.user.email == "ali@example.com"

    async def test_rejects_password_containing_the_email(self, service):
        with pytest.raises(ValidationFailed) as caught:
            await service.register(
                registration(email="alina.haddad@example.com", password="Alina-Haddad-2026")
            )

        assert any(p.get("code") == "CONTAINS_EMAIL" for p in caught.value.details["problems"])

    async def test_rejects_invalid_email(self, service):
        with pytest.raises(ValidationFailed) as caught:
            await service.register(registration(email="not-an-email"))

        assert caught.value.details["field"] == "email"

    async def test_rejects_unknown_timezone(self, service):
        with pytest.raises(ValidationFailed) as caught:
            await service.register(registration(timezone="AST"))

        assert caught.value.details["field"] == "timezone"

    async def test_rejects_short_display_name(self, service):
        with pytest.raises(ValidationFailed):
            await service.register(registration(display_name="A"))


# ========================================================================= login
class TestLogin:
    async def test_returns_tokens_and_me(self, service):
        await service.register(registration())

        result = await service.login(LoginCommand(email="ali@example.com", password=GOOD_PASSWORD))

        assert result.user.email == "ali@example.com"
        assert result.tokens.access_token

    async def test_email_match_is_case_insensitive(self, service):
        await service.register(registration())

        result = await service.login(LoginCommand(email="ALI@EXAMPLE.COM", password=GOOD_PASSWORD))

        assert result.user.email == "ali@example.com"

    async def test_wrong_password_is_rejected_without_saying_which(self, service):
        await service.register(registration())

        with pytest.raises(UnauthorizedError) as caught:
            await service.login(LoginCommand(email="ali@example.com", password="Wrong-pass-99"))

        assert caught.value.code == ErrorCode.AUTH_INVALID_CREDENTIALS

    async def test_unknown_email_produces_the_same_error_as_a_wrong_password(self, service):
        await service.register(registration())

        with pytest.raises(UnauthorizedError) as known:
            await service.login(LoginCommand(email="ali@example.com", password="Wrong-pass-99"))
        with pytest.raises(UnauthorizedError) as unknown:
            await service.login(LoginCommand(email="nobody@example.com", password="Wrong-pass-99"))

        # Identical code and message: the response must not reveal which accounts exist.
        assert known.value.code == unknown.value.code
        assert known.value.message == unknown.value.message

    async def test_failed_attempts_persist_and_lock_the_account(self, service, settings, session):
        await service.register(registration())

        for _ in range(settings.login_max_failed_attempts):
            with pytest.raises(UnauthorizedError):
                await service.login(LoginCommand(email="ali@example.com", password="Wrong-pass-99"))

        # The counter has to be committed, not rolled back with the raised error,
        # otherwise the lockout is a no-op against a determined attacker.
        stored = (await session.execute(select(UserModel))).scalar_one()
        assert stored.failed_login_attempts == settings.login_max_failed_attempts
        assert stored.locked_until is not None

        with pytest.raises(RateLimitedError) as caught:
            await service.login(LoginCommand(email="ali@example.com", password=GOOD_PASSWORD))
        assert caught.value.code == ErrorCode.AUTH_ACCOUNT_LOCKED

    async def test_successful_login_clears_the_failure_counter(self, service, session):
        await service.register(registration())

        with pytest.raises(UnauthorizedError):
            await service.login(LoginCommand(email="ali@example.com", password="Wrong-pass-99"))
        result = await service.login(LoginCommand(email="ali@example.com", password=GOOD_PASSWORD))

        assert result.user.email == "ali@example.com"
        stored = (await session.execute(select(UserModel))).scalar_one()
        assert stored.failed_login_attempts == 0
        assert stored.locked_until is None

    async def test_suspended_account_is_refused(self, service, session):
        await service.register(registration())
        stored = (await session.execute(select(UserModel))).scalar_one()
        stored.status = UserStatus.SUSPENDED
        await session.commit()

        with pytest.raises(ForbiddenError) as caught:
            await service.login(LoginCommand(email="ali@example.com", password=GOOD_PASSWORD))
        assert caught.value.code == ErrorCode.AUTH_ACCOUNT_DISABLED

    async def test_deleted_account_is_refused(self, service, session):
        await service.register(registration())
        stored = (await session.execute(select(UserModel))).scalar_one()
        stored.status = UserStatus.DELETED
        await session.commit()

        with pytest.raises(UnauthorizedError) as caught:
            await service.login(LoginCommand(email="ali@example.com", password=GOOD_PASSWORD))
        assert caught.value.code == ErrorCode.USER_DELETED


# ======================================================================= refresh
class TestRefresh:
    async def test_rotates_the_token_and_preserves_the_family(self, service, session):
        registered = await service.register(registration())
        first = registered.tokens.refresh_token

        rotated = await service.refresh(RefreshCommand(refresh_token=first))

        assert rotated.tokens.refresh_token != first
        rows = (await session.execute(select(RefreshTokenModel))).scalars().all()
        # Both the consumed and the replacement exist: the old one is kept as evidence
        # of the chain so a replay can be detected.
        assert len(rows) == 2
        assert {row.family_id for row in rows} == {rows[0].family_id}
        assert sorted(row.revoked_at is not None for row in rows) == [False, True]

    async def test_replaying_a_rotated_token_kills_the_whole_family(self, service, session):
        registered = await service.register(registration())
        first = registered.tokens.refresh_token
        # The rotation that consumes `first` also hands us the successor, so the replay
        # below is a *second* use of an already-consumed token.
        successor = (await service.refresh(RefreshCommand(refresh_token=first))).tokens

        with pytest.raises(UnauthorizedError) as caught:
            await service.refresh(RefreshCommand(refresh_token=first))
        assert caught.value.code == ErrorCode.AUTH_REFRESH_REUSED

        # The successor is collateral damage by design: if the old token replayed, the
        # chain is compromised even if the newest token was not stolen.
        with pytest.raises(UnauthorizedError):
            await service.refresh(RefreshCommand(refresh_token=successor.refresh_token))

        rows = (await session.execute(select(RefreshTokenModel))).scalars().all()
        assert all(row.revoked_at is not None for row in rows)

    async def test_garbage_token_is_rejected(self, service):
        with pytest.raises(UnauthorizedError):
            await service.refresh(RefreshCommand(refresh_token="not-a-jwt"))

    async def test_access_token_is_not_accepted_as_a_refresh_token(self, service):
        registered = await service.register(registration())

        with pytest.raises(UnauthorizedError):
            await service.refresh(RefreshCommand(refresh_token=registered.tokens.access_token))

    async def test_unknown_jti_is_rejected(self, service):
        registered = await service.register(registration())

        # Correctly signed and well-formed, but its jti was never persisted. Mutating
        # characters in an existing token would only corrupt the signature and test the
        # wrong rejection path, so a genuinely foreign token is minted instead.
        forged, _claims = create_refresh_token(registered.user_id)

        with pytest.raises(UnauthorizedError) as caught:
            await service.refresh(RefreshCommand(refresh_token=forged))
        assert caught.value.code in {
            ErrorCode.AUTH_REFRESH_INVALID,
            ErrorCode.AUTH_REFRESH_REUSED,
        }

    async def test_tampered_signature_is_rejected(self, service):
        registered = await service.register(registration())

        # Same token, damaged signature: this must not be reported as a reuse.
        damaged = registered.tokens.refresh_token[:-3] + "aaa"

        with pytest.raises(UnauthorizedError) as caught:
            await service.refresh(RefreshCommand(refresh_token=damaged))
        assert caught.value.code == ErrorCode.AUTH_TOKEN_INVALID

    async def test_raw_token_is_never_persisted(self, service, session):
        registered = await service.register(registration())
        claims = decode_token(registered.tokens.refresh_token, "refresh")

        rows = (await session.execute(select(RefreshTokenModel))).scalars().all()
        assert rows
        for row in rows:
            # The column holds the digest of the jti, never the jti and never the token.
            assert row.jti_hash == hash_token(claims.jti)
            assert row.jti_hash != claims.jti
            assert len(row.jti_hash) == 64  # SHA-256 hex

        # Nothing replayable exists in the table, so a database dump cannot be used to
        # impersonate the learner.
        persisted = " ".join(str(row.jti_hash) for row in rows)
        assert registered.tokens.refresh_token not in persisted
        assert claims.jti not in persisted
        assert registered.tokens.access_token not in persisted


# ======================================================================= logout
class TestLogout:
    async def test_revokes_the_presented_chain(self, service, session):
        token = (await service.register(registration())).tokens.refresh_token

        await service.logout(LogoutCommand(refresh_token=token))

        with pytest.raises(UnauthorizedError):
            await service.refresh(RefreshCommand(refresh_token=token))

    async def test_all_devices_revokes_every_chain(self, service):
        first = (await service.register(registration())).tokens.refresh_token
        second = (
            await service.login(LoginCommand(email="ali@example.com", password=GOOD_PASSWORD))
        ).tokens.refresh_token

        await service.logout(LogoutCommand(refresh_token=first, all_devices=True))

        for token in (first, second):
            with pytest.raises(UnauthorizedError):
                await service.refresh(RefreshCommand(refresh_token=token))

    async def test_unparseable_token_is_a_no_op(self, service):
        # Idempotent: a client retrying after a dropped response must not see an error.
        await service.logout(LogoutCommand(refresh_token="garbage"))


# ================================================================ password reset
class TestPasswordReset:
    async def test_unknown_address_reports_nothing_and_sends_nothing(self, service, delivery):
        assert (
            await service.forgot_password(ForgotPasswordCommand(email="nobody@example.com")) is None
        )
        assert delivery.sent == []

    async def test_dispatches_a_link_and_stores_only_its_digest(self, service, delivery, session):
        await service.register(registration())

        ticket = await service.forgot_password(ForgotPasswordCommand(email="ali@example.com"))

        assert ticket is not None
        assert len(ticket.token) == 64
        assert delivery.latest_for("ali@example.com") is not None

        rows = (await session.execute(select(PasswordResetTokenModel))).scalars().all()
        assert len(rows) == 1
        # The digest cannot be reversed, so a database leak yields no usable link.
        assert rows[0].token_hash == hash_token(ticket.token)
        assert ticket.token not in rows[0].token_hash

    async def test_new_request_invalidates_the_previous_link(self, service, session):
        await service.register(registration())
        first = await service.forgot_password(ForgotPasswordCommand(email="ali@example.com"))
        await service.forgot_password(ForgotPasswordCommand(email="ali@example.com"))

        with pytest.raises(ValidationFailed):
            await service.reset_password(
                ResetPasswordCommand(token=first.token, new_password="Baghdad-Reset-2026")
            )

    async def test_reset_changes_the_password_and_revokes_sessions(
        self, service, session, delivery
    ):
        registered = await service.register(registration())
        session_token = registered.tokens.refresh_token
        ticket = await service.forgot_password(ForgotPasswordCommand(email="ali@example.com"))

        await service.reset_password(
            ResetPasswordCommand(token=ticket.token, new_password="Baghdad-Reset-2026")
        )

        # The old password no longer works...
        with pytest.raises(UnauthorizedError):
            await service.login(LoginCommand(email="ali@example.com", password=GOOD_PASSWORD))
        # ...the new one does...
        await service.login(LoginCommand(email="ali@example.com", password="Baghdad-Reset-2026"))
        # ...and every session opened with the old one is gone.
        with pytest.raises(UnauthorizedError):
            await service.refresh(RefreshCommand(refresh_token=session_token))

    async def test_token_is_single_use(self, service):
        await service.register(registration())
        ticket = await service.forgot_password(ForgotPasswordCommand(email="ali@example.com"))
        await service.reset_password(
            ResetPasswordCommand(token=ticket.token, new_password="Baghdad-Reset-2026")
        )

        with pytest.raises(ValidationFailed):
            await service.reset_password(
                ResetPasswordCommand(token=ticket.token, new_password="Another-Reset-2026")
            )

    async def test_unknown_token_is_rejected(self, service):
        with pytest.raises(ValidationFailed) as caught:
            await service.reset_password(
                ResetPasswordCommand(token="deadbeef", new_password="Baghdad-Reset-2026")
            )
        assert caught.value.details["field"] == "token"

    async def test_reset_rejects_a_password_containing_the_address(self, service):
        await service.register(registration(email="alina.haddad@example.com"))
        ticket = await service.forgot_password(
            ForgotPasswordCommand(email="alina.haddad@example.com")
        )

        with pytest.raises(ValidationFailed) as caught:
            await service.reset_password(
                ResetPasswordCommand(token=ticket.token, new_password="Alina-Haddad-2026")
            )
        assert any(p.get("code") == "CONTAINS_EMAIL" for p in caught.value.details["problems"])


# =========================================================================== me
class TestMe:
    async def test_updates_profile_and_settings(self, service):
        registered = await service.register(registration())

        view = await service.update_me(
            registered.user_id,
            profile_changes=UpdateProfileCommand(
                display_name="  Ali H.  ", timezone="Asia/Baghdad"
            ),
            settings_changes=UpdateSettingsCommand(daily_goal_minutes=45),
        )

        assert view.profile.display_name == "Ali H."
        assert view.settings.daily_goal_minutes == 45

    async def test_rejects_an_unknown_timezone(self, service):
        registered = await service.register(registration())

        with pytest.raises(ValidationFailed) as caught:
            await service.update_me(
                registered.user_id, profile_changes=UpdateProfileCommand(timezone="GMT+3")
            )
        assert caught.value.details["field"] == "timezone"

    async def test_learner_cannot_self_assign_a_level(self, service):
        from app.core.types import CEFRLevel

        registered = await service.register(registration())

        with pytest.raises(ForbiddenError) as caught:
            await service.update_me(
                registered.user_id,
                profile_changes=UpdateProfileCommand(current_level=CEFRLevel.C1),
            )
        assert caught.value.details["field"] == "current_level"

    async def test_rejects_out_of_range_goals(self, service):
        registered = await service.register(registration())

        with pytest.raises(ValidationFailed):
            await service.update_me(
                registered.user_id, settings_changes=UpdateSettingsCommand(daily_goal_minutes=0)
            )
        with pytest.raises(ValidationFailed):
            await service.update_me(
                registered.user_id,
                settings_changes=UpdateSettingsCommand(daily_goal_new_words=5000),
            )

    async def test_me_is_scoped_to_the_caller(self, service):
        ali = await service.register(registration(email="ali@example.com"))
        await service.register(registration(email="dana@example.com", display_name="Dana"))

        dana = await service.get_me(ali.user_id)

        assert dana.email == "ali@example.com"

    async def test_notification_patch_leaves_unsent_channels_alone(self, service):
        registered = await service.register(registration())
        # Start from a state that differs from the dataclass defaults, so a revert to
        # defaults cannot masquerade as a correct merge.
        await service.update_me(
            registered.user_id,
            settings_changes=UpdateSettingsCommand(
                notification_preferences={
                    "review_due": False,
                    "quiet_hours_start": 23,
                }
            ),
        )

        view = await service.update_me(
            registered.user_id,
            settings_changes=UpdateSettingsCommand(
                notification_preferences={"email": False, "push": False}
            ),
        )

        prefs = view.settings.notification_preferences
        assert prefs.email is False
        assert prefs.push is False
        # Channels the caller never mentioned keep their stored values, including one
        # that differs from the default.
        assert prefs.review_due is False
        assert prefs.quiet_hours_start == 23
        assert prefs.daily_reminder is True

    async def test_explicit_null_in_a_patch_is_ignored(self, service):
        """A client that serialises unset fields as null must not blank a switch."""
        registered = await service.register(registration())

        view = await service.update_me(
            registered.user_id,
            settings_changes=UpdateSettingsCommand(
                notification_preferences={"email": False, "push": None}
            ),
        )

        assert view.settings.notification_preferences.email is False
        assert view.settings.notification_preferences.push is True

    async def test_unknown_preference_key_is_rejected(self, service):
        registered = await service.register(registration())

        with pytest.raises(ValueError, match="Unknown notification preference"):
            await service.update_me(
                registered.user_id,
                settings_changes=UpdateSettingsCommand(
                    notification_preferences={"weekly_repurt": False}
                ),
            )


# ======================================================================= devices
class TestDevices:
    async def test_registration_with_a_push_token_creates_a_device(self, service):
        registered = await service.register(
            registration(
                device=DeviceContext(platform="android", push_token="fcm-abc", app_version="1.2.0")
            )
        )

        devices = await service.list_devices(registered.user_id)

        assert len(devices) == 1
        assert devices[0].platform == "android"
        assert devices[0].app_version == "1.2.0"
        assert devices[0].push_enabled is True

    async def test_web_registration_creates_no_device_row(self, service):
        # Without a push token or app version there is no durable device identifier,
        # so a row would be a guess. Revocation is therefore only offered for installs
        # that can actually be recognised again.
        registered = await service.register(registration())

        assert await service.list_devices(registered.user_id) == []

    async def test_repeat_registration_reuses_one_row_per_account(self, service):
        context = DeviceContext(platform="android", push_token="fcm-abc", app_version="1.2.0")
        registered = await service.register(registration(device=context))
        await service.login(
            LoginCommand(email="ali@example.com", password=GOOD_PASSWORD, device=context)
        )

        devices = await service.list_devices(registered.user_id)

        assert len(devices) == 1
        assert devices[0].push_enabled is True

    async def test_revoke_device_ends_its_sessions(self, service):
        context = DeviceContext(platform="android", push_token="fcm-abc", app_version="1.2.0")
        registered = await service.register(registration(device=context))
        device = (await service.list_devices(registered.user_id))[0]
        # A second chain on the same device, so revocation has something to end.
        second = await service.login(
            LoginCommand(email="ali@example.com", password=GOOD_PASSWORD, device=context)
        )

        await service.revoke_device(registered.user_id, device.id)

        assert await service.list_devices(registered.user_id) == []
        with pytest.raises(UnauthorizedError):
            await service.refresh(RefreshCommand(refresh_token=second.tokens.refresh_token))

    async def test_cannot_revoke_another_accounts_device(self, service):
        ali = await service.register(
            registration(
                email="ali@example.com",
                device=DeviceContext(platform="android", push_token="fcm-ali"),
            )
        )
        dana = await service.register(
            registration(
                email="dana@example.com",
                display_name="Dana",
                device=DeviceContext(platform="android", push_token="fcm-dana"),
            )
        )
        ali_device = (await service.list_devices(ali.user_id))[0]

        with pytest.raises(ValidationFailed):
            await service.revoke_device(dana.user_id, ali_device.id)

    async def test_a_push_token_cannot_be_claimed_by_another_account(self, service):
        shared = DeviceContext(platform="android", push_token="fcm-shared")
        ali = await service.register(registration(email="ali@example.com", device=shared))
        # Same install, different learner: a shared family handset is the normal case.
        dana = await service.register(
            registration(email="dana@example.com", display_name="Dana", device=shared)
        )

        assert (await service.list_devices(ali.user_id))[0].push_enabled is True
        assert (await service.list_devices(dana.user_id))[0].push_enabled is True

    async def test_reauthenticating_a_revoked_device_revives_it(self, service):
        context = DeviceContext(platform="android", push_token="fcm-abc")
        registered = await service.register(registration(device=context))
        device = (await service.list_devices(registered.user_id))[0]
        await service.revoke_device(registered.user_id, device.id)

        await service.login(
            LoginCommand(email="ali@example.com", password=GOOD_PASSWORD, device=context)
        )

        assert len(await service.list_devices(registered.user_id)) == 1


# ================================================================== authenticate
class TestAuthenticate:
    async def test_resolves_a_valid_access_token(self, service):
        registered = await service.register(registration())

        user = await service.authenticate(registered.tokens.access_token)

        assert user.id == registered.user_id

    async def test_rejects_a_refresh_token_as_an_access_token(self, service):
        registered = await service.register(registration())

        with pytest.raises(UnauthorizedError):
            await service.authenticate(registered.tokens.refresh_token)

    async def test_rejects_a_token_for_a_deleted_account(self, service, session):
        registered = await service.register(registration())
        stored = await session.get(UserModel, registered.user_id)
        stored.status = UserStatus.DELETED
        await session.commit()
        # `commit` expires every attribute, so the next read would trigger a lazy
        # refresh mid-assertion. Dropping the identity map forces a clean load.
        session.expunge_all()

        with pytest.raises(UnauthorizedError) as caught:
            await service.authenticate(registered.tokens.access_token)
        assert caught.value.code == ErrorCode.USER_DELETED

    async def test_role_check_refuses_an_insufficient_role(self, service):
        from app.core.types import UserRole

        registered = await service.register(registration())
        user = await service.authenticate(registered.tokens.access_token)

        # A plain account satisfies its own role but not an elevated one.
        assert await service.require_role(user, UserRole.USER) is user
        with pytest.raises(ForbiddenError) as caught:
            await service.require_role(user, UserRole.ADMIN)
        assert caught.value.code == ErrorCode.AUTH_INSUFFICIENT_ROLE
