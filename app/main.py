"""FastAPI application factory.

Lifespan ordering matters:
1. **Startup** validates configuration first. A process that would refuse to serve
   should exit before it opens a database pool or accepts a request.
2. **Shutdown** disposes the engine and flushes telemetry, in that order, so in-flight
   work still has a usable session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import configure_mappers

from app.api.deps import get_cache
from app.api.v1.router import api_router, dev_login_router
from app.api.v1.routes.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.database import dispose_engine
from app.core.exceptions import ERROR_RESPONSES, register_exception_handlers
from app.core.idempotency import IdempotencyMiddleware
from app.core.logging import configure_logging, get_logger
from app.core.middleware import register_middleware
from app.core.registry import import_all_models


async def drain_ai_usage() -> None:
    """Flush pending AI usage writes before the engine is disposed."""
    from app.modules.ai.infrastructure.usage import drain

    await drain()


logger = get_logger("app")

DESCRIPTION = """
Backend API for **Natiqo**, an English-learning app for Iraqi learners.

- Content is British English, delivered in English, Modern Standard Arabic or Iraqi Arabic.
- Calendar rules (streaks, daily goals, quiet hours) follow the learner's timezone.
- Every error response carries a stable `code` and a `request_id`.
"""


def _check_config(settings: Settings) -> None:
    problems = settings.validate_environment()
    if problems:
        for problem in problems:
            logger.error("config.invalid", problem=problem)
        raise RuntimeError("Refusing to start with an unsafe configuration: " + "; ".join(problems))
    if settings.uses_redis:
        logger.info("config.redis_configured")
    else:
        logger.warning(
            "config.using_in_memory_adapters",
            detail="Cache and queue are in-process. Set REDIS_URL before production.",
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _check_config(settings)
    configure_logging(settings.log_level, settings.log_format)
    # Register every mapper before serving. A malformed relationship or a foreign key
    # pointing at an unregistered table then fails at boot, not on the first request.
    import_all_models()
    configure_mappers()
    logger.info(
        "app.started",
        environment=settings.app_env,
        ai_enabled=settings.ai_enabled,
        api_prefix=settings.api_v1_prefix,
    )
    try:
        yield
    finally:
        await drain_ai_usage()
        await dispose_engine()
        logger.info("app.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    app = FastAPI(
        title=app_settings.app_name,
        description=DESCRIPTION,
        version="0.1.1",
        docs_url="/docs" if not app_settings.is_production or app_settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json"
        if not app_settings.is_production or app_settings.docs_enabled
        else None,
        root_path=app_settings.root_path,
        lifespan=lifespan,
    )

    if app_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app_settings.cors_origins,
            allow_credentials=app_settings.cors_allow_credentials,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Idempotency-Key"],
            # ``ETag`` and ``Cache-Control`` are exposed because revalidation is invisible
            # without them: a browser will not let script read a header that is not on this
            # list, so an unexposed ETag cannot be stored and therefore cannot be sent back
            # as ``If-None-Match``. The whole conditional-request path would be unreachable
            # from the web and webview builds while working only for native clients that read
            # headers directly.
            expose_headers=[
                "ETag",
                "Cache-Control",
                "X-Request-ID",
                "Retry-After",
                "Content-Encoding",
                "Content-Length",
            ],
            max_age=3600,
        )
    # 400 rather than Starlette's 1 KiB default, because this API's catalogue pages are
    # small by design: a level list measured 426 bytes and a lesson detail 809, so the 1 KiB
    # default compressed *nothing* and every mobile client paid full price for JSON that
    # shrinks by roughly a third (426 -> 289, 809 -> 505 on the seeded curriculum).
    #
    # The floor is where the fixed 18 bytes of gzip header and trailer stop being dwarfed by
    # the saving. Raise this if catalogue pages grow well past a kilobyte and the per-response
    # CPU is worth more than the bytes — the saving scales, the overhead does not.
    app.add_middleware(GZipMiddleware, minimum_size=400)

    register_middleware(app)
    register_exception_handlers(app)

    # Idempotency is installed outside the middleware stack (below) because it needs the
    # cache adapter, which the composition root owns. A repeated Idempotency-Key replays
    # the stored response instead of executing the route again.
    app.add_middleware(
        IdempotencyMiddleware,
        cache=get_cache(),
        ttl_seconds=app_settings.idempotency_ttl_seconds,
    )

    # Health probes stay unversioned at the root so orchestrators do not have to track
    # the API prefix. The rest of the API is mounted under the versioned prefix.
    app.include_router(health_router)
    # ``ERROR_RESPONSES`` is attached here rather than per-route. Declaring the error
    # envelope once at the mount point keeps every future route honest by default, and it
    # is also what stops FastAPI from publishing its own ``HTTPValidationError`` body for
    # 422 (see the note on that dict).
    app.include_router(
        api_router,
        prefix=app_settings.api_v1_prefix,
        responses=ERROR_RESPONSES,
    )
    # The development sign-in is mounted here rather than on ``api_router`` because
    # the flag is read from this app's own settings, so the route can be omitted from
    # the router and the OpenAPI document entirely — not merely refused at request
    # time. The flag defaults to on in every environment (see ``Settings``); set
    # ``DEV_LOGIN_ENABLED=false`` to keep the route out of the app object.
    if app_settings.dev_login_enabled:
        app.include_router(
            dev_login_router,
            prefix=app_settings.api_v1_prefix,
            responses=ERROR_RESPONSES,
        )

    # Curriculum media is content, not a per-learner upload: it is identical for every
    # client and immutable for a given slug. Serving it same-origin — rather than through
    # a signed URL that would expire mid-lesson — lets a client cache an aid for the life
    # of the app. Mounted only when the directory is present so a checkout without the
    # media tree (or a test app) still boots.
    media_root = Path(app_settings.media_root)
    if media_root.is_dir():
        app.mount("/media", StaticFiles(directory=media_root), name="media")

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": app_settings.app_name, "docs": "/docs", "api": app_settings.api_v1_prefix}

    return app


app = create_app()
