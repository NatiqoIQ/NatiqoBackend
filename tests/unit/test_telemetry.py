"""Optional observability wiring (F6).

The important property is the no-op: a deployment with neither backend configured must
not import either SDK. The second is that a configured-but-missing SDK fails at startup
with an actionable message, instead of running with telemetry silently off.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import telemetry
from app.core.config import Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "sentry_dsn": None,
        "otel_enabled": False,
        "otel_exporter_otlp_endpoint": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_nothing_is_initialised_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telemetry, "_init_sentry", lambda _: pytest.fail("sentry called"))
    monkeypatch.setattr(telemetry, "_init_otel", lambda *_: pytest.fail("otel called"))

    telemetry.init_observability(SimpleNamespace(), _settings())


def test_a_configured_dsn_initialises_sentry(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Settings] = []
    monkeypatch.setattr(telemetry, "_init_sentry", called.append)

    telemetry.init_observability(
        SimpleNamespace(), _settings(sentry_dsn="https://key@sentry.example/1")
    )

    assert len(called) == 1


def test_otel_requires_an_endpoint() -> None:
    with pytest.raises(RuntimeError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        telemetry._init_otel(SimpleNamespace(), _settings(otel_enabled=True))


def test_a_missing_sdk_is_a_startup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # makes ``import`` raise

    with pytest.raises(RuntimeError, match="sentry-sdk is not installed"):
        telemetry._init_sentry(_settings(sentry_dsn="https://key@sentry.example/1"))


def test_sentry_receives_the_privacy_and_sampling_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    fake = SimpleNamespace(init=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)

    telemetry._init_sentry(
        _settings(
            sentry_dsn="https://key@sentry.example/1",
            app_env="production",
            sentry_traces_sample_rate=0.05,
        )
    )

    assert calls == [
        {
            "dsn": "https://key@sentry.example/1",
            "environment": "production",
            "traces_sample_rate": 0.05,
            "send_default_pii": False,
        }
    ]
