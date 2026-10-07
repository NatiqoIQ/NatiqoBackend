"""Google and Apple ID-token verifiers.

Both providers sign the same kind of token; they differ in which claim carries the
learner's name and picture, and in the issuers and audiences that are acceptable. Those
differences are confined here.

A verifier is only constructed when its client ids are configured. A deployment with no
Google client id therefore has no Google verifier, and ``/auth/google`` answers
``AUTH_PROVIDER_UNAVAILABLE`` instead of accepting a token it cannot check.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.modules.users.domain.identity import (
    IdentityProvider,
    IdentityVerificationError,
    IdentityVerifierPort,
    VerifiedIdentity,
)
from app.platform.identity.jwks import JwksConfig, JwksTokenVerifier

#: Google has published both spellings of its issuer for years and uses them
#: interchangeably, so both are accepted.
GOOGLE_ISSUERS: tuple[str, ...] = ("https://accounts.google.com", "accounts.google.com")
APPLE_ISSUERS: tuple[str, ...] = ("https://appleid.apple.com",)


def _as_bool(value: Any) -> bool:
    """Coerce a claim that arrives as a bool from one provider and a string from the
    other. Google sends ``true``; Apple has been observed sending ``"true"``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _as_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _identity_from_claims(
    claims: dict[str, Any],
    *,
    provider: IdentityProvider,
    with_profile_claims: bool,
) -> VerifiedIdentity:
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        # ``sub`` is in the decode options' required list, but an empty string satisfies
        # "present" while keying nothing — and an identity keyed on nothing would merge
        # unrelated learners into one account.
        raise IdentityVerificationError("missing_subject")
    return VerifiedIdentity(
        provider=provider,
        # Bounded to the column width so a hostile token cannot fail the insert later,
        # after the account row has already been written.
        subject=subject[:255],
        email=_as_text(claims.get("email"), limit=254),
        email_verified=_as_bool(claims.get("email_verified")),
        display_name=_as_text(claims.get("name"), limit=120) if with_profile_claims else None,
        picture_url=_as_text(claims.get("picture"), limit=512) if with_profile_claims else None,
    )


class GoogleIdentityVerifier:
    """Verifies a Google ID token."""

    provider = IdentityProvider.GOOGLE

    def __init__(self, verifier: JwksTokenVerifier) -> None:
        self._verifier = verifier

    @classmethod
    def from_settings(cls, settings: Settings) -> GoogleIdentityVerifier:
        return cls(
            JwksTokenVerifier(
                JwksConfig(
                    issuers=GOOGLE_ISSUERS,
                    audiences=tuple(settings.google_client_ids),
                    jwks_url=settings.google_jwks_url,
                    cache_seconds=settings.jwks_cache_seconds,
                )
            )
        )

    async def verify(self, id_token: str) -> VerifiedIdentity:
        claims = await self._verifier.decode(id_token)
        return _identity_from_claims(claims, provider=self.provider, with_profile_claims=True)


class AppleIdentityVerifier:
    """Verifies an Apple ID token.

    Apple only returns the learner's name on the very first authorization, and it comes
    back through the client rather than the token, so no profile claim is read here. The
    account is keyed on ``sub``, which Apple guarantees is stable for the same developer
    team and the same learner.
    """

    provider = IdentityProvider.APPLE

    def __init__(self, verifier: JwksTokenVerifier) -> None:
        self._verifier = verifier

    @classmethod
    def from_settings(cls, settings: Settings) -> AppleIdentityVerifier:
        return cls(
            JwksTokenVerifier(
                JwksConfig(
                    issuers=APPLE_ISSUERS,
                    audiences=tuple(settings.apple_client_ids),
                    jwks_url=settings.apple_jwks_url,
                    cache_seconds=settings.jwks_cache_seconds,
                )
            )
        )

    async def verify(self, id_token: str) -> VerifiedIdentity:
        claims = await self._verifier.decode(id_token)
        return _identity_from_claims(claims, provider=self.provider, with_profile_claims=False)


def build_verifiers(settings: Settings) -> dict[IdentityProvider, IdentityVerifierPort]:
    """Build a verifier for every provider this deployment has credentials for."""
    verifiers: dict[IdentityProvider, IdentityVerifierPort] = {}
    if settings.uses_google:
        verifiers[IdentityProvider.GOOGLE] = GoogleIdentityVerifier.from_settings(settings)
    if settings.uses_apple:
        verifiers[IdentityProvider.APPLE] = AppleIdentityVerifier.from_settings(settings)
    return verifiers


__all__ = [
    "APPLE_ISSUERS",
    "GOOGLE_ISSUERS",
    "AppleIdentityVerifier",
    "GoogleIdentityVerifier",
    "build_verifiers",
]
