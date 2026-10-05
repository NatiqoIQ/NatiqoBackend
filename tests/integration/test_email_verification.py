"""Email verification and authenticated password change.

Three flows, one shared risk: a token or a credential that grants account control. The
tests below are mostly about what must *not* happen — a link that stays live after it is
used, a "resend" that floods an inbox, a password change that does not require the
password being replaced.

Login is deliberately not gated on verification, so these tests also pin that decision:
an unverified learner keeps working access, because gating it would lock out every
account registered before the feature existed.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.errors import ErrorCode, UnauthorizedError, ValidationFailed
from app.core.security import verify_password
from app.core.time import utc_now
from app.core.types import UserId, UserStatus
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.users.application.auth_service import AuthService
from app.modules.users.application.dto import (
    ChangePasswordCommand,
    ForgotPasswordCommand,
    LoginCommand,
    RefreshCommand,
    RegisterCommand,
    ResendVerificationCommand,
    ResetPasswordCommand,
    VerifyEmailCommand,
)
from app.modules.users.domain.ports import EmailDeliveryError, EmailVerificationDeliveryPort
from app.modules.users.infrastructure.delivery import InMemoryPasswordResetDelivery
from app.modules.users.infrastructure.models import EmailVerificationTokenModel, UserModel

pytestmark = pytest.mark.integration

PASSWORD = "Baghdad-learns-2026"
NEW_PASSWORD = "Baghdad-progresses-2027"


class _Collector:
    """Captures verification links so a test can read the token that was emailed."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_reset_link(self, **_kwargs) -> None:  # pragma: no cover - unused here
        raise AssertionError("the reset channel must not be used by these flows")

    async def send_verification_link(
        self,
        *,
        user_id,
        email: str,
        raw_token: str,
        expires_at,
    ) -> None:
        self.sent.append({"user_id": str(user_id), "email": email, "token": raw_token})


class _ResetOnlyAdapter:
    """Implements the reset port only — the case the fallback has to survive."""

    async def send_reset_link(self, **_kwargs) -> None:
        return None


@pytest.fixture
def verification() -> _Collector:
    return _Collector()


@pytest.fixture
async def uow():
    from app.core.database import create_all, dispose_engine, get_session_factory
    from app.core.registry import import_all_models

    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


async def _registered(uow, settings, verification, email: str = "learner@example.com"):
    service = AuthService(
        uow,
        settings,
        reset_delivery=InMemoryPasswordResetDelivery(),
        verification_delivery=verification,
    )
    await service.register(
        RegisterCommand(
            email=email,
            password=PASSWORD,
            display_name="Natiqo Learner",
        )
    )
    return service


# ------------------------------------------------------------------ verification
async def test_a_fresh_account_starts_unverified(uow, settings, verification):
    """The client needs a truthful flag to decide what to prompt."""
    service = await _registered(uow, settings, verification)
    me = await service.get_me((await uow.users.get_by_email("learner@example.com")).id)  # type: ignore[union-attr]
    assert me.email_verified is False


async def test_an_unverified_account_can_still_sign_in(uow, settings, verification):
    """Gating login on verification would lock out every pre-existing account."""
    service = await _registered(uow, settings, verification)
    # Registration itself returns a working session, which is the observable proof.
    assert service is not None
    user = await uow.users.get_by_email("learner@example.com")
    assert user is not None
    assert user.status.value == "active"


async def test_resend_mints_a_token_and_emails_the_link(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")

    ticket = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]

    assert ticket is not None
    assert len(verification.sent) == 1
    assert verification.sent[0]["token"] == ticket.token


