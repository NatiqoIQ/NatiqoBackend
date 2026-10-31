"""Composition root.

The **only** place in the codebase that knows which concrete adapter satisfies which
port. Everything else depends on the port. This is what makes "swap Redis for
something else" and "run with no Redis at all" a single-file change instead of a
refactor.

Adapters are cached per process (``lru_cache``) so a client pool is created once, not
once per request. :func:`reset_composition_root` clears them for tests.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator, Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings, get_settings
from app.core.database import make_read_uow, make_uow
from app.core.errors import RateLimitedError
from app.core.events import InProcessEventBus
from app.core.logging import get_logger
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.admin.application.content_service import AdminContentService
from app.modules.admin.application.stats_service import AdminStatsService
from app.modules.admin.application.users_service import AdminUserService
from app.modules.ai.application.tutor_service import AiTutorService
from app.modules.content.application.catalogue_service import CatalogueService, tree_cache_key
from app.modules.gamification.application.handlers import build_gamification_bus
from app.modules.gamification.application.service import GamificationService
from app.modules.media.application.service import MediaService
from app.modules.quiz.application.progress_service import ProgressService
from app.modules.quiz.application.quiz_service import QuizService
from app.modules.quiz.application.review_service import ReviewService
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


@lru_cache(maxsize=1)
def _rate_limit_fallback() -> CachePort:
    """Counters kept in this process while the shared cache is unreachable.

    Not a second source of truth: it exists only so a Redis blip degrades the limiter
    from exact to approximate instead of turning every limited route into a 500.
    """
    from app.platform.cache.memory import InMemoryCache

    return InMemoryCache()


# ---------------------------------------------------------------------- queue
@lru_cache(maxsize=1)
def _build_queue() -> QueuePort:
    settings = get_settings()
    if settings.uses_redis and settings.queue_enabled:
        from app.platform.queue.redis import RedisQueue

        logger.info("deps.queue_adapter", adapter="redis")
        #: Jobs live in their own instance when one is configured: the cache can evict
        #: under memory pressure, a queued job must not be evicted with it.
        return RedisQueue.from_url(settings.queue_redis_url or settings.redis_url or "")
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


async def read_uow_dep() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    """Request-scoped unit of work on the replica, when one is configured.

    Reserved for reads that tolerate replication lag. Learner state is deliberately not
    routed here: seeing your own lesson as unfinished a second after you finished it is a
    bug users report, not a scaling win.
    """
    uow = make_read_uow()
    await uow.__aenter__()
    exc_info: tuple[type[BaseException] | None, BaseException | None, object] = (None, None, None)
    try:
        yield uow
    except BaseException as exc:
        exc_info = (type(exc), exc, exc.__traceback__)
        raise
    finally:
        await uow.__aexit__(*exc_info)


ReadUowDep = Annotated[SqlAlchemyUnitOfWork, Depends(read_uow_dep)]


# ------------------------------------------------------------------- event bus
def get_bus(uow: UowDep, settings: SettingsDep) -> InProcessEventBus:
    """Build a request-scoped event bus bound to **this request's** unit of work.

    A bus per request, not a process singleton, is the whole point: the gamification
    subscriber writes the stats it maintains through the same session that emits the event,
    so a lesson completion and the XP, streak and achievements it earned share one
    transaction and commit or roll back together. A process-wide bus would hand the
    subscriber a different session and break that guarantee.

    FastAPI caches ``uow_dep`` for the request, so the unit of work handed to the bus is the
    same instance the emitting service uses — not a second one.
    """
    return build_gamification_bus(uow, settings)


EventBusDep = Annotated[InProcessEventBus, Depends(get_bus)]


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


# ------------------------------------------------------------------ ai tutor
def get_ai_tutor_service(uow: UowDep, settings: SettingsDep) -> AiTutorService:
    """Build a request-scoped :class:`AiTutorService`.

    Bound to the request's unit of work, so a stored thread and the AI usage it produced
    share one transaction. It composes three ports: the persistence layer for threads, the
    platform :class:`AIService` for phrasing, and the same budget the platform enforces, so
    the quota the UI reads is the quota the provider respects.
    """
    from app.modules.ai.infrastructure.budget import budget_for

    return AiTutorService(uow, get_ai_service(uow), budget_for(uow.session, settings))


AiTutorServiceDep = Annotated[AiTutorService, Depends(get_ai_tutor_service)]


# ------------------------------------------------------------------- catalogue
def get_catalogue_service(
    uow: ReadUowDep, cache: CacheDep, settings: SettingsDep
) -> CatalogueService:
    """Build a request-scoped :class:`CatalogueService`.

    Bound to the request's unit of work, like every other service, so the four read
    endpoints share one session and one transaction. It is the one service routed to
    ``ReadUowDep``: the catalogue is identical for every learner, so a replica that is a
    second behind costs nothing, while it takes the heaviest reads off the primary. The shared cache is passed because
    the tree is the one response every cold start downloads: four queries and a full
    nesting pass per launch, for a document that changes only when an editor publishes.
    Its TTL is deliberately short (``CATALOGUE_CACHE_TTL_SECONDS``); a cache failure
    falls through to the database rather than failing the read.
    """
    return CatalogueService(uow, cache, ttl_seconds=settings.catalogue_cache_ttl_seconds)


CatalogueServiceDep = Annotated[CatalogueService, Depends(get_catalogue_service)]


async def invalidate_catalogue_tree(request: Request, cache: CacheDep) -> AsyncIterator[None]:
    """Drop the cached tree after an editorial write.

    The TTL alone would leave an editor staring at the old catalogue for up to 30
    seconds after pressing publish. Hanging this off the admin router rather than every
    service method means a write path added later cannot forget it: any successful
    unsafe request under ``/admin/content`` invalidates the one key.

    Runs after the response, and swallows its own failure: failing to invalidate is a
    staleness bug bounded by the TTL, while a 500 after a successful publish is not.
    """
    yield
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    if not request.url.path.startswith("/api/v1/admin/content"):
        return
    try:
        await cache.delete(tree_cache_key(cache))
    except Exception as exc:  # noqa: BLE001 - TTL is the backstop
        logger.warning("catalogue.cache_invalidate_failed", error=str(exc)[:200])


# ------------------------------------------------------------------------ quiz
def get_quiz_service(uow: UowDep, settings: SettingsDep, bus: EventBusDep) -> QuizService:
    """Build a request-scoped :class:`QuizService`.

    Bound to the request's unit of work, so starting a session, grading an answer and
    completing a lesson each commit as one transaction. It takes the settings because a
    session's lease (``QUIZ_SESSION_TTL_SECONDS``) is configuration, not a constant.

    It also takes the request's event bus, because finishing a lesson publishes
    ``LessonCompleted`` and the gamification subscriber that consumes it must write through
    this same unit of work — otherwise the XP a completion earns could be committed without
    the completion, or vice versa.
    """
    return QuizService(uow, settings, bus=bus)


QuizServiceDep = Annotated[QuizService, Depends(get_quiz_service)]


# ----------------------------------------------------------------- gamification
def get_gamification_service(uow: UowDep, settings: SettingsDep) -> GamificationService:
    """Build a request-scoped :class:`GamificationService`.

    Bound to the request's unit of work like every other service, so a claim or a freeze
    commits as one transaction. Reads project stats from history without persisting, so a
    pure hub read rolls back cleanly through the dependency's ``__aexit__``.
    """
    return GamificationService(uow, settings)


GamificationServiceDep = Annotated[GamificationService, Depends(get_gamification_service)]


# ------------------------------------------------------------------------ admin
def get_admin_content_service(uow: UowDep) -> AdminContentService:
    """Build a request-scoped :class:`AdminContentService`.

    Bound to the request's unit of work so an editorial write and the audit row that records
    it share one transaction — which is the whole reason the audit trail lives in this phase
    rather than as an afterthought: a mutation that committed without its row would leave
    exactly the hole an incident review comes to the trail to fill.
    """
    return AdminContentService(uow)


AdminContentServiceDep = Annotated[AdminContentService, Depends(get_admin_content_service)]


def get_admin_user_service(uow: UowDep) -> AdminUserService:
    """Build a request-scoped :class:`AdminUserService`, bound to the request's unit of work."""
    return AdminUserService(uow)


