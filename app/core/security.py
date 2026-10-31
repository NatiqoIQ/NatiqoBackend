"""Cryptography: JWT issuing and verification.

Token model
-----------
* **Access token** — short-lived (15 min) JWT signed with ``JWT_SECRET``. Stateless.
  The client may present it as a bearer token. It is *not* stored server-side.
* **Refresh token** — long-lived JWT signed with ``JWT_REFRESH_SECRET`` carrying a
  ``jti`` and a ``family_id``. Only a hash of the ``jti`` is persisted, so a database
  leak cannot be replayed. Rotation is mandatory: presenting a consumed token is
  treated as theft and revokes the entire token family.

The two secrets are separate on purpose: a leaked access-signing key must not let an
attacker mint refresh tokens.

Rotation (F25)
--------------
Every token carries a ``kid`` header naming the key that signed it. The current key is
``JWT_KEY_ID``; ``JWT_PREVIOUS_KEY_ID`` plus the two previous secrets keep tokens minted
before a rotation verifiable for one access/refresh lifetime. Rotation is then: set the
current pair as "previous", publish a new current pair, deploy. Tokens without a ``kid``
predate rotation and are still verified against both keys so an upgrade is not a forced
logout.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import jwt

from app.core.config import get_settings
from app.core.errors import ErrorCode, UnauthorizedError
from app.core.time import utc_now
from app.core.types import UserId

TokenType = Literal["access", "refresh"]


# ------------------------------------------------------------------------ secrets
def hash_token(value: str) -> str:
    """SHA-256 hex digest used to store refresh ``jti`` values.

    Fast on purpose: this is a lookup key for a random 256-bit identifier, not a
    password, and it must stay indexable.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- JWT
@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Validated claims extracted from a token."""

    subject: uuid.UUID
    token_type: TokenType
    jti: str
    family_id: str | None
    device_id: uuid.UUID | None
    issued_at: datetime
    expires_at: datetime

    @property
    def user_id(self) -> UserId:
        return UserId(self.subject)


def _key_ring(token_type: TokenType) -> list[tuple[str, str]]:
    """``(kid, secret)`` pairs, current first, then the previous key during rotation."""
    settings = get_settings()
    if token_type == "access":  # noqa: S105 - comparing a token type, not a secret
        current = settings.jwt_secret.get_secret_value()
        previous = (
            settings.jwt_previous_secret.get_secret_value()
            if settings.jwt_previous_secret is not None
            else None
        )
    else:
        current = settings.jwt_refresh_secret.get_secret_value()
        previous = (
            settings.jwt_previous_refresh_secret.get_secret_value()
            if settings.jwt_previous_refresh_secret is not None
            else None
        )
    ring = [(settings.jwt_key_id, current)]
    if previous is not None and settings.jwt_previous_key_id is not None:
        ring.append((settings.jwt_previous_key_id, previous))
    return ring


def _encode(payload: dict[str, Any]) -> str:
    settings = get_settings()
    kid, secret = _key_ring(payload["typ"])[0]
    return jwt.encode(
        payload,
        secret,
        algorithm=settings.jwt_algorithm,
        headers={"kid": kid},
    )


def _decode_with_ring(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Verify a token against the key ring, selecting by ``kid`` when present.

    Selecting by ``kid`` is what makes rotation safe: without it a token signed with the
    old key would be rejected the moment the current key changes, or worse, accepted
    indefinitely if every key were tried without bound.
    """
    settings = get_settings()
    ring = _key_ring(expected_type)
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("The token is invalid.", code=ErrorCode.AUTH_TOKEN_INVALID) from exc

    kid = header.get("kid")
    candidates = ring
    if isinstance(kid, str):
        candidates = [pair for pair in ring if pair[0] == kid]
        if not candidates:
            raise UnauthorizedError("The token is invalid.", code=ErrorCode.AUTH_TOKEN_INVALID)

    last_error: Exception | None = None
    for _kid, secret in candidates:
        try:
            return jwt.decode(
                token,
                secret,
                algorithms=[settings.jwt_algorithm],
                audience=settings.jwt_audience,
                issuer=settings.jwt_issuer,
                options={"require": ["exp", "iat", "sub", "jti"]},
            )
        except jwt.ExpiredSignatureError as exc:
            # Expiry is key-independent: no point trying the other key for the same token.
            raise UnauthorizedError(
                "The token has expired.", code=ErrorCode.AUTH_TOKEN_EXPIRED
            ) from exc
        except jwt.InvalidTokenError as exc:
            last_error = exc
    raise UnauthorizedError(
        "The token is invalid.", code=ErrorCode.AUTH_TOKEN_INVALID
    ) from last_error


def create_access_token(
    user_id: uuid.UUID,
    *,
    ttl_seconds: int | None = None,
) -> tuple[str, TokenClaims]:
    settings = get_settings()
    ttl = ttl_seconds if ttl_seconds is not None else settings.access_token_ttl_seconds
    now = utc_now()
    expires = now + timedelta(seconds=ttl)
    jti = uuid.uuid4().hex
    payload = {
        "sub": str(user_id),
        "typ": "access",
        "jti": jti,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    claims = TokenClaims(
        subject=user_id,
        token_type="access",  # noqa: S106 - token type literal, not a secret
        jti=jti,
        family_id=None,
        device_id=None,
        issued_at=now,
        expires_at=expires,
    )
    return _encode(payload), claims


def create_refresh_token(
    user_id: uuid.UUID,
    *,
    family_id: str | None = None,
    device_id: uuid.UUID | None = None,
    ttl_seconds: int | None = None,
) -> tuple[str, TokenClaims]:
    """Mint a refresh token. ``family_id`` groups a rotation chain for reuse detection."""
    settings = get_settings()
    ttl = ttl_seconds if ttl_seconds is not None else settings.refresh_token_ttl_seconds
    now = utc_now()
    expires = now + timedelta(seconds=ttl)
    jti = uuid.uuid4().hex
    family = family_id or uuid.uuid4().hex
    payload = {
        "sub": str(user_id),
        "typ": "refresh",
        "jti": jti,
        "fid": family,
        "did": str(device_id) if device_id else None,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    claims = TokenClaims(
        subject=user_id,
        token_type="refresh",  # noqa: S106 - token type literal, not a secret
        jti=jti,
        family_id=family,
        device_id=device_id,
        issued_at=now,
        expires_at=expires,
    )
    return _encode(payload), claims


def decode_token(token: str, expected_type: TokenType) -> TokenClaims:
    """Verify signature, issuer, audience, expiry and type.

    Raises :class:`UnauthorizedError` with a precise machine-readable code so the
    client knows whether to refresh (``AUTH_TOKEN_EXPIRED``) or re-authenticate.
    """
    payload = _decode_with_ring(token, expected_type)

    if payload.get("typ") != expected_type:
        raise UnauthorizedError("Unexpected token type.", code=ErrorCode.AUTH_TOKEN_INVALID)

    try:
        subject = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("The token is invalid.", code=ErrorCode.AUTH_TOKEN_INVALID) from exc

    device_raw = payload.get("did")
    try:
        device_id = uuid.UUID(str(device_raw)) if device_raw else None
    except ValueError:
        device_id = None

    return TokenClaims(
        subject=subject,
        token_type=expected_type,
        jti=str(payload["jti"]),
        family_id=payload.get("fid"),
        device_id=device_id,
        issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=utc_now().tzinfo),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=utc_now().tzinfo),
    )