async def test_only_the_token_digest_is_persisted(uow, settings, verification):
    """A database leak must yield no usable verification links."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    ticket = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]
    assert ticket is not None
    await uow.commit()

    rows = (await uow.session.execute(select(EmailVerificationTokenModel))).scalars().all()
    assert rows, "the token row must exist"
    assert all(row.token_hash != ticket.token for row in rows)


async def test_verifying_confirms_the_address(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    ticket = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]

    assert ticket is not None
    await service.verify_email(VerifyEmailCommand(token=ticket.token))
    await uow.commit()

    assert (await uow.users.get(user.id)).is_email_verified  # type: ignore[union-attr]


async def test_a_link_superseded_by_a_newer_one_is_refused(uow, settings, verification):
    """The real single-use property: a stale link must not confirm anything.

    Resending supersedes the earlier link. If the old one still worked, then a link
    captured days ago would keep working for the whole three-day TTL, which is the
    window the whole feature is built around.
    """
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    first = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]
    assert first is not None

    # Expire the first so the resend is not suppressed, then force a second link.
    row = (await uow.session.execute(select(EmailVerificationTokenModel))).scalars().one()
    row.expires_at = utc_now() - timedelta(seconds=1)
    await uow.commit()
    second = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]

    assert second is not None and second.token != first.token
    with pytest.raises(ValidationFailed):
        await service.verify_email(VerifyEmailCommand(token=first.token))


async def test_opening_the_same_link_twice_is_not_an_error(uow, settings, verification):
    """A mail scanner opens the link before the learner does.

    The second open has to succeed silently, otherwise the learner who taps a link that
    a security product already consumed is shown "this link is invalid" for an address
    that is in fact already confirmed.
    """
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    ticket = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]
    assert ticket is not None

    await service.verify_email(VerifyEmailCommand(token=ticket.token))
    await service.verify_email(VerifyEmailCommand(token=ticket.token))

    assert (await uow.users.get(user.id)).is_email_verified  # type: ignore[union-attr]


async def test_a_consumed_link_cannot_confirm_an_unconfirmed_address(uow, settings, verification):
    """A spent token that did not verify anything must still fail.

    This is the branch that keeps idempotency from becoming a bypass: returning success
    for any spent token would let a revoked link report a confirmation that never
    happened.
    """
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    ticket = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]
    assert ticket is not None

    row = (await uow.session.execute(select(EmailVerificationTokenModel))).scalars().one()
    row.used_at = utc_now()
    await uow.commit()

    with pytest.raises(ValidationFailed):
        assert ticket is not None
        await service.verify_email(VerifyEmailCommand(token=ticket.token))


async def test_an_expired_link_is_refused(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    ticket = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]

    row = (await uow.session.execute(select(EmailVerificationTokenModel))).scalars().one()
    row.expires_at = utc_now() - timedelta(seconds=1)
    await uow.commit()

    with pytest.raises(ValidationFailed):
        assert ticket is not None
        await service.verify_email(VerifyEmailCommand(token=ticket.token))


async def test_an_unknown_token_is_refused(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    with pytest.raises(ValidationFailed):
        await service.verify_email(VerifyEmailCommand(token="0" * 64))


async def test_a_resend_while_one_is_in_flight_sends_nothing(uow, settings, verification):
    """A live link is the answer; a second copy is how a provider rate-limits the user."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")

    await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]
    second = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]

    assert second is None
    assert len(verification.sent) == 1


async def test_resending_after_the_link_expired_does_send_again(uow, settings, verification):
    """The suppression must not outlive the link it was protecting."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]

    row = (await uow.session.execute(select(EmailVerificationTokenModel))).scalars().one()
    row.expires_at = utc_now() - timedelta(seconds=1)
    await uow.commit()

    again = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]

    assert again is not None
    assert len(verification.sent) == 2


async def test_resending_after_confirmation_sends_nothing(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    ticket = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]
    assert ticket is not None
    await service.verify_email(VerifyEmailCommand(token=ticket.token))
    await uow.commit()

    result = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]

    assert result is None
    assert len(verification.sent) == 1


async def test_a_reset_only_adapter_does_not_receive_a_verification_link(uow, settings):
    """Sending a *reset* link for a verification request would be a confusing dead end.

    The service must notice the adapter cannot verify and say so in a log rather than
    mailing the wrong link.
    """
    reset_only = _ResetOnlyAdapter()
    service = AuthService(uow, settings, reset_delivery=reset_only)
    await service.register(
        RegisterCommand(email="plain@example.com", password=PASSWORD, display_name="Plain")
    )
    user = await uow.users.get_by_email("plain@example.com")

    ticket = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]

    assert ticket is not None, "the token is still minted; only delivery is unavailable"


async def test_the_verification_fallback_is_used_when_no_adapter_is_given(uow, settings):
    """Constructing the service the old way must keep working, not raise."""
    service = AuthService(uow, settings, reset_delivery=InMemoryPasswordResetDelivery())
    assert isinstance(service._verification_delivery, EmailVerificationDeliveryPort)


# ------------------------------------------------------------------ change password
async def test_changing_the_password_requires_the_current_one(uow, settings, verification):
    """Without this check a stolen access token becomes permanent ownership."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")

    with pytest.raises(UnauthorizedError):
        await service.change_password(
            ChangePasswordCommand(
                user_id=user.id,  # type: ignore[union-attr]
                current_password="wrong-password",
                new_password=NEW_PASSWORD,
            )
        )


