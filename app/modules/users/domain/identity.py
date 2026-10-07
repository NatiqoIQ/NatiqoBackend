"""External-identity port.

Sign-in is delegated to an identity provider (Google or Apple). The client obtains an
**ID token** from the provider's SDK on the device and posts it to the API; the server
verifies the token's signature against the provider's published keys and reads the
stable account identifier out of it.

Nothing here imports a provider SDK or an HTTP client. The application layer depends on
:class:`IdentityVerifierPort` and the composition root chooses the concrete adapter, so
a swap of verification strategy — or a test that needs to assert a flow without a
network — never reaches the use case.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class IdentityProvider(StrEnum):
    """A sign-in provider whose ID tokens this service is willing to trust."""

    GOOGLE = "google"
    APPLE = "apple"


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """The parts of a provider ID token the application is allowed to rely on.

    ``subject`` is the provider's stable, opaque user id (``sub``) and is the only value
    that is safe to key an account on. ``email`` is present for both providers but is
    **not** a guaranteed-unique key on its own: the same address can arrive from a
    Google account and an Apple "Hide My Email" relay, so identity resolution always
    tries ``(provider, subject)`` first and only falls back to the address.
    """

    provider: IdentityProvider
    subject: str
    email: str | None
    email_verified: bool
    display_name: str | None = None
    picture_url: str | None = None


class IdentityVerificationError(RuntimeError):
    """Raised when an ID token cannot be trusted.

    Carries a short, non-sensitive ``reason`` for the log line. It never carries the
    token: the token is a credential until the moment it is verified, and a logged
    credential is a leaked credential.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@runtime_checkable
class IdentityVerifierPort(Protocol):
    """Verifies one provider's ID tokens."""

    provider: IdentityProvider

    async def verify(self, id_token: str) -> VerifiedIdentity:
        """Return the identity asserted by ``id_token``, or raise.

        Implementations must verify the signature, the issuer, the audience and the
        expiry, and must raise :class:`IdentityVerificationError` for any of them.
        """
        ...
