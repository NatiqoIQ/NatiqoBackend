"""JWT key rotation (F25).

Rotation must be a deploy, not a logout: a token minted with the previous key has to
stay valid for its lifetime, while a token claiming a key we never published must be
rejected even if its signature happens to verify against one of ours.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import jwt
import pytest
from pydantic import SecretStr

from app.core import security
from app.core.config import Settings
from app.core.errors import ErrorCode, UnauthorizedError
from app.core.time import utc_now

OLD_ACCESS = "old-access-secret-0000000000000000000000000000"
OLD_REFRESH = "old-refresh-secret-000000000000000000000000000"
NEW_ACCESS = "new-access-secret-1111111111111111111111111111"
NEW_REFRESH = "new-refresh-secret-111111111111111111111111111"


def build(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def before_rotation() -> Settings:
    return build(
        jwt_secret=SecretStr(OLD_ACCESS),
        jwt_refresh_secret=SecretStr(OLD_REFRESH),
        jwt_key_id="v1",
    )


def after_rotation() -> Settings:
    return build(
        jwt_secret=SecretStr(NEW_ACCESS),
        jwt_refresh_secret=SecretStr(NEW_REFRESH),
        jwt_key_id="v2",
        jwt_previous_key_id="v1",
        jwt_previous_secret=SecretStr(OLD_ACCESS),
        jwt_previous_refresh_secret=SecretStr(OLD_REFRESH),
    )


def payload(typ: str = "access", **extra: Any) -> dict[str, Any]:
    now = utc_now()
    base = {
        "sub": str(uuid.uuid4()),
        "typ": typ,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": "natiqo",
        "aud": "natiqo-clients",
    }
    base.update(extra)
    return base


class TestIssuing:
    def test_every_token_names_its_key(self, monkeypatch) -> None:
        monkeypatch.setattr(security, "get_settings", after_rotation)
        token, _ = security.create_access_token(uuid.uuid4())
        assert jwt.get_unverified_header(token)["kid"] == "v2"

        refresh, _ = security.create_refresh_token(uuid.uuid4())
        assert jwt.get_unverified_header(refresh)["kid"] == "v2"


class TestRotationWindow:
    def test_a_token_from_the_previous_key_still_verifies(self, monkeypatch) -> None:
        monkeypatch.setattr(security, "get_settings", before_rotation)
        token, claims = security.create_access_token(uuid.uuid4())

        monkeypatch.setattr(security, "get_settings", after_rotation)
        assert security.decode_token(token, "access").subject == claims.subject

    def test_a_previous_refresh_token_still_verifies(self, monkeypatch) -> None:
        monkeypatch.setattr(security, "get_settings", before_rotation)
        token, claims = security.create_refresh_token(uuid.uuid4())

        monkeypatch.setattr(security, "get_settings", after_rotation)
        assert security.decode_token(token, "refresh").jti == claims.jti

    def test_tokens_without_a_kid_still_verify(self, monkeypatch) -> None:
        """Tokens minted before this change carry no header; the upgrade is not a logout."""
        monkeypatch.setattr(security, "get_settings", after_rotation)
        legacy = jwt.encode(payload(), OLD_ACCESS, algorithm="HS256")
        assert security.decode_token(legacy, "access").token_type == "access"

    def test_after_the_window_only_the_current_key_works(self, monkeypatch) -> None:
        monkeypatch.setattr(security, "get_settings", before_rotation)
        stale, _ = security.create_access_token(uuid.uuid4())

        rotated = build(
            jwt_secret=SecretStr(NEW_ACCESS),
            jwt_refresh_secret=SecretStr(NEW_REFRESH),
            jwt_key_id="v2",
        )
        monkeypatch.setattr(security, "get_settings", lambda: rotated)
        with pytest.raises(UnauthorizedError) as caught:
            security.decode_token(stale, "access")
        assert caught.value.code == ErrorCode.AUTH_TOKEN_INVALID


class TestRejections:
    def test_an_unpublished_kid_is_rejected(self, monkeypatch) -> None:
        monkeypatch.setattr(security, "get_settings", after_rotation)
        forged = jwt.encode(payload(), NEW_ACCESS, algorithm="HS256", headers={"kid": "v9"})
        with pytest.raises(UnauthorizedError) as caught:
            security.decode_token(forged, "access")
        assert caught.value.code == ErrorCode.AUTH_TOKEN_INVALID

    def test_a_previous_key_claiming_the_current_kid_is_rejected(self, monkeypatch) -> None:
        monkeypatch.setattr(security, "get_settings", after_rotation)
        wrong = jwt.encode(payload(), OLD_ACCESS, algorithm="HS256", headers={"kid": "v2"})
        with pytest.raises(UnauthorizedError) as caught:
            security.decode_token(wrong, "access")
        assert caught.value.code == ErrorCode.AUTH_TOKEN_INVALID

    def test_an_expired_previous_token_reports_expiry(self, monkeypatch) -> None:
        monkeypatch.setattr(security, "get_settings", after_rotation)
        expired = payload(exp=int((utc_now() - timedelta(minutes=1)).timestamp()))
        token = jwt.encode(expired, OLD_ACCESS, algorithm="HS256", headers={"kid": "v1"})
        with pytest.raises(UnauthorizedError) as caught:
            security.decode_token(token, "access")
        assert caught.value.code == ErrorCode.AUTH_TOKEN_EXPIRED


class TestConfigGuardrails:
    def test_previous_key_without_its_secret_is_a_boot_problem(self) -> None:
        settings = build(jwt_previous_key_id="v1")
        assert any("JWT_PREVIOUS_SECRET" in problem for problem in settings.validate_environment())

    def test_previous_key_equal_to_current_is_rejected(self) -> None:
        settings = build(
            jwt_key_id="v1",
            jwt_previous_key_id="v1",
            jwt_previous_secret=SecretStr(OLD_ACCESS),
            jwt_previous_refresh_secret=SecretStr(OLD_REFRESH),
        )
        assert any("must differ" in problem for problem in settings.validate_environment())

    def test_a_full_rotation_pair_is_accepted(self) -> None:
        assert after_rotation().validate_environment() == []