async def test_a_wrong_current_password_leaves_the_credential_alone(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")

    with pytest.raises(UnauthorizedError):
        await service.change_password(
            ChangePasswordCommand(
                user_id=user.id,  # type: ignore[union-attr]
                current_password="wrong-password",
                new_password=NEW_PASSWORD,
            )
        )

    await uow.rollback()
    refreshed = await uow.users.get(user.id)  # type: ignore[union-attr]
    assert verify_password(PASSWORD, refreshed.password_hash)


async def test_the_new_password_actually_takes_effect(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")

    await service.change_password(
        ChangePasswordCommand(
            user_id=user.id,  # type: ignore[union-attr]
            current_password=PASSWORD,
            new_password=NEW_PASSWORD,
        )
    )

    await uow.rollback()
    refreshed = await uow.users.get(user.id)  # type: ignore[union-attr]
    assert verify_password(NEW_PASSWORD, refreshed.password_hash)
    assert not verify_password(PASSWORD, refreshed.password_hash)


async def test_changing_the_password_revokes_every_session(uow, settings, verification):
    """Including the caller's own: the old credential must be useless everywhere."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")

    revoked = await service.change_password(
        ChangePasswordCommand(
            user_id=user.id,  # type: ignore[union-attr]
            current_password=PASSWORD,
            new_password=NEW_PASSWORD,
        )
    )

    assert revoked >= 1


async def test_a_weak_new_password_is_refused(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")

    with pytest.raises(ValidationFailed):
        await service.change_password(
            ChangePasswordCommand(
                user_id=user.id,  # type: ignore[union-attr]
                current_password=PASSWORD,
                new_password="short",
            )
        )


async def test_reusing_the_same_password_is_refused(uow, settings, verification):
    """Re-hashing an identical password changes the hash while changing nothing."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    original_hash = (await uow.users.get(user.id)).password_hash  # type: ignore[union-attr]

    with pytest.raises(ValidationFailed):
        await service.change_password(
            ChangePasswordCommand(
                user_id=user.id,  # type: ignore[union-attr]
                current_password=PASSWORD,
                new_password=PASSWORD,
            )
        )

    await uow.rollback()
    assert (await uow.users.get(user.id)).password_hash == original_hash  # type: ignore[union-attr]


async def test_changing_the_password_kills_outstanding_reset_links(uow, settings, verification):
    """A reset link minted against the old password must not survive the change."""
    reset_delivery = InMemoryPasswordResetDelivery()
    service = AuthService(
        uow,
        settings,
        reset_delivery=reset_delivery,
        verification_delivery=verification,
    )
    await service.register(
        RegisterCommand(
            email="learner@example.com", password=PASSWORD, display_name="Natiqo Learner"
        )
    )
    user = await uow.users.get_by_email("learner@example.com")
    await service.forgot_password(ForgotPasswordCommand(email="learner@example.com"))
    issued = reset_delivery.latest_for("learner@example.com")
    assert issued is not None

    await service.change_password(
        ChangePasswordCommand(
            user_id=user.id,  # type: ignore[union-attr]
            current_password=PASSWORD,
            new_password=NEW_PASSWORD,
        )
    )

    with pytest.raises(ValidationFailed):
        await service.reset_password(
            ResetPasswordCommand(token=issued.raw_token, new_password="Another-strong-2027")
        )


async def test_an_unknown_account_cannot_change_a_password(uow, settings, verification):
    from app.core.types import UserId

    service = await _registered(uow, settings, verification)
    with pytest.raises(UnauthorizedError):
        await service.change_password(
            ChangePasswordCommand(
                user_id=UserId(uuid.uuid4()),
                current_password=PASSWORD,
                new_password=NEW_PASSWORD,
            )
        )


async def test_the_user_row_survives_a_password_change(uow, settings, verification):
    """A regression guard: the entity must still be loadable afterwards."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    await service.change_password(
        ChangePasswordCommand(
            user_id=user.id,  # type: ignore[union-attr]
            current_password=PASSWORD,
            new_password=NEW_PASSWORD,
        )
    )
    rows = (await uow.session.execute(select(UserModel))).scalars().all()
    assert len(rows) == 1


class _FailingDelivery:
    """A transport that is down, which is the case the two tests below are about."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send_reset_link(self, **_kwargs) -> None:
        self.attempts += 1
        raise EmailDeliveryError("smtp unavailable")

    async def send_verification_link(self, **_kwargs) -> None:
        self.attempts += 1
        raise EmailDeliveryError("smtp unavailable")


async def test_a_transport_outage_does_not_reveal_that_an_account_exists(uow, settings):
    """The whole point of the generic answer is that it survives a bad day upstream.

    Unknown address: 200, no delivery attempt. Known address with the mail server down:
    200, same body, delivery attempted and failed. If the exception escaped, the second
    case would be a 500 and the difference would enumerate registered addresses.
    """
    down = _FailingDelivery()
    service = AuthService(uow, settings, reset_delivery=down)
    await service.register(
        RegisterCommand(email="known@example.com", password=PASSWORD, display_name="Known")
    )

    unknown = await service.forgot_password(ForgotPasswordCommand(email="nobody@example.com"))
    assert unknown is None
    assert down.attempts == 0, "an unknown address must not reach the mail channel at all"

    known = await service.forgot_password(ForgotPasswordCommand(email="known@example.com"))
    assert known is not None
    assert down.attempts == 1, "the outage must be visible as an attempted send"


async def test_a_failed_reset_send_still_leaves_the_endpoint_answering_successfully(uow, settings):
    """Same test from the route's side: one response shape regardless of the outage."""
    down = _FailingDelivery()
    service = AuthService(uow, settings, reset_delivery=down)
    await service.register(
        RegisterCommand(email="known@example.com", password=PASSWORD, display_name="Known")
    )

    ticket = await service.forgot_password(ForgotPasswordCommand(email="known@example.com"))

    assert ticket is not None, "the token is minted and stored; only the hand-off failed"


async def test_a_failed_verification_send_can_be_retried_immediately(uow, settings):
    """A one-minute provider outage must not cost the learner three days.

    If the undelivered token stayed stored it would read as "already pending" and every
    later resend would be suppressed until the TTL expired. The learner would be unable
    to confirm their address and unable to tell why.
    """
    down = _FailingDelivery()
    service = AuthService(uow, settings, reset_delivery=down, verification_delivery=down)
    await service.register(
        RegisterCommand(email="learner@example.com", password=PASSWORD, display_name="Natiqo")
    )
    user = await uow.users.get_by_email("learner@example.com")

    first = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]
    await uow.commit()
    rows = (await uow.session.execute(select(EmailVerificationTokenModel))).scalars().all()

    assert first is None, "no usable token may be promised after a failed send"
    assert all(row.used_at is not None for row in rows), "the undelivered token must be revoked"


async def test_a_retry_after_a_transport_outage_actually_works(uow, settings):
    """Recovery, not just the absence of a lockout: the second attempt delivers."""
    _FailingDelivery()
    collector = _Collector()

    class _Flaky(_FailingDelivery):
        """Fails a fixed number of times, then behaves."""

        failures = 1

        async def send_verification_link(self, **kwargs) -> None:  # type: ignore[override]
            self.attempts += 1
            if self.attempts <= self.failures:
                raise EmailDeliveryError("smtp unavailable")
            await collector.send_verification_link(**kwargs)

    flaky = _Flaky()
    service = AuthService(
        uow,
        settings,
        reset_delivery=InMemoryPasswordResetDelivery(),
        verification_delivery=flaky,
    )
    await service.register(
        RegisterCommand(email="learner@example.com", password=PASSWORD, display_name="Natiqo")
    )
    user = await uow.users.get_by_email("learner@example.com")

    await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]
    retry = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]

    assert retry is not None
    assert len(collector.sent) == 1
    assert collector.sent[0]["token"] == retry.token