AdminUserServiceDep = Annotated[AdminUserService, Depends(get_admin_user_service)]


def get_admin_stats_service(uow: UowDep) -> AdminStatsService:
    """Build a request-scoped :class:`AdminStatsService` on the request's unit of work."""
    return AdminStatsService(uow)


AdminStatsServiceDep = Annotated[AdminStatsService, Depends(get_admin_stats_service)]


# ------------------------------------------------------------------------ media
def get_media_service(uow: UowDep, storage: StorageDep, settings: SettingsDep) -> MediaService:
    """Build a request-scoped :class:`MediaService`.

    Composes the three things the upload flow needs and nothing else: the request's unit of
    work for the asset row, the process-cached storage port for the grants and the object
    itself, and the settings for the size ceilings. The storage adapter is cached per process
    (it owns a client pool), while the service is bound to the request's session so a confirm
    and the row it writes commit together.
    """
    return MediaService(uow, storage, settings)


MediaServiceDep = Annotated[MediaService, Depends(get_media_service)]


# -------------------------------------------------------------------- progress
def get_progress_service(uow: UowDep) -> ProgressService:
    """Build a request-scoped :class:`ProgressService`.

    Bound to the request's unit of work like every other service. It is read-only, so it
    never commits: the session is opened and rolled back by the dependency's ``__aexit__``,
    which is exactly what a pure read should do.
    """
    return ProgressService(uow)


