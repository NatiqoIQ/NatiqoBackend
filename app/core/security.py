"""Cryptography: Argon2id password hashing and JWT issuing/verification.

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
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import get_settings
from app.core.errors import ErrorCode, UnauthorizedError
from app.core.time import utc_now
from app.core.types import UserId

TokenType = Literal["access", "refresh"]

#: Argon2id parameters are tuned for a VPS-class deployment; the cost values come
#: from configuration so they can be raised as hardware improves.
_hasher: PasswordHasher | None = None


def get_hasher() -> PasswordHasher:
    global _hasher
    if _hasher is None:
        settings = get_settings()
        _hasher = PasswordHasher(
            time_cost=settings.password_argon2_time_cost,
            memory_cost=settings.password_argon2_memory_cost,
            parallelism=settings.password_argon2_parallelism,
            hash_len=32,
            salt_len=16,
        )
    return _hasher


# --------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    return get_hasher().hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish verification. Never raises on malformed input."""
    try:
        return get_hasher().verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when a stored hash was produced with weaker parameters than configured."""
    try:
        return get_hasher().check_needs_rehash(password_hash)
    except (InvalidHashError, VerificationError):
        return True


#: Burn a comparable amount of CPU on unknown accounts so that response timing does
#: not disclose whether an email is registered.
_DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHR2YWx1ZQ$Zm9vYmFyYmF6cXV4Zm9vYmFyYmF6"


def dummy_verify() -> None:
    try:
        get_hasher().verify(_DUMMY_HASH, "timing-equaliser")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass


# ------------------------------------------------------------------------ secrets
def generate_opaque_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


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
    roles: tuple[str, ...] = ()

    @property
    def user_id(self) -> UserId:
        return UserId(self.subject)


def _secret_for(token_type: TokenType) -> str:
    settings = get_settings()
    if token_type == "access":  # noqa: S105 - comparing a token type, not a secret
        return settings.jwt_secret.get_secret_value()
    return settings.jwt_refresh_secret.get_secret_value()


def _encode(payload: dict[str, Any]) -> str:
    settings = get_settings()
    return jwt.encode(payload, _secret_for(payload["typ"]), algorithm=settings.jwt_algorithm)


def create_access_token(
    user_id: uuid.UUID,
    *,
    roles: tuple[str, ...] = (),
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
        "roles": list(roles),
    }
    claims = TokenClaims(
        subject=user_id,
        token_type="access",  # noqa: S106 - token type literal, not a secret
        jti=jti,
        family_id=None,
        device_id=None,
        issued_at=now,
        expires_at=expires,
        roles=roles,
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
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            _secret_for(expected_type),
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError(
            "The token has expired.", code=ErrorCode.AUTH_TOKEN_EXPIRED
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("The token is invalid.", code=ErrorCode.AUTH_TOKEN_INVALID) from exc

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
        roles=tuple(payload.get("roles") or ()),
    )