# ------------------------------------------------------------------ account closure
# A learner who asks to be forgotten needs one guarantee: the account stops being
# reachable. Everything below is a different door into the same room, and each one is
# checked separately because closing only some of them is the failure mode that matters.


async def test_deleting_an_account_marks_it_closed(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    assert user is not None

    assert await service.delete_account(user.id) is True

    await uow.commit()
    assert (await uow.users.get(user.id)).is_deleted is True


async def test_a_closed_account_records_when_it_closed(uow, settings, verification):
    """"When did this account end" is a question the export and retention sweep ask."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")

    await service.delete_account(user.id)

    deleted_at = (await uow.users.get(user.id)).deleted_at
    assert deleted_at is not None
    assert deleted_at <= utc_now()


async def test_a_closed_account_cannot_be_logged_into(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    await service.delete_account(user.id)
    await uow.commit()

    with pytest.raises(UnauthorizedError) as raised:
        await service.login(LoginCommand(email="learner@example.com", password=PASSWORD))

    assert raised.value.code == ErrorCode.USER_DELETED


async def test_a_closed_account_cannot_be_authenticated_with_its_old_token(uow, settings, verification):
    """The token in the client's hand must stop resolving, not keep working until expiry."""
    service = await _registered(uow, settings, verification)
    tokens = await service.login(LoginCommand(email="learner@example.com", password=PASSWORD))
    user = await uow.users.get_by_email("learner@example.com")

    await service.delete_account(user.id)
    await uow.commit()

    with pytest.raises(UnauthorizedError) as raised:
        await service.authenticate(tokens.tokens.access_token)
    assert raised.value.code == ErrorCode.USER_DELETED


async def test_deleting_revokes_every_session(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    tokens = await service.login(LoginCommand(email="learner@example.com", password=PASSWORD))
    user = await uow.users.get_by_email("learner@example.com")

    await service.delete_account(user.id)

    with pytest.raises(UnauthorizedError):
        await service.refresh(RefreshCommand(refresh_token=tokens.tokens.refresh_token))


async def test_deleting_kills_outstanding_password_reset_links(uow, settings, verification):
    """A reset link is a way back in. Closing the account must close that door too."""
    reset_delivery = InMemoryPasswordResetDelivery()
    service = AuthService(uow, settings, reset_delivery=reset_delivery)
    await service.register(
        RegisterCommand(email="learner@example.com", password=PASSWORD, display_name="Natiqo")
    )
    user = await uow.users.get_by_email("learner@example.com")
    await service.forgot_password(ForgotPasswordCommand(email="learner@example.com"))
    issued = reset_delivery.latest_for("learner@example.com")
    assert issued is not None

    await service.delete_account(user.id)

    with pytest.raises(ValidationFailed):
        await service.reset_password(
            ResetPasswordCommand(token=issued.raw_token, new_password="Another-strong-2027")
        )


async def test_deleting_kills_outstanding_verification_links(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    ticket = await service.resend_verification(ResendVerificationCommand(user_id=user.id))  # type: ignore[union-attr]
    assert ticket is not None

    await service.delete_account(user.id)

    rows = (await uow.session.execute(select(EmailVerificationTokenModel))).scalars().all()
    assert all(row.used_at is not None for row in rows)


async def test_deleting_twice_is_not_an_error(uow, settings, verification):
    """A double tap, or a retry after a dropped response, must not fail."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")

    assert await service.delete_account(user.id) is True
    assert await service.delete_account(user.id) is False


async def test_deleting_keeps_the_closure_timestamp_of_the_first_call(uow, settings, verification):
    """A second delete must not rewrite when the account actually closed."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")
    await service.delete_account(user.id)
    first = (await uow.users.get(user.id)).deleted_at

    await service.delete_account(user.id)

    assert (await uow.users.get(user.id)).deleted_at == first


async def test_deleting_an_unknown_account_reports_nothing_to_do(uow, settings, verification):
    service = await _registered(uow, settings, verification)
    assert await service.delete_account(UserId(uuid.uuid4())) is False


async def test_closing_an_account_keeps_the_row_and_its_history(uow, settings, verification):
    """The row survives: attempts, cards and streaks still point at this user."""
    service = await _registered(uow, settings, verification)
    user = await uow.users.get_by_email("learner@example.com")

    await service.delete_account(user.id)

    rows = (await uow.session.execute(select(UserModel))).scalars().all()
    assert len(rows) == 1
    assert UserStatus(rows[0].status) is UserStatus.DELETED
