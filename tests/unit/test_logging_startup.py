"""Startup-path tests.

These exercise code that only runs inside the ASGI lifespan, which is why a plain
`TestClient` request suite never touched it: `configure_logging` is called once at
boot, so a defect in the structlog processor chain is invisible to every other test
and takes the whole server down at startup instead.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog

from app.core.logging import _inject_context, configure_logging, get_logger


@pytest.fixture(autouse=True)
def _restore_structlog() -> Iterator[None]:
    """Leave structlog as it was found, so ordering cannot leak between tests."""
    before = structlog.get_config()
    yield
    structlog.configure(**before)


@pytest.mark.parametrize("log_format", ["console", "json"])
def test_configured_logging_can_actually_emit(log_format: str) -> None:
    """The processor chain must be executable, not merely constructible.

    A mismatch between the factory and the processors fails only when a line is
    rendered, so the assertion is that a real call returns instead of raising.
    """
    configure_logging("INFO", log_format)

    for name in ("app", "app.modules.users"):
        assert get_logger(name).info("startup_probe", probe="value") is None


def test_logger_name_reaches_the_rendered_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`add_logger_name` is only honoured when a stdlib logger is behind structlog."""
    configure_logging("INFO", "json")
    get_logger("users.auth").info("named_line")

    assert '"logger": "users.auth"' in capsys.readouterr().out


def test_context_is_injected_into_every_line(capsys: pytest.CaptureFixture[str]) -> None:
    from app.core.logging import request_id_var as rid_var, user_id_var

    configure_logging("INFO", "json")
    rid_var.set("req-123")
    user_id_var.set("usr-456")
    try:
        get_logger("app").info("context_line")
    finally:
        rid_var.set(None)
        user_id_var.set(None)

    out = capsys.readouterr().out
    assert '"request_id": "req-123"' in out
    assert '"user_id": "usr-456"' in out


def test_configure_logging_is_idempotent() -> None:
    """Startup calls it once, but tests call it repeatedly; it must not accumulate."""
    configure_logging("INFO", "json")
    first = len(structlog.get_config()["processors"])
    configure_logging("INFO", "json")
    assert len(structlog.get_config()["processors"]) == first


def test_inject_context_is_a_processor() -> None:
    assert callable(_inject_context)
