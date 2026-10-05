"""Transactional email over SMTP.

The gap this closes is not theoretical. ``forgot_password`` returned ``200`` with
"If that address has an account, a reset link is on its way" and then, in every
deployment, handed the link to :class:`LoggingPasswordResetDelivery`, which logs that it
*could not* send it. The learner saw a success message and received nothing. This module
is the missing third option.

Everything is plain SMTP (``smtplib``/``aiosmtplib``) rather than a vendor SDK on
purpose: the project has no provider account, so wiring SendGrid or SES would be a guess
about credentials. SMTP is the protocol every provider accepts, including the
"relay on port 587" that Iraqi hosting providers tend to offer.

Two properties are load-bearing:

**The token is never logged.** :class:`LoggingPasswordResetDelivery` refuses to carry it,
and this adapter logs the recipient's domain and the expiry but never the body. A reset
token in a log is a password reset waiting to be read by whoever has log access.

**A send failure does not fail the request.** ``send_reset_link`` raises
:class:`EmailDeliveryError`; the caller decides. The choice belongs to the use case,
because the correct behaviour differs: a resend-verification request should tell the user
it failed, while a forgot-password request must still return the same indistinguishable
``200`` it returns for an unknown address, or it becomes an account-existence oracle.
"""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.core.logging import get_logger
from app.modules.users.domain.ports import EmailDeliveryError as _EmailDeliveryError

logger = get_logger("app.modules.users")


# Re-exported, not redefined: the application layer catches this to keep an SMTP outage
# from turning into an account-existence oracle, and it must be able to do that without
# importing an SMTP adapter. One class, one identity, imported from the port module.
EmailDeliveryError = _EmailDeliveryError


@dataclass(frozen=True, slots=True)
class SMTPConfig:
    host: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    sender: str = "noreply@natiqo.app"
    sender_name: str = "Natiqo"
    use_tls: bool = True
    timeout_seconds: float = 15.0


class SmtpEmailSender:
    """Send one message at a time over SMTP.

    One-at-a-time rather than a pooled session: the volume here is password resets and
    daily reminders, measured in hundreds per day, not the tens of thousands that would
    justify holding a connection open. It also means a dead server cannot leave a
    half-broken connection cached in the process.
    """

    def __init__(self, config: SMTPConfig) -> None:
        self.config = config

    async def send(self, *, to: str, subject: str, body: str, reply_to: str | None = None) -> None:
        message = self._build(to=to, subject=subject, body=body, reply_to=reply_to)
        await asyncio.to_thread(self._send_blocking, message)

    def _build(self, *, to: str, subject: str, body: str, reply_to: str | None) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((self.config.sender_name, self.config.sender))
        message["To"] = to
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=self.config.sender.rpartition("@")[2] or None)
        if reply_to:
            message["Reply-To"] = reply_to
        message.set_content(body)
        return message

    def _send_blocking(self, message: EmailMessage) -> None:
        cfg = self.config
        try:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout_seconds) as server:
                if cfg.use_tls:
                    # starttls *before* login: authenticating over a plaintext socket
                    # would send the password in the clear.
                    server.starttls()
                if cfg.username:
                    server.login(cfg.username, cfg.password or "")
                server.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            # The recipient is deliberately absent: a bounce log full of addresses is
            # a second copy of the user list, and the exception is the useful part.
            logger.error("email.send_failed", error_type=type(exc).__name__)
            raise EmailDeliveryError(type(exc).__name__) from None


def build_smtp_sender(
    *,
    host: str | None,
    port: int,
    username: str | None,
    password: str | None,
    sender: str,
    use_tls: bool,
) -> SmtpEmailSender | None:
    """Return a sender when SMTP is configured, else ``None``.

    ``None`` is the honest answer for an unconfigured environment; the caller falls back
    to the logging adapter rather than pretending a message went out.
    """
    if not host:
        return None
    return SmtpEmailSender(
        SMTPConfig(
            host=host,
            port=port,
            username=username,
            password=password,
            sender=sender,
            use_tls=use_tls,
        )
    )


__all__ = ["EmailDeliveryError", "SMTPConfig", "SmtpEmailSender", "build_smtp_sender"]
