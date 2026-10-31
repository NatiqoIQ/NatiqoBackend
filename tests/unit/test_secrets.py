"""Secret-manager bootstrap (F23).

The loader is optional and must be invisible when ``SECRETS_BACKEND`` is unset. These
tests cover the failure that matters: a malformed or non-object secret must stop startup
with a clear message, not half-populate the environment.
"""

from __future__ import annotations

import json
import os

import pytest

from app.core import secrets


def test_payload_must_be_a_json_object() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        secrets.parse_secret_payload(json.dumps(["DATABASE_URL"]))
    with pytest.raises(ValueError, match="not valid JSON"):
        secrets.parse_secret_payload("DATABASE_URL=postgres://")


def test_values_must_be_strings_and_keys_must_look_like_env_names() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        secrets.parse_secret_payload(json.dumps({"DATABASE_URL": 42}))
    with pytest.raises(ValueError, match="environment variable name"):
        secrets.parse_secret_payload(json.dumps({"jwt.secret": "x"}))


def test_applying_a_payload_overrides_stale_environment_values() -> None:
    environ = {"DATABASE_URL": "postgres://stale", "PATH": "/usr/bin"}
    keys = secrets.apply_secret_payload({"DATABASE_URL": "postgres://new"}, environ=environ)

    assert keys == ["DATABASE_URL"]
    assert environ["DATABASE_URL"] == "postgres://new"
    assert environ["PATH"] == "/usr/bin"


def test_the_backend_is_a_no_op_unless_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRETS_BACKEND", raising=False)
    monkeypatch.setattr(secrets, "load_aws_secret", lambda *a, **k: pytest.fail("called"))
    secrets.bootstrap_secrets()


def test_the_aws_backend_applies_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets.reset_secrets_cache()
    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    monkeypatch.setenv("AWS_SECRET_ID", "natiqo/prod")
    monkeypatch.setenv("AWS_REGION", "me-central-1")
    monkeypatch.setenv("DATABASE_URL", "postgres://stale")
    monkeypatch.setattr(
        secrets,
        "load_aws_secret",
        lambda secret_id, region: {"DATABASE_URL": f"postgres://from-{secret_id}-{region}"},
    )

    secrets.bootstrap_secrets()

    assert os.environ["DATABASE_URL"] == "postgres://from-natiqo/prod-me-central-1"
    secrets.reset_secrets_cache()


def test_missing_secret_id_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets.reset_secrets_cache()
    monkeypatch.setenv("SECRETS_BACKEND", "aws")
    monkeypatch.delenv("AWS_SECRET_ID", raising=False)

    with pytest.raises(RuntimeError, match="AWS_SECRET_ID"):
        secrets.bootstrap_secrets()
    secrets.reset_secrets_cache()
