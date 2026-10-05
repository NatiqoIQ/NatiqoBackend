"""SMTP delivery: the reset link actually leaves the process.

``forgot_password`` has always answered ``200`` and always handed the link to a logger.
That combination is the worst of both: the learner is told the email is on its way, and
it never arrives. These tests cover the adapter that fixes it, and they pin the two rules
that make it safe to use — the token never reaches a log, and a dead SMTP server surfaces
as an error instead of a silent success.
"""

from __future__ import annotations

import logging
import smtplib
import uuid
from datetime import timedelta

import pytest

from app.core.time import utc_now
from app.core.types import UserId
from app.modules.users.infrastructure.delivery import (
    LoggingPasswordResetDelivery,
    SmtpAuthDelivery,
    build_delivery,
)
from app.modules.users.infrastructure.email import (
    EmailDeliveryError,
    SMTPConfig,
    SmtpEmailSender,
    build_smtp_sender,
)

pytestmark = pytest.mark.integration

USER_ID = UserId(uuid.UUID("11111111-2222-3333-4444-555555555555"))
RAW_TOKEN = "reset-token-abcdef0123456789"


class _Recorder:
    """Stands in for ``SmtpEmailSender`` and keeps what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send(self, *, to: str, subject: str, body: str, reply_to: str | None = None) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body, "reply_to": reply_to or ""})


# ------------------------------------------------------------------ selection
def test_no_smtp_host_means_the_honest_adapter_is_kept():
    """An unconfigured environment must not pretend a message went out."""
    assert isinstance(build_delivery(production=False), LoggingPasswordResetDelivery)


def test_a_configured_host_selects_the_smtp_adapter():
    sender = build_smtp_sender(
        host="smtp.example.com",
        port=587,
        username="u",
        password="p",
        sender="no-reply@natiqo.app",
        use_tls=True,
    )
    delivery = build_delivery(production=True, smtp_sender=sender)
    assert isinstance(delivery, SmtpAuthDelivery)


def test_an_explicit_override_beats_the_configured_sender():
    """A test injecting a collector must not be overruled by ambient settings."""
    collector = _Recorder()
    delivery = build_delivery(
        production=False,
        override=collector,  # type: ignore[arg-type]
        smtp_sender=build_smtp_sender(
            host="smtp.example.com",
            port=587,
            username=None,
            password=None,
            sender="no-reply@natiqo.app",
            use_tls=True,
        ),
    )
    assert delivery is collector


def test_no_smtp_host_produces_no_sender():
    assert (
        build_smtp_sender(
            host=None, port=587, username=None, password=None, sender="a@b.c", use_tls=True
        )
        is None
    )


# ------------------------------------------------------------------ message body
async def test_the_reset_email_carries_a_working_link():
    """The link is the product. A body without it delivers nothing."""
    recorder = _Recorder()
    delivery = SmtpAuthDelivery(recorder, base_url="https://natiqo.app")  # type: ignore[arg-type]

    await delivery.send_reset_link(
        user_id=USER_ID,
        email="learner@example.com",
        raw_token=RAW_TOKEN,
        expires_at=utc_now() + timedelta(minutes=30),
    )

    assert len(recorder.sent) == 1
    message = recorder.sent[0]
    assert message["to"] == "learner@example.com"
    assert f"https://natiqo.app/reset-password?token={RAW_TOKEN}" in message["body"]


async def test_the_verification_email_carries_its_own_link():
    """Verification and reset are different flows and must not share a URL."""
    recorder = _Recorder()
    delivery = SmtpAuthDelivery(recorder, base_url="https://natiqo.app/")  # type: ignore[arg-type]

    await delivery.send_verification_link(
        user_id=USER_ID,
        email="learner@example.com",
        raw_token=RAW_TOKEN,
        expires_at=utc_now() + timedelta(hours=24),
    )

    body = recorder.sent[0]["body"]
    assert f"https://natiqo.app/verify-email?token={RAW_TOKEN}" in body
    assert "reset-password" not in body


async def test_a_trailing_slash_in_the_base_url_does_not_double_the_separator():
    """A config of `https://natiqo.app/` must not produce `app//reset-password`."""
    recorder = _Recorder()
    delivery = SmtpAuthDelivery(recorder, base_url="https://natiqo.app/")  # type: ignore[arg-type]

    await delivery.send_reset_link(
        user_id=USER_ID,
        email="learner@example.com",
        raw_token=RAW_TOKEN,
        expires_at=utc_now() + timedelta(minutes=30),
    )

    assert "//reset-password" not in recorder.sent[0]["body"]


# ------------------------------------------------------------------ secrets
async def test_the_token_never_reaches_a_log(caplog: pytest.LogCaptureFixture):
    """A reset token in a log is a password reset for whoever can read logs.

    This is asserted by scanning the formatted output, not by inspecting call arguments:
    a logger that interpolated the body into a message would pass an argument check.
    """
    recorder = _Recorder()
    delivery = SmtpAuthDelivery(recorder, base_url="https://natiqo.app")  # type: ignore[arg-type]

    with caplog.at_level(logging.DEBUG):
        await delivery.send_reset_link(
            user_id=USER_ID,
            email="learner@example.com",
            raw_token=RAW_TOKEN,
            expires_at=utc_now() + timedelta(minutes=30),
        )

    formatted = "\n".join(record.getMessage() for record in caplog.records)
    assert RAW_TOKEN not in formatted
    assert "learner@example.com" not in formatted


async def test_a_send_failure_surfaces_as_an_error(monkeypatch: pytest.MonkeyPatch):
    """Silently swallowing an SMTP failure is how the current bug shipped.

    The use case needs to know, because its response differs per flow: forgot-password
    must stay indistinguishable from an unknown address, resend-verification should not.
    """
    sender = SmtpEmailSender(SMTPConfig(host="smtp.invalid"))

    def _boom(*_args, **_kwargs):
        raise smtplib.SMTPConnectError(421, b"unavailable")

    monkeypatch.setattr("smtplib.SMTP", _boom)
    with pytest.raises(EmailDeliveryError):
        await sender.send(to="learner@example.com", subject="s", body="b")


async def test_the_failure_carries_no_address_or_token():
    """The exception is logged, so it must be safe to log."""
    sender = SmtpEmailSender(SMTPConfig(host="smtp.invalid"))
    with pytest.raises(EmailDeliveryError) as caught:
        await sender.send(to="learner@example.com", subject="Reset", body=f"token={RAW_TOKEN}")
    assert RAW_TOKEN not in str(caught.value)
    assert "learner@example.com" not in str(caught.value)


def test_starttls_is_requested_before_the_password_is_sent(monkeypatch: pytest.MonkeyPatch):
    """Authenticating over a plaintext socket leaks the password to the network.

    Ordering is asserted as a sequence rather than by flags, because a configuration
    that offers ``use_tls=False`` is legitimate; sending credentials anyway is not.
    """
    order: list[str] = []

    class _FakeSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def starttls(self):
            order.append("starttls")

        def login(self, _user, _password):
            order.append("login")

        def send_message(self, _message):
            order.append("send")

    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    sender = SmtpEmailSender(
        SMTPConfig(host="smtp.example.com", username="u", password="p", use_tls=True)
    )
    sender._send_blocking(sender._build(to="a@b.c", subject="s", body="b", reply_to=None))

    assert order == ["starttls", "login", "send"]


def test_no_login_is_attempted_when_no_username_is_configured(monkeypatch: pytest.MonkeyPatch):
    """An unauthenticated relay is a real setup; forcing login on it breaks delivery."""
    calls: list[str] = []

    class _FakeSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def starttls(self):
            calls.append("starttls")

        def login(self, *_args):
            calls.append("login")

        def send_message(self, _message):
            calls.append("send")

    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    sender = SmtpEmailSender(SMTPConfig(host="smtp.example.com", username=None))
    sender._send_blocking(sender._build(to="a@b.c", subject="s", body="b", reply_to=None))

    assert "login" not in calls
    assert calls == ["starttls", "send"]
