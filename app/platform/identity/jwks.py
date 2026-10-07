"""JWKS-backed ID-token verification.

Google and Apple both sign ID tokens with RS256 and publish the matching public keys as
a JWK set at a stable URL. Verification is therefore the same operation for both, and it
lives here once rather than twice.

Two properties are load-bearing:

**Every check is enforced.** Signature, algorithm, audience, expiry and issuer. Dropping
any one of them turns a verifier into a decoder, and a decoder accepts a token anyone
can mint.

**The key set is fetched off the event loop.** ``PyJWKClient`` fetches over a blocking
socket, so it runs in a worker thread. A synchronous fetch inside an ``async`` handler
would stall every other request for the duration of a network round trip.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import jwt

from app.modules.users.domain.identity import IdentityVerificationError

#: The only signing algorithm either provider uses for ID tokens. Pinned rather than
#: negotiated: accepting the token's own ``alg`` is the classic algorithm-confusion bug,
#: which lets an attacker sign a token with HMAC keyed on the public key.
_ALLOWED_ALGORITHMS = ["RS256"]


@dataclass(frozen=True, slots=True)
class JwksConfig:
    """Everything needed to verify one provider's tokens."""

    #: Accepted ``iss`` values. A tuple because Google publishes both the bare and the
    #: scheme-prefixed spelling of its issuer and uses them interchangeably.
    issuers: tuple[str, ...]
    #: Accepted ``aud`` values — the client ids this deployment registered.
    audiences: tuple[str, ...]
    jwks_url: str
    #: How long a fetched key set is reused. Providers rotate keys on their own schedule
    #: and publish the new one in the same set before signing with it, so a cache in the
    #: minutes-to-hours range is safe and keeps verification off the network.
    cache_seconds: int = 3600


class JwksTokenVerifier:
    """Verifies a token against a cached JWK set."""

    def __init__(self, config: JwksConfig) -> None:
        if not config.audiences:
            # Refusing at construction is the only safe behaviour: an empty audience
            # list would mean "accept any token this provider ever issued", including
            # ones minted for a different application.
            raise ValueError("At least one audience is required to verify an ID token.")
        self._config = config
        self._client = jwt.PyJWKClient(
            config.jwks_url,
            cache_jwk_set=True,
            lifespan=max(int(config.cache_seconds), 1),
        )

    async def decode(self, id_token: str) -> dict[str, Any]:
        """Return the verified claims, or raise :class:`IdentityVerificationError`."""
        try:
            return await asyncio.to_thread(self._decode_blocking, id_token)
        except IdentityVerificationError:
            raise
        except Exception as exc:  # noqa: BLE001 - any failure is a rejected token
            # A key-set fetch that times out is not the learner's fault and not a
            # reason to leak the distinction; it is still not a valid token.
            raise IdentityVerificationError(f"unavailable:{type(exc).__name__}") from None

    def _decode_blocking(self, id_token: str) -> dict[str, Any]:
        if not id_token or len(id_token) > 8192:
            # Bounded before any parsing: a megabyte of base64 is a denial-of-service
            # attempt, not a credential.
            raise IdentityVerificationError("malformed_token")

        try:
            signing_key = self._client.get_signing_key_from_jwt(id_token)
        except jwt.PyJWKClientConnectionError as exc:
            raise IdentityVerificationError("keys_unavailable") from exc
        except (jwt.PyJWKClientError, jwt.DecodeError) as exc:
            raise IdentityVerificationError("unknown_signing_key") from exc

        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                signing_key.key,
                algorithms=_ALLOWED_ALGORITHMS,
                audience=list(self._config.audiences),
                options={
                    "require": ["exp", "iat", "sub", "aud", "iss"],
                    # Verified below against every accepted spelling, because PyJWT
                    # compares ``iss`` to a single value and Google publishes two.
                    "verify_iss": False,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise IdentityVerificationError("expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise IdentityVerificationError("audience_mismatch") from exc
        except jwt.InvalidTokenError as exc:
            raise IdentityVerificationError("invalid_token") from exc

        if claims.get("iss") not in self._config.issuers:
            raise IdentityVerificationError("issuer_mismatch")
        return claims
