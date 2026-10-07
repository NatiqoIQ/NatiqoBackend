"""Structured logging.

Design rules:
- Every log line is JSON in production and a readable console format in development.
- The logging context carries ``request_id`` and, when known, ``user_id`` so any
  line can be correlated with a request without log parsing heuristics.
- Secrets are never logged. ``REDACTED_KEYS`` scrubs anything that looks like a
  credential even if a caller passes it by mistake.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

from app.core.config import get_settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)

REDACTED = "***redacted***"
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        # Kept even though the application stores no credential any more: a value bound
        # to this key in some future log line is the one leak that would matter most.
        "password",
        "password_hash",
        # The provider ID token is a bearer credential until it has been verified, so the
        # verifier's own log lines must never be able to reproduce one.
        "id_token",
        "identity_token",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "secret",
        "api_key",
        "apikey",
        "jwt_secret",
        "jwt_refresh_secret",
        "client_secret",
        "payment_api_key",
        "payment_webhook_secret",
        "s3_secret_access_key",
        "s3_access_key_id",
        "fcm_credentials_json",
        "ai_api_key",
        "dsn",
        "sentry_dsn",
        "credit_card",
        "card_number",
        "cvv",
    }
)


def _inject_context(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: attach correlation ids and scrub secrets."""
    request_id = request_id_var.get()
    if request_id and "request_id" not in event_dict:
        event_dict["request_id"] = request_id
    user_id = user_id_var.get()
    if user_id and "user_id" not in event_dict:
        event_dict["user_id"] = user_id
    return redact(event_dict)


def redact(event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if key.lower() in REDACTED_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def new_request_id() -> str:
    return uuid.uuid4().hex


def configure_logging(level_name: str | None = None, log_format: str | None = None) -> None:
    """Idempotently configure structlog + stdlib logging. Called once at startup."""
    settings = get_settings()
    level = getattr(logging, level_name or settings.log_level, logging.INFO)
    renderer_name = log_format or settings.log_format

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    for noisy in ("uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _inject_context,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any
    if renderer_name == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    else:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # The stdlib factory, not PrintLoggerFactory: `add_logger_name` and
        # `add_log_level` in the chain above read attributes that only a stdlib
        # logger has. Pairing them with a PrintLogger makes *every* log call raise
        # AttributeError, which surfaces as a crash during startup.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
