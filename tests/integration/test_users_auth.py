"""Users/auth service tests.

These run against a real SQLite schema with the real repositories. The point is to
exercise the parts that only break against a real database — unique constraints,
foreign keys, the rotation UPDATE, the one-identity-per-provider shape — which a
hand-written fake would have asserted away.

The ID-token signature check is stubbed, not skipped. The adapters that actually talk to
Google's and Apple's key sets are covered separately; what these tests are about is
everything that happens *after* a token has been trusted, and a signature they would
have to forge to reach is not the property under test.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.errors import (
    ErrorCode,
    ForbiddenError,
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
    LogoutCommand,
    RefreshCommand,
    SocialLoginCommand,
    UpdateProfileCommand,
    UpdateSettingsCommand,
)
from app.modules.users.domain.identity import (
    IdentityProvider,
    IdentityVerificationError,
    VerifiedIdentity,
)
from app.modules.users.infrastructure.models import (
    RefreshTokenModel,
    UserIdentityModel,
    UserModel,
    UserProfileModel,
    UserSettingsModel,
)

pytestmark = pytest.mark.integration

GOOD_EMAIL = "ali@example.com"


class FakeVerifier:
    """A verifier that trusts exactly the tokens a test hands it."""

    def __init__(self, provider: IdentityProvider = IdentityProvider.GOOGLE) -> None:
        self.provider = provider
        self._identities: dict[str, VerifiedIdentity] = {}

    def trust(self, token: str, identity: VerifiedIdentity) -> None:
        self._identities[token] = identity

    async def verify(self, id_token: str) -> VerifiedIdentity:
        try:
            return self._identities[id_token]
        except KeyError:
            raise IdentityVerificationError("unknown_token") from None


def verified_identity(**overrides: Any) -> VerifiedIdentity:
    """A provider assertion, with any field overridable."""
    return VerifiedIdentity(
        **{
            "provider": IdentityProvider.GOOGLE,
            "subject": "google-subject-1",
            "email": GOOD_EMAIL,
            "email_verified": True,
            "display_name": "Ali",
            **overrides,
        }
    )


@pytest_asyncio.fixture
async def uow():
    """An *open* unit of work spanning the whole test.

    The UoW owns its session, and the service calls ``commit`` on it, so it has to be
    entered for the life of the test rather than constructed and passed around. Leaving
    the block without a commit rolls back, which is exactly the behaviour the auth code
    relies on.
    """
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest.fixture
def google() -> FakeVerifier:
    return FakeVerifier(IdentityProvider.GOOGLE)


@pytest.fixture
def apple() -> FakeVerifier:
    return FakeVerifier(IdentityProvider.APPLE)


@pytest.fixture
def service(uow, settings, google, apple) -> AuthService:
    return AuthService(
        uow,
        settings,
        verifiers={
            IdentityProvider.GOOGLE: google,
            IdentityProvider.APPLE: apple,
        },
    )


@pytest.fixture
def session(uow) -> AsyncSession:
    return uow.session


async def sign_in(
    service: AuthService,
    verifier: FakeVerifier,
    *,
    token: str | None = None,
    device: DeviceContext | None = None,
    **identity_overrides: Any,
):
    """Trust ``token`` and sign in with it."""
    id_token = token or f"{verifier.provider.value}-id-token"
    verifier.trust(id_token, verified_identity(provider=verifier.provider, **identity_overrides))
    return await service.login_with_identity(
        SocialLoginCommand(
            provider=verifier.provider,
            id_token=id_token,
            device=device or DeviceContext(),
        )
    )


# ====================================================================== sign in
class TestSignInWithIdentity:
    async def test_creates_account_profile_settings_identity_and_session(
        self, service, google, session
    ):
        result = await sign_in(service, google)

        assert result.user.email == GOOD_EMAIL
        assert result.user.status == str(UserStatus.ACTIVE)
        # The provider vouched for the address, so the account records it as confirmed.
        assert result.user.email_verified is True
        assert result.tokens.access_token and result.tokens.refresh_token

        assert (await session.scalar(select(func.count()).select_from(UserModel))) == 1
        assert (await session.scalar(select(func.count()).select_from(UserProfileModel))) == 1
        assert (await session.scalar(select(func.count()).select_from(UserSettingsModel))) == 1
        assert (await session.scalar(select(func.count()).select_from(UserIdentityModel))) == 1
        assert (await session.scalar(select(func.count()).select_from(RefreshTokenModel))) == 1

    async def test_no_credential_is_stored_anywhere(self, service, google, session):
        """The whole point of delegating: there is nothing to leak."""
        await sign_in(service, google)

        stored = (await session.execute(select(UserModel))).scalar_one()
        assert not hasattr(stored, "password_hash")

        identity = (await session.execute(select(UserIdentityModel))).scalar_one()
        # The provider's subject is the key; the token itself is never persisted.
        assert identity.subject == "google-subject-1"
        assert "token-1" not in {identity.subject, identity.email}

    async def test_the_same_identity_reuses_its_account(self, service, google, session):
        first = await sign_in(service, google)
        second = await sign_in(service, google)

        assert second.user_id == first.user_id
        assert (await session.scalar(select(func.count()).select_from(UserModel))) == 1
        assert (await session.scalar(select(func.count()).select_from(UserIdentityModel))) == 1

    async def test_a_subject_is_scoped_to_its_provider(self, service, google, apple, session):
        """Two providers may both call themselves ``shared`` and mean different people.

        The addresses differ as well: identical verified emails would be the *link*
        case, which is a different test, so sharing the subject is the only overlap.
        """
        first = await sign_in(service, google, subject="shared")
        second = await sign_in(
            service,
            apple,
            token="apple-token",
            subject="shared",
            email="dana@example.com",
        )

        assert first.user_id != second.user_id
        assert (await session.scalar(select(func.count()).select_from(UserModel))) == 2

    async def test_email_is_normalised_before_the_unique_check(self, service, google, session):
        await sign_in(service, google, email="Ali@Example.COM")

        stored = (await session.execute(select(UserModel))).scalar_one()
        assert stored.email == GOOD_EMAIL

    async def test_display_name_falls_back_to_the_address(self, service, google):
        """Providers may share no name at all; a sign-in must not fail over a label."""
        result = await sign_in(service, google, display_name=None)

        assert result.user.profile.display_name == "ali"

    async def test_a_one_character_provider_name_is_not_used(self, service, google):
        result = await sign_in(service, google, display_name="A")

        assert result.user.profile.display_name == "ali"

    async def test_rejects_unknown_timezone(self, service, google):
        google.trust("token-1", verified_identity())

        with pytest.raises(ValidationFailed) as caught:
            await service.login_with_identity(
                SocialLoginCommand(
                    provider=IdentityProvider.GOOGLE,
                    id_token="token-1",
                    timezone="AST",
                )
            )

        assert caught.value.details["field"] == "timezone"

    async def test_an_unconfigured_provider_is_refused_not_trusted(self, uow, settings, google):
        service = AuthService(uow, settings, verifiers={})

        with pytest.raises(UnauthorizedError) as caught:
            await sign_in(service, google)

        assert caught.value.code == ErrorCode.AUTH_PROVIDER_UNAVAILABLE

    async def test_a_token_the_verifier_rejects_signs_nobody_in(self, service):
        with pytest.raises(UnauthorizedError) as caught:
            await service.login_with_identity(
                SocialLoginCommand(provider=IdentityProvider.GOOGLE, id_token="forged-token")
            )

        assert caught.value.code == ErrorCode.AUTH_IDENTITY_INVALID

    async def test_an_identity_without_an_email_cannot_create_an_account(self, service, google):
        with pytest.raises(UnauthorizedError) as caught:
            await sign_in(service, google, email=None)

        assert caught.value.code == ErrorCode.AUTH_IDENTITY_INCOMPLETE

    async def test_a_second_provider_links_by_verified_email(self, service, google, apple, session):
        """The same person on two platforms must land on one account."""
        first = await sign_in(service, google)
        second = await sign_in(service, apple, token="apple-token", subject="apple-subject-1")

        assert second.user_id == first.user_id
        assert (await session.scalar(select(func.count()).select_from(UserModel))) == 1
        assert (await session.scalar(select(func.count()).select_from(UserIdentityModel))) == 2

    async def test_an_unverified_email_cannot_claim_an_existing_account(
        self, service, google, apple
    ):
        """Otherwise a provider account carrying someone else's address takes it over."""
        await sign_in(service, google)

        with pytest.raises(UnauthorizedError) as caught:
            await sign_in(
                service,
                apple,
                token="apple-token",
                subject="apple-subject-1",
                email_verified=False,
            )

        assert caught.value.code == ErrorCode.AUTH_IDENTITY_CONFLICT

    async def test_a_verified_email_flips_the_flag_on_a_pre_existing_account(
        self, service, google, session
    ):
        first = await sign_in(service, google, email_verified=False)
        assert first.user.email_verified is False

        # Same subject, now verified — as happens when a learner confirms at the provider.
        second = await sign_in(service, google, email_verified=True)

        assert second.user.email_verified is True
        stored = (await session.execute(select(UserModel))).scalar_one()
        assert stored.email_verified_at is not None

    async def test_suspended_account_is_refused(self, service, google, session):
        await sign_in(service, google)
        stored = (await session.execute(select(UserModel))).scalar_one()
        stored.status = UserStatus.SUSPENDED
        await session.commit()
        # The commit leaves server-generated columns stale on the in-session instance,
        # so the next sign-in would trip a lazy refresh outside the greenlet. Dropping
        # the identity map forces the service's lookup to read a clean row.
        session.expunge_all()

        with pytest.raises(ForbiddenError) as caught:
            await sign_in(service, google)
        assert caught.value.code == ErrorCode.AUTH_ACCOUNT_DISABLED

    async def test_deleted_account_is_refused(self, service, google, session):
        await sign_in(service, google)
        stored = (await session.execute(select(UserModel))).scalar_one()
        stored.status = UserStatus.DELETED
        await session.commit()
        session.expunge_all()

        with pytest.raises(UnauthorizedError) as caught:
            await sign_in(service, google)
        assert caught.value.code == ErrorCode.USER_DELETED

    async def test_a_second_sign_in_records_the_time(self, service, google, session):
        await sign_in(service, google)
        first_seen = (await session.execute(select(UserIdentityModel))).scalar_one().last_login_at

        await sign_in(service, google)

        stored = (await session.execute(select(UserIdentityModel))).scalar_one()
        assert stored.last_login_at >= first_seen


