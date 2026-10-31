"""Optional Sentry and OpenTelemetry wiring (F6).

``/metrics`` answers "how much and how slow"; traces answer "where inside this request",
and Sentry answers "which exception, with which stack, for which user count". Both SDKs are
optional: a deployment that does not install them must boot exactly as before, and the
metrics endpoint keeps working. A deployment that *configures* one but did not install it
fails at startup instead of silently running blind.

The SDKs are imported inside the initialisers on purpose. Importing ``sentry_sdk`` at
module import costs startup time even when it is disabled, and it would make the test suite
depend on an SDK it does not exercise.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger("core.telemetry")

__all__ = ["init_observability"]


def init_observability(app: Any, settings: Settings) -> None:
    """Initialise whichever backends the environment configured.

    Called once from :func:`app.main.create_app`, after routers are mounted: both FastAPI
    integrations wrap the app object, and wrapping before routes exist would miss them.
    """
    if settings.sentry_dsn:
        _init_sentry(settings)
    if settings.otel_enabled:
        _init_otel(app, settings)


def _init_sentry(settings: Settings) -> None:
    try:
        import sentry_sdk
    except ImportError as exc:  # pragma: no cover - exercised by configuration, not tests
        raise RuntimeError(
            "SENTRY_DSN is set but sentry-sdk is not installed; "
            "install sentry-sdk[fastapi] or unset SENTRY_DSN"
        ) from exc

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        #: Error sampling is not a thing Sentry does; this is the share of transactions
        #: that become performance traces. Five percent is enough to see p95 shape.
        traces_sample_rate=settings.sentry_traces_sample_rate,
        #: Request bodies can carry learner text and AI prompts. Attaching them by default
        #: would ship content to a third party; errors still carry stack and tags.
        send_default_pii=False,
    )
    logger.info("observability.sentry_enabled", environment=settings.app_env)


def _init_otel(app: Any, settings: Settings) -> None:
    if not settings.otel_exporter_otlp_endpoint:
        raise RuntimeError("OTEL_ENABLED requires OTEL_EXPORTER_OTLP_ENDPOINT")
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:  # pragma: no cover - exercised by configuration, not tests
        raise RuntimeError(
            "OTEL_ENABLED is set but the OpenTelemetry SDK/instrumentation is not installed"
        ) from exc

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name}),
        #: Parent-based keeps a trace sampled end to end: without it the collector can see
        #: a parent but the service silently drops its children.
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sample_rate)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
    )
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")
    #: The engine is created lazily, so instrument the factory rather than one engine.
    SQLAlchemyInstrumentor().instrument()
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    logger.info(
        "observability.otel_enabled",
        service_name=settings.otel_service_name,
        sample_ratio=settings.otel_traces_sample_rate,
    )
