"""Composition root.

The **only** place in the codebase that knows which concrete adapter satisfies which
port. Everything else depends on the port. This is what makes "swap Redis for
something else" and "run with no Redis at all" a single-file change instead of a
refactor.

Adapters are cached per process (``lru_cache``) so a client pool is created once, not
once per request. :func:`reset_composition_root` clears them for tests.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.database import make_uow
from app.core.errors import RateLimitedError
from app.core.events import InProcessEventBus, get_event_bus
from app.core.logging import get_logger
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.content.application.catalogue_service import CatalogueService
from app.modules.users.domain.identity import (
    IdentityProvider,
    IdentityVerifierPort,
)
from app.platform.ai.port import AIProvider
from app.platform.ai.service import AIService
from app.platform.cache.port import CachePort
from app.platform.push.port import PushPort
from app.platform.queue.port import QueuePort
from app.platform.storage.port import ObjectStoragePort

logger = get_logger("deps")


# ------------------------------------------------------------------- settings
def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


# ---------------------------------------------------------------------- cache
@lru_cache(maxsize=1)
def _build_cache() -> CachePort:
    settings = get_settings()
    if settings.uses_redis:
        from app.platform.cache.redis import RedisCache

        logger.info("deps.cache_adapter", adapter="redis")
        return RedisCache.from_url(settings.redis_url or "", namespace="natiqo")
    logger.info("deps.cache_adapter", adapter="memory")
    from app.platform.cache.memory import InMemoryCache

    return InMemoryCache()


def get_cache() -> CachePort:
    return _build_cache()


CacheDep = Annotated[CachePort, Depends(get_cache)]


# ---------------------------------------------------------------------- queue
@lru_cache(maxsize=1)
def _build_queue() -> QueuePort:
    settings = get_settings()
    if settings.uses_redis and settings.queue_enabled:
        from app.platform.queue.redis import RedisQueue

        logger.info("deps.queue_adapter", adapter="redis")
        return RedisQueue.from_url(settings.redis_url or "")
    logger.info("deps.queue_adapter", adapter="memory")
    from app.platform.queue.memory import InMemoryQueue

    return InMemoryQueue()


def get_queue() -> QueuePort:
    return _build_queue()


QueueDep = Annotated[QueuePort, Depends(get_queue)]


# -------------------------------------------------------------------- storage
@lru_cache(maxsize=1)
def _build_storage() -> ObjectStoragePort:
    settings = get_settings()
    if settings.uses_s3:
        from app.platform.storage.s3 import S3ObjectStorage

        logger.info("deps.storage_adapter", adapter="s3")
        return S3ObjectStorage.from_settings()
    logger.info("deps.storage_adapter", adapter="local")
    from app.platform.storage.local import LocalObjectStorage

    return LocalObjectStorage()


def get_storage() -> ObjectStoragePort:
    return _build_storage()


StorageDep = Annotated[ObjectStoragePort, Depends(get_storage)]


# ------------------------------------------------------------------------ push
@lru_cache(maxsize=1)
def _build_push() -> PushPort:
    settings = get_settings()
    if settings.uses_fcm:
        from app.platform.push.fcm import FcmPush

        try:
            adapter = FcmPush.from_settings()
            logger.info("deps.push_adapter", adapter="fcm")
            return adapter
        except Exception as exc:  # noqa: BLE001 - notifications must never block boot
            logger.warning("deps.push_fcm_unavailable", error=str(exc)[:200])
    logger.info("deps.push_adapter", adapter="logging")
    from app.platform.push.logging_push import LoggingPush

    return LoggingPush()


def get_push() -> PushPort:
    return _build_push()


PushDep = Annotated[PushPort, Depends(get_push)]


# ------------------------------------------------------------------ identity
@lru_cache(maxsize=1)
def _build_identity_verifiers() -> dict[IdentityProvider, IdentityVerifierPort]:
    """Provider verifiers, built once per process.

    Process-cached on purpose: each verifier owns a JWK-set cache, and a per-request
    verifier would refetch the provider's keys on every single sign-in — turning a
    cached local check into a network round trip to Google or Apple.
    """
    from app.platform.identity.verifiers import build_verifiers

    verifiers = build_verifiers(get_settings())
    logger.info(
        "deps.identity_verifiers",
        providers=sorted(str(provider) for provider in verifiers),
    )
    return verifiers


def get_identity_verifiers() -> dict[IdentityProvider, IdentityVerifierPort]:
    return _build_identity_verifiers()


IdentityVerifiersDep = Annotated[
    dict[IdentityProvider, IdentityVerifierPort], Depends(get_identity_verifiers)
]


# ------------------------------------------------------------------- database
async def uow_dep() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    """Request-scoped unit of work.

    The dependency owns the session lifecycle so no route can leak one. Handlers
    commit explicitly through the use case; leaving the request without a commit
    rolls back, so a mid-handler failure cannot leave partial writes behind.

    The exception is forwarded to ``__aexit__`` so the rollback decision is made in
    exactly one place; doing it here as well would issue a second rollback.
    """
    uow = make_uow()
    await uow.__aenter__()
    exc_info: tuple[type[BaseException] | None, BaseException | None, object] = (None, None, None)
    try:
        yield uow
    except BaseException as exc:
        exc_info = (type(exc), exc, exc.__traceback__)
        raise
    finally:
        await uow.__aexit__(*exc_info)


UowDep = Annotated[SqlAlchemyUnitOfWork, Depends(uow_dep)]


# --------------------------------------------------------------------------- ai
@lru_cache(maxsize=1)
def _build_ai_provider() -> AIProvider:
    from app.platform.ai.providers import AnthropicProvider, DisabledProvider, OpenAIProvider

    settings = get_settings()
    if not settings.ai_enabled or settings.ai_provider == "none" or not settings.ai_api_key:
        logger.info("deps.ai_provider", provider="none")
        return DisabledProvider()
    try:
        if settings.ai_provider == "openai":
            provider: AIProvider = OpenAIProvider.from_settings()
        else:
            provider = AnthropicProvider.from_settings()
    except Exception as exc:  # noqa: BLE001 - AI must never block boot
        logger.warning("deps.ai_provider_unavailable", error=str(exc)[:200])
        return DisabledProvider()
    logger.info("deps.ai_provider", provider=str(provider.name))
    return provider


def get_ai_service(uow: UowDep) -> AIService:
    """Build a request-scoped :class:`AIService`.

    The provider is process-cached (it owns an HTTP client pool), but the service is
    not: its budget reads the caller's own transaction, so it must be bound to the
    request's session. When ``AI_DAILY_USER_BUDGET_USD`` is 0 the budget is unmetered.
    """
    from app.modules.ai.infrastructure.budget import budget_for
    from app.modules.ai.infrastructure.usage import record_ai_usage

    return AIService(
        _build_ai_provider(),
        usage_recorder=record_ai_usage,
        budget=budget_for(uow.session, get_settings()),
    )


AIServiceDep = Annotated[AIService, Depends(get_ai_service)]


# ------------------------------------------------------------------- catalogue
def get_catalogue_service(uow: UowDep) -> CatalogueService:
    """Build a request-scoped :class:`CatalogueService`.

    Bound to the request's unit of work, like every other service, so the four read
    endpoints share one session and one transaction. It takes no cache: the catalogue is
    public data that changes only when an editor publishes, and an HTTP cache header is
    the right place for that decision, not a process-wide ``lru_cache`` that would keep
    serving a retired level to the next process to start.
    """
    return CatalogueService(uow)


CatalogueServiceDep = Annotated[CatalogueService, Depends(get_catalogue_service)]


# ------------------------------------------------------------------- event bus
def get_bus() -> InProcessEventBus:
    return get_event_bus()


EventBusDep = Annotated[InProcessEventBus, Depends(get_bus)]


# ----------------------------------------------------------------- rate limit
def client_ip(request: Request) -> str:
    """Best-effort client identity for rate limiting.

    ``X-Forwarded-For`` is only honoured when the app is behind a proxy we control;
    otherwise a client could spoof the header to dodge its own budget.
    """
    settings = get_settings()
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    if request.client:
        return request.client.host[:64]
    return "unknown"


def rate_limit(scope: str = "default", limit: int | None = None) -> Callable[..., object]:
    """Build a FastAPI dependency enforcing a per-client request budget.

    A route opts in by taking the returned dependency. Nothing is limited by default: a
    dependency that no route declares is a rule that is never enforced, so the wiring is
    explicit at the endpoint rather than a middleware nobody can see.

    ``scope`` picks the budget, so a public read path cannot spend the same allowance as
    the write path that shares a client. Login is scoped separately because a credential
    stuffing attempt must not be able to exhaust an ordinary user's budget.
    """

    async def _dependency(request: Request, cache: CacheDep, settings: SettingsDep) -> None:
        if not settings.rate_limit_enabled:
            return
        budget = limit
        if budget is None:
            budget = {
                "auth": settings.rate_limit_auth_requests_per_minute,
                "catalogue": settings.rate_limit_catalogue_requests_per_minute,
            }.get(scope, settings.rate_limit_requests_per_minute)
        # The window key is derived server-side from the clock, never from a header.
        window = int(time.time() // 60)
        key = cache.build_key("ratelimit", scope, client_ip(request), str(window))
        current = await cache.increment(key, amount=1, ttl_seconds=120)
        if current > budget:
            logger.warning("ratelimit.exceeded", scope=scope, limit=budget)
            raise RateLimitedError(
                "Too many requests. Please slow down and try again shortly.",
                retry_after=60,
            )

    return _dependency


#: Budget for the public catalogue. Declared as one alias rather than repeated per route so
#: the four read endpoints cannot drift apart, and so changing the scope is a single edit.
#: It is separate from the default budget because a learner scrolling the curriculum should
#: not be able to spend the allowance of an authenticated write happening on the same
#: device.
CatalogueRateLimitDep = Annotated[None, Depends(rate_limit(scope="catalogue"))]

#: Budget for the credential-adjacent auth routes. Separate from the default so that a
#: burst of "resend verification" taps cannot consume the allowance that login and
#: register share, and so those two cannot be starved by one another.
AuthRateLimitDep = Annotated[None, Depends(rate_limit(scope="auth"))]


RateLimitDep = Callable[..., object]


# ------------------------------------------------------------------- test hook
def reset_composition_root() -> None:
    """Drop every cached adapter so a test starts from a clean process state."""
    for factory in (
        _build_cache,
        _build_queue,
        _build_storage,
        _build_push,
        _build_ai_provider,
        _build_identity_verifiers,
    ):
        factory.cache_clear()