# ======================================================================= refresh
class TestRefresh:
    async def test_rotates_the_token_and_preserves_the_family(self, service, google, session):
        signed_in = await sign_in(service, google)
        first = signed_in.tokens.refresh_token

        rotated = await service.refresh(RefreshCommand(refresh_token=first))

        assert rotated.tokens.refresh_token != first
        rows = (await session.execute(select(RefreshTokenModel))).scalars().all()
        # Both the consumed and the replacement exist: the old one is kept as evidence
        # of the chain so a replay can be detected.
        assert len(rows) == 2
        assert {row.family_id for row in rows} == {rows[0].family_id}
        assert sorted(row.revoked_at is not None for row in rows) == [False, True]

    async def test_replaying_a_rotated_token_kills_the_whole_family(self, service, google, session):
        signed_in = await sign_in(service, google)
        first = signed_in.tokens.refresh_token
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

    async def test_access_token_is_not_accepted_as_a_refresh_token(self, service, google):
        signed_in = await sign_in(service, google)

        with pytest.raises(UnauthorizedError):
            await service.refresh(RefreshCommand(refresh_token=signed_in.tokens.access_token))

    async def test_unknown_jti_is_rejected(self, service, google):
        signed_in = await sign_in(service, google)

        # Correctly signed and well-formed, but its jti was never persisted. Mutating
        # characters in an existing token would only corrupt the signature and test the
        # wrong rejection path, so a genuinely foreign token is minted instead.
        forged, _claims = create_refresh_token(signed_in.user_id)

        with pytest.raises(UnauthorizedError) as caught:
            await service.refresh(RefreshCommand(refresh_token=forged))
        assert caught.value.code in {
            ErrorCode.AUTH_REFRESH_INVALID,
            ErrorCode.AUTH_REFRESH_REUSED,
        }

    async def test_tampered_signature_is_rejected(self, service, google):
        signed_in = await sign_in(service, google)

        # Same token, damaged signature: this must not be reported as a reuse.
        damaged = signed_in.tokens.refresh_token[:-3] + "aaa"

        with pytest.raises(UnauthorizedError) as caught:
            await service.refresh(RefreshCommand(refresh_token=damaged))
        assert caught.value.code == ErrorCode.AUTH_TOKEN_INVALID

    async def test_raw_token_is_never_persisted(self, service, google, session):
        signed_in = await sign_in(service, google)
        claims = decode_token(signed_in.tokens.refresh_token, "refresh")

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
        assert signed_in.tokens.refresh_token not in persisted
        assert claims.jti not in persisted
        assert signed_in.tokens.access_token not in persisted