ProgressServiceDep = Annotated[ProgressService, Depends(get_progress_service)]


# --------------------------------------------------------------------- review
def get_review_service(uow: UowDep) -> ReviewService:
    """Build a request-scoped :class:`ReviewService`.

    Bound to the request's unit of work like every other service, so answering a card
    advances the schedule, appends the log and commits as one transaction.
    """
    return ReviewService(uow)


ReviewServiceDep = Annotated[ReviewService, Depends(get_review_service)]


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


def rate_limit_principal(request: Request) -> str:
    """Who owns the budget: a user, a token, or — last resort — an IP address.

    Keying the limiter on the IP alone breaks the moment a carrier puts a city behind
    one NAT address: every subscriber shares one 1,200/minute allowance, so ordinary
    browsing starts returning 429 for people who did nothing wrong. The ladder is:

    * ``u:<id>`` when an earlier dependency already resolved the caller;
    * ``t:<sha256(access token)>`` otherwise — stable for the token's lifetime, and it
      cannot be spoofed, because a forged token never reaches this dependency (auth
      rejects it first). The digest, not the token, so the key is safe to log;
    * ``ip:<address>`` for anonymous traffic (the catalogue before sign-in, login).

    A caller cannot promote itself: the header is only read after the token has been
    accepted, and an unauthenticated request has no other identity to claim.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"u:{user_id}"
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        digest = hashlib.sha256(token.strip().encode()).hexdigest()[:32]
        return f"t:{digest}"
    return f"ip:{client_ip(request)}"


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
                "quiz": settings.rate_limit_quiz_requests_per_minute,
                "progress": settings.rate_limit_progress_requests_per_minute,
                "review": settings.rate_limit_review_requests_per_minute,
                "ai": settings.rate_limit_ai_requests_per_minute,
                "gamification": settings.rate_limit_gamification_requests_per_minute,
                "admin": settings.rate_limit_admin_requests_per_minute,
                "media": settings.rate_limit_media_requests_per_minute,
            }.get(scope, settings.rate_limit_requests_per_minute)
        # The window key is derived server-side from the clock, never from a header.
        window = int(time.time() // 60)
        key = cache.build_key("ratelimit", scope, rate_limit_principal(request), str(window))
        try:
            current = await cache.increment(key, amount=1, ttl_seconds=120)
        except Exception as exc:  # noqa: BLE001 - Redis is not on the request's critical path
            # A cache outage must not turn into an API outage. Count in this process
            # instead: the allowance is approximate (once per replica) but the endpoint
            # keeps serving, which is strictly better than a 500 for every caller.
            logger.warning("ratelimit.cache_unavailable", scope=scope, error=str(exc)[:200])
            try:
                current = await _rate_limit_fallback().increment(key, amount=1, ttl_seconds=120)
            except Exception:  # noqa: BLE001 - last resort: fail open
                logger.warning("ratelimit.failed_open", scope=scope)
                return
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

#: Budget for the attempt engine. Separate from the catalogue because grading is a write
#: burst (one per question) while browsing is reads, and a learner mid-lesson should not be
#: able to exhaust the allowance that browsing the next lesson needs.
QuizRateLimitDep = Annotated[None, Depends(rate_limit(scope="quiz"))]

#: Budget for the progress read surface. Separate from the quiz write budget so a client
#: refreshing a summary after every completed lesson does not eat into the answer-grading
#: allowance, and vice versa.
ProgressRateLimitDep = Annotated[None, Depends(rate_limit(scope="progress"))]

#: Budget for the review surface. Separate from the quiz write budget so a learner drilling a
#: due queue does not spend the allowance that the next lesson's answer burst needs, and vice
#: versa.
ReviewRateLimitDep = Annotated[None, Depends(rate_limit(scope="review"))]

#: Budget for the AI surface. The tightest scope in the app because a request here can trigger
#: an upstream, billable call — a runaway client burns money rather than just bandwidth. It is
#: also the only budget besides auth that a single endpoint can exhaust on its own.
AiRateLimitDep = Annotated[None, Depends(rate_limit(scope="ai"))]

#: Budget for the gamification surface. Separate from the progress read budget because the
#: hub, achievements and streak screens are their own client story — a learner pulling to
#: refresh a badge list should not spend the allowance that a progress summary needs, and
#: claiming or freezing (the two writes here) should not spend the quiz answer budget.
GamificationRateLimitDep = Annotated[None, Depends(rate_limit(scope="gamification"))]

#: Budget for the admin surface. Its own scope because it is staff tooling rather than a
#: learner path: a batch job editing the catalogue should not be able to consume the
#: allowance a learner's app needs, and vice versa.
AdminRateLimitDep = Annotated[None, Depends(rate_limit(scope="admin"))]

#: Budget for the media surface. Its own scope because it is a learner path rather than staff
#: tooling, and because presign and confirm bracket a real upload: a client retrying a flaky
#: PUT should not be able to spend an editor's allowance, nor an editor a learner's.
MediaRateLimitDep = Annotated[None, Depends(rate_limit(scope="media"))]

#: Budget for the credential-adjacent auth routes. Separate from the default so that a
#: burst of "resend verification" taps cannot consume the allowance that login and
#: register share, and so those two cannot be starved by one another.
AuthRateLimitDep = Annotated[None, Depends(rate_limit(scope="auth"))]

#: Budget for the daily-challenge reads and reports under ``/me``. The scope deliberately
#: has no entry in the settings map, so it falls back to the default 120/min budget — the
#: figure the architecture doc quotes for these routes — and a burst past it is a 429
#: carrying ``Retry-After`` like every other scope. Its own scope name (rather than the
#: bare default) keeps the window key distinguishable in the logs: an exhausted
#: "daily_challenge" budget reads as such, not as some anonymous default client.
DailyChallengeRateLimitDep = Annotated[None, Depends(rate_limit(scope="daily_challenge"))]


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
        _rate_limit_fallback,
    ):
        factory.cache_clear()
