"""Password-reset delivery adapters.

No SMTP or transactional-mail provider is configured for this project yet, so this
module ships the two adapters that can exist honestly today:

* :class:`LoggingPasswordResetDelivery` — the default in non-production. It records
  *that* a reset was requested and refuses to carry the token.
* :class:`InMemoryPasswordResetDelivery` — used by tests, which need to read the raw
  link to drive the reset endpoint.

There is deliberately no adapter that writes the token to a log, and no default that
silently drops the message while claiming the flow works. Until a real provider is
wired, ``forgot_password`` completes the storage half of the flow and reports that
delivery is unavailable, rather than minting a token nobody can receive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.logging import get_logger
from app.core.types import UserId
from app.modules.users.domain.ports import PasswordResetDeliveryPort
from app.modules.users.infrastructure.email import SmtpEmailSender

logger = get_logger("app.modules.users")


@dataclass(frozen=True, slots=True)
class SentResetLink:
    """One dispatched reset link, captured for tests and local verification."""

    user_id: UserId
    email: str
    raw_token: str
    expires_at: datetime


class InMemoryPasswordResetDelivery:
    """Keeps dispatched links in memory so a test can complete the flow.

    Process-local and unbounded on purpose: this is a test/dev adapter, not a queue.
    """

    def __init__(self) -> None:
        self.sent: list[SentResetLink] = []

    async def send_reset_link(
        self,
        *,
        user_id: UserId,
        email: str,
        raw_token: str,
        expires_at: datetime,
    ) -> None:
        self.sent.append(
            SentResetLink(user_id=user_id, email=email, raw_token=raw_token, expires_at=expires_at)
        )

    def latest_for(self, email: str) -> SentResetLink | None:
        for link in reversed(self.sent):
            if link.email == email:
                return link
        return None

    def clear(self) -> None:
        self.sent.clear()


class LoggingPasswordResetDelivery:
    """Records that a reset was requested, without the token.

    The default outside production. It exists so the request is auditable and so a
    developer can see that the use case reached delivery, but it cannot complete the
    flow — a real provider is required for that, and pretending otherwise would leave
    learners locked out with no way to recover.
    """

    async def send_reset_link(
        self,
        *,
        user_id: UserId,
        email: str,
        raw_token: str,
        expires_at: datetime,
    ) -> None:
        logger.warning(
            "auth.password_reset_not_delivered",
            user_id=str(user_id),
            email_domain=email.rsplit("@", 1)[-1],
            expires_at=expires_at.isoformat(),
            detail="No mail provider is configured; the reset link was not sent.",
        )


def build_delivery(
    *,
    production: bool,
    override: PasswordResetDeliveryPort | None = None,
    smtp_sender: SmtpEmailSender | None = None,
    public_web_base_url: str = "https://natiqo.app",
) -> PasswordResetDeliveryPort:
    """Choose the delivery adapter for the current environment.

    ``override`` is the explicit injection point: a test passes an
    :class:`InMemoryPasswordResetDelivery` to read back the link it just requested.
    ``smtp_sender`` is what a real deployment configures; when it is present the reset
    link is actually emailed instead of being logged as undeliverable.

    The ``production`` flag is accepted so a deployment that still has no SMTP host
    keeps a visible warning rather than silently dropping mail.
    """
    if override is not None:
        return override
    if smtp_sender is not None:
        return SmtpAuthDelivery(smtp_sender, base_url=public_web_base_url)
    if production:
        logger.warning("auth.reset_delivery_unconfigured", environment="production")
    return LoggingPasswordResetDelivery()


class SmtpAuthDelivery:
    """Emails the password-reset and verification links over SMTP.

    Implements both auth delivery ports from one sender because both messages are the
    same shape — a single-use token inside a link — and splitting them would mean two
    places to get the "never log the token" rule wrong.
    """

    def __init__(self, sender: SmtpEmailSender, *, base_url: str) -> None:
        self._sender = sender
        # Normalised here rather than only at the factory: every caller appends
        # "/reset-password", so a config value of "https://natiqo.app/" would otherwise
        # yield a doubled slash in a link the learner is being asked to trust.
        self._base_url = base_url.rstrip("/")

    async def send_reset_link(
        self,
        *,
        user_id: UserId,
        email: str,
        raw_token: str,
        expires_at: datetime,
    ) -> None:
        minutes = max(1, int((expires_at - _now()).total_seconds() // 60))
        await self._sender.send(
            to=email,
            subject="Natiqo \u2014 \u0625\u0639\u0627\u062f\u0629 \u062a\u0623\u0643\u064a\u062f\u0629 \u0627\u0644\u0631\u0645\u0632 \u0627\u0644\u0645\u0631\u0648\u0631",
            body=(
                "\u0635\u0644\u064d\u062d \u0627\u0644\u0633\u0644\u0627\u0645\u060c\n\n"
                "\u0623\u0646\u0634\u0623 \u0637\u0644\u0628\u062a \u0625\u0639\u0627\u062f\u0629 \u0627\u0644\u0631\u0645\u0632 \u0627\u0644\u0645\u0631\u0648\u0631 \u0644\u062d\u0633\u0627\u0628\u0643."
                f"\u0644\u0647\u0643 {minutes} \u062f\u0642\u064a\u0642\u0629 \u0644\u0627\u0633\u062a\u062e\u062f\u0627\u0645\u0647\u060c"
                "\u0648\u0644\u064a\u064f\u0633\u062a\u062e\u062f\u0645 \u0645\u0631\u0629 \u0648\u0627\u062d\u062f\u0629\u060c"
                "\u0644\u0623\u0646 \u0644\u0645 \u062a\u0643\u0646 \u0623\u0646\u062a \u0647\u0648 \u0627\u0644\u0634\u062e\u0635 \u0645\u064a\u0643\u0646 \u0627\u0637\u0644\u0644\u0628 \u0645\u0646 \u0647\u0630\u0627 "
                "\u0627\u0644\u0628\u0631\u064a\u062f\u060c \u0623\u062a\u0631\u0643 \u0647\u0630\u0627 \u0627\u0644\u0631\u0627\u0628\u0637\u060c\n\n"
                f"{self._base_url}/reset-password?token={raw_token}\n\n"
                "\u0645\u0631\u062d\u064b\u0627 \u0628\u0645\u0634\u0627\u0647\u062f\u0629\u060c \u062a\u0623\u0643\u064a\u062f\u0629 \u0627\u0644\u0631\u0645\u0632 "
                "\u062a\u0645\u062a \u062a\u062d\u062f\u064a\u062f\u0647\u0627 \u0644\u0644\u0635\u0644\u0627\u062d\u0629."
            ),
        )
        logger.info(
            "auth.password_reset_delivered",
            user_id=str(user_id),
            email_domain=email.rsplit("@", 1)[-1],
            expires_at=expires_at.isoformat(),
        )

    async def send_verification_link(
        self,
        *,
        user_id: UserId,
        email: str,
        raw_token: str,
        expires_at: datetime,
    ) -> None:
        hours = max(1, int((expires_at - _now()).total_seconds() // 3600))
        await self._sender.send(
            to=email,
            subject="Natiqo \u2014 \u062a\u0623\u0643\u064a\u062f \u0628\u0631\u064a\u062f\u0643",
            body=(
                "\u0645\u0631\u062d\u064b\u0627\u0628\u064b\u0627\u060c\n\n"
                "\u0644\u062a\u0623\u0643\u064a\u062f \u0628\u0631\u064a\u062f\u0643 \u0627\u0644\u0628\u0631\u064a\u062f\u064a \u0644\u062a\u0634\u064a\u063a\u064a\u0631 \u0627\u0644\u0628\u0631\u064a\u062f."
                f"\u0644\u0647\u0643 {hours} \u0633\u0627\u0639\u0629\u060c \u0648\u062a\u064f\u0633\u062a\u062e\u062f\u0645 \u0645\u0631\u0629 \u0648\u0627\u062d\u062f\u0629.\n\n"
                f"{self._base_url}/verify-email?token={raw_token}\n\n"
                "\u0625\u0646\u0644\u0645 \u062a\u0637\u0644\u0628\u062a \u0647\u0630\u0627 \u0627\u0644\u0631\u0627\u0628\u0637\u061f \u062a\u0623\u0643\u064a\u062f \u064a\u0645\u0643\u0646 "
                "\u0625\u0646\u0634\u0627\u0621 \u064a\u0645\u0643\u0646 \u0627\u0644\u0636\u0639\u0641 \u0625\u0644\u0649 \u0647\u0630\u0627 \u0627\u0644\u0628\u0631\u064a\u062f."
            ),
        )
        logger.info(
            "auth.verification_delivered",
            user_id=str(user_id),
            email_domain=email.rsplit("@", 1)[-1],
            expires_at=expires_at.isoformat(),
        )


def _now() -> datetime:
    from app.core.time import utc_now

    return utc_now()


__all__ = [
    "InMemoryPasswordResetDelivery",
    "LoggingPasswordResetDelivery",
    "SentResetLink",
    "SmtpAuthDelivery",
    "build_delivery",
]