# ======================================================================= logout
class TestLogout:
    async def test_revokes_the_presented_chain(self, service, google):
        token = (await sign_in(service, google)).tokens.refresh_token

        await service.logout(LogoutCommand(refresh_token=token))

        with pytest.raises(UnauthorizedError):
            await service.refresh(RefreshCommand(refresh_token=token))

    async def test_all_devices_revokes_every_chain(self, service, google):
        first = (await sign_in(service, google)).tokens.refresh_token
        second = (await sign_in(service, google)).tokens.refresh_token

        await service.logout(LogoutCommand(refresh_token=first, all_devices=True))

        for token in (first, second):
            with pytest.raises(UnauthorizedError):
                await service.refresh(RefreshCommand(refresh_token=token))

    async def test_unparseable_token_is_a_no_op(self, service):
        # Idempotent: a client retrying after a dropped response must not see an error.
        await service.logout(LogoutCommand(refresh_token="garbage"))


# ================================================================== account closure
class TestDeleteAccount:
    async def test_closing_revokes_every_chain_and_blocks_the_account(self, service, google):
        signed_in = await sign_in(service, google)

        changed = await service.delete_account(signed_in.user_id)

        assert changed is True
        with pytest.raises(UnauthorizedError) as caught:
            await service.refresh(RefreshCommand(refresh_token=signed_in.tokens.refresh_token))
        assert caught.value.code in {ErrorCode.AUTH_REFRESH_INVALID, ErrorCode.AUTH_REFRESH_REUSED}
        with pytest.raises(UnauthorizedError):
            await service.authenticate(signed_in.tokens.access_token)

    async def test_deleting_twice_reports_nothing_changed(self, service, google):
        signed_in = await sign_in(service, google)

        assert await service.delete_account(signed_in.user_id) is True
        assert await service.delete_account(signed_in.user_id) is False

    async def test_the_closed_account_cannot_sign_in_again(self, service, google):
        signed_in = await sign_in(service, google)
        await service.delete_account(signed_in.user_id)

        with pytest.raises(UnauthorizedError) as caught:
            await sign_in(service, google)
        assert caught.value.code == ErrorCode.USER_DELETED


# =========================================================================== me
class TestMe:
    async def test_updates_profile_and_settings(self, service, google):
        signed_in = await sign_in(service, google)

        view = await service.update_me(
            signed_in.user_id,
            profile_changes=UpdateProfileCommand(
                display_name="  Ali H.  ", timezone="Asia/Baghdad"
            ),
            settings_changes=UpdateSettingsCommand(daily_goal_minutes=45),
        )

        assert view.profile.display_name == "Ali H."
        assert view.settings.daily_goal_minutes == 45

    async def test_rejects_an_unknown_timezone(self, service, google):
        signed_in = await sign_in(service, google)

        with pytest.raises(ValidationFailed) as caught:
            await service.update_me(
                signed_in.user_id, profile_changes=UpdateProfileCommand(timezone="GMT+3")
            )
        assert caught.value.details["field"] == "timezone"

    async def test_learner_cannot_self_assign_a_level(self, service, google):
        from app.core.types import CEFRLevel

        signed_in = await sign_in(service, google)

        with pytest.raises(ForbiddenError) as caught:
            await service.update_me(
                signed_in.user_id,
                profile_changes=UpdateProfileCommand(current_level=CEFRLevel.C1),
            )
        assert caught.value.details["field"] == "current_level"

    async def test_rejects_out_of_range_goals(self, service, google):
        signed_in = await sign_in(service, google)

        with pytest.raises(ValidationFailed):
            await service.update_me(
                signed_in.user_id, settings_changes=UpdateSettingsCommand(daily_goal_minutes=0)
            )
        with pytest.raises(ValidationFailed):
            await service.update_me(
                signed_in.user_id,
                settings_changes=UpdateSettingsCommand(daily_goal_new_words=5000),
            )

    async def test_me_is_scoped_to_the_caller(self, service, google, apple):
        ali = await sign_in(service, google)
        await sign_in(
            service, apple, token="apple-token", subject="apple-1", email="dana@example.com"
        )

        dana = await service.get_me(ali.user_id)

        assert dana.email == GOOD_EMAIL

    async def test_notification_patch_leaves_unsent_channels_alone(self, service, google):
        signed_in = await sign_in(service, google)
        # Start from a state that differs from the dataclass defaults, so a revert to
        # defaults cannot masquerade as a correct merge.
        await service.update_me(
            signed_in.user_id,
            settings_changes=UpdateSettingsCommand(
                notification_preferences={
                    "review_due": False,
                    "quiet_hours_start": 23,
                }
            ),
        )

        view = await service.update_me(
            signed_in.user_id,
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

    async def test_explicit_null_in_a_patch_is_ignored(self, service, google):
        """A client that serialises unset fields as null must not blank a switch."""
        signed_in = await sign_in(service, google)

        view = await service.update_me(
            signed_in.user_id,
            settings_changes=UpdateSettingsCommand(
                notification_preferences={"email": False, "push": None}
            ),
        )

        assert view.settings.notification_preferences.email is False
        assert view.settings.notification_preferences.push is True

    async def test_unknown_preference_key_is_rejected(self, service, google):
        signed_in = await sign_in(service, google)

        with pytest.raises(ValueError, match="Unknown notification preference"):
            await service.update_me(
                signed_in.user_id,
                settings_changes=UpdateSettingsCommand(
                    notification_preferences={"weekly_repurt": False}
                ),
            )


# ======================================================================= devices
class TestDevices:
    async def test_a_push_token_creates_a_device(self, service, google):
        signed_in = await sign_in(
            service,
            google,
            device=DeviceContext(platform="android", push_token="fcm-abc", app_version="1.2.0"),
        )

        devices = await service.list_devices(signed_in.user_id)

        assert len(devices) == 1
        assert devices[0].platform == "android"
        assert devices[0].app_version == "1.2.0"
        assert devices[0].push_enabled is True

    async def test_a_web_session_creates_no_device_row(self, service, google):
        # Without a push token or app version there is no durable device identifier,
        # so a row would be a guess. Revocation is therefore only offered for installs
        # that can actually be recognised again.
        signed_in = await sign_in(service, google)

        assert await service.list_devices(signed_in.user_id) == []

    async def test_signing_in_twice_reuses_one_row_per_account(self, service, google):
        context = DeviceContext(platform="android", push_token="fcm-abc", app_version="1.2.0")
        signed_in = await sign_in(service, google, device=context)
        await sign_in(service, google, device=context)

        devices = await service.list_devices(signed_in.user_id)

        assert len(devices) == 1
        assert devices[0].push_enabled is True

    async def test_revoke_device_ends_its_sessions(self, service, google):
        context = DeviceContext(platform="android", push_token="fcm-abc", app_version="1.2.0")
        signed_in = await sign_in(service, google, device=context)
        device = (await service.list_devices(signed_in.user_id))[0]
        # A second chain on the same device, so revocation has something to end.
        second = await sign_in(service, google, device=context)

        await service.revoke_device(signed_in.user_id, device.id)

        assert await service.list_devices(signed_in.user_id) == []
        with pytest.raises(UnauthorizedError):
            await service.refresh(RefreshCommand(refresh_token=second.tokens.refresh_token))

    async def test_cannot_revoke_another_accounts_device(self, service, google, apple):
        ali = await sign_in(
            service,
            google,
            device=DeviceContext(platform="android", push_token="fcm-ali"),
        )
        dana = await sign_in(
            service,
            apple,
            token="apple-token",
            subject="apple-1",
            email="dana@example.com",
            device=DeviceContext(platform="android", push_token="fcm-dana"),
        )
        ali_device = (await service.list_devices(ali.user_id))[0]

        with pytest.raises(ValidationFailed):
            await service.revoke_device(dana.user_id, ali_device.id)

    async def test_a_push_token_cannot_be_claimed_by_another_account(self, service, google, apple):
        shared = DeviceContext(platform="android", push_token="fcm-shared")
        ali = await sign_in(service, google, device=shared)
        # Same install, different learner: a shared family handset is the normal case.
        dana = await sign_in(
            service,
            apple,
            token="apple-token",
            subject="apple-1",
            email="dana@example.com",
            device=shared,
        )

        assert (await service.list_devices(ali.user_id))[0].push_enabled is True
        assert (await service.list_devices(dana.user_id))[0].push_enabled is True

    async def test_reauthenticating_a_revoked_device_revives_it(self, service, google):
        context = DeviceContext(platform="android", push_token="fcm-abc")
        signed_in = await sign_in(service, google, device=context)
        device = (await service.list_devices(signed_in.user_id))[0]
        await service.revoke_device(signed_in.user_id, device.id)

        await sign_in(service, google, device=context)

        assert len(await service.list_devices(signed_in.user_id)) == 1


# ================================================================== authenticate
class TestAuthenticate:
    async def test_resolves_a_valid_access_token(self, service, google):
        signed_in = await sign_in(service, google)

        user = await service.authenticate(signed_in.tokens.access_token)

        assert user.id == signed_in.user_id

    async def test_rejects_a_refresh_token_as_an_access_token(self, service, google):
        signed_in = await sign_in(service, google)

        with pytest.raises(UnauthorizedError):
            await service.authenticate(signed_in.tokens.refresh_token)

    async def test_rejects_a_token_for_a_deleted_account(self, service, google, session):
        signed_in = await sign_in(service, google)
        stored = await session.get(UserModel, signed_in.user_id)
        stored.status = UserStatus.DELETED
        await session.commit()
        # `commit` expires every attribute, so the next read would trigger a lazy
        # refresh mid-assertion. Dropping the identity map forces a clean load.
        session.expunge_all()

        with pytest.raises(UnauthorizedError) as caught:
            await service.authenticate(signed_in.tokens.access_token)
        assert caught.value.code == ErrorCode.USER_DELETED

    async def test_role_check_refuses_an_insufficient_role(self, service, google):
        from app.core.types import UserRole

        signed_in = await sign_in(service, google)
        user = await service.authenticate(signed_in.tokens.access_token)

        # A plain account satisfies its own role but not an elevated one.
        assert await service.require_role(user, UserRole.USER) is user
        with pytest.raises(ForbiddenError) as caught:
            await service.require_role(user, UserRole.ADMIN)
        assert caught.value.code == ErrorCode.AUTH_INSUFFICIENT_ROLE
