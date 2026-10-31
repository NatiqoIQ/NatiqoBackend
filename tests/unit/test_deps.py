"""Tests for the composition root and the request-scoped dependencies.

Two things are asserted here that are otherwise only observable in production:

* the **fallback ladder** — a missing credential degrades to a no-op adapter rather than
  failing the boot. That is deliberate ("AI must never block boot", "notifications must
  never block boot"), and it is exactly the kind of decision that rots silently: when the
  fallback quietly stops applying, the app still starts and the feature is just dead.
* the **rate-limit identity** — a client must not be able to choose its own budget by
  setting a header.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import cast

import pytest
from starlette.requests import Request

from app.api.deps import (
    _build_ai_provider,
    _build_cache,
    _build_push,
    _build_queue,
    _build_storage,
    client_ip,
    get_ai_service,
    get_cache,
    rate_limit,
    rate_limit_principal,
)
from app.core.config import get_settings, reset_settings_cache
from app.core.errors import RateLimitedError
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork


def make_request(
    *, host: str | None = "203.0.113.9", headers: dict[str, str] | None = None
) -> Request:
    """A real ASGI request, so header lookup and ``client`` behave as in production."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "root_path": "",
            "query_string": b"",
            "headers": [
                (name.lower().encode(), value.encode()) for name, value in (headers or {}).items()
            ],
            "client": (host, 54321) if host else None,
            "server": ("testserver", 80),
        }
    )


class FakeUow:
    """Carries only the session attribute ``get_ai_service`` reads."""

    def __init__(self, session: object) -> None:
        self.session = session


class TestClientIp:
    def test_the_peer_address_is_used_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRUST_FORWARDED_FOR", "false")
        reset_settings_cache()

        assert client_ip(make_request(host="203.0.113.9")) == "203.0.113.9"

    def test_a_forwarded_header_is_ignored_unless_it_is_trusted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise any client could spoof its identity and escape its own budget."""
        monkeypatch.setenv("TRUST_FORWARDED_FOR", "false")
        reset_settings_cache()
        request = make_request(host="203.0.113.9", headers={"X-Forwarded-For": "1.2.3.4"})

        assert client_ip(request) == "203.0.113.9"

    def test_the_leftmost_forwarded_hop_wins_behind_a_trusted_proxy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The proxy appends; the leftmost entry is the only one the client cannot forge."""
        monkeypatch.setenv("TRUST_FORWARDED_FOR", "true")
        reset_settings_cache()
        request = make_request(headers={"X-Forwarded-For": "  198.51.100.7 , 10.0.0.1 "})

        assert client_ip(request) == "198.51.100.7"

    def test_a_forwarded_identity_is_length_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """It becomes part of a cache key, so it must not be able to grow unbounded."""
        monkeypatch.setenv("TRUST_FORWARDED_FOR", "true")
        reset_settings_cache()
        request = make_request(headers={"X-Forwarded-For": "x" * 500})

        assert len(client_ip(request)) == 64

    def test_a_trusted_proxy_with_no_forwarded_header_falls_back_to_the_peer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRUST_FORWARDED_FOR", "true")
        reset_settings_cache()

        assert client_ip(make_request(host="203.0.113.9")) == "203.0.113.9"

    def test_a_request_with_no_peer_at_all_is_still_a_stable_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Must not raise: a request with no peer shares one bucket rather than failing."""
        monkeypatch.setenv("TRUST_FORWARDED_FOR", "true")
        reset_settings_cache()

        assert client_ip(make_request(host=None)) == "unknown"


class TestRateLimitBudgetSelection:
    """Each scope resolves to its own budget, so scopes cannot quietly share one."""

    @pytest.fixture(autouse=True)
    def _limiting_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`conftest` disables rate limiting suite-wide, so these opt back in.

        Every budget assertion needs it, and forgetting it produces a test that passes
        because nothing was ever counted.
        """
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
        reset_settings_cache()

    async def _spend(self, request: Request, dependency: object) -> None:
        await dependency(request=request, cache=_build_cache(), settings=get_settings())  # type: ignore[operator]

    async def test_the_default_scope_uses_the_default_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "2")
        reset_settings_cache()
        dependency = rate_limit()

        await self._spend(make_request(), dependency)
        await self._spend(make_request(), dependency)
        with pytest.raises(RateLimitedError):
            await self._spend(make_request(), dependency)

    async def test_the_catalogue_scope_spends_its_own_allowance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole reason the scope exists: browsing must not starve another path."""
        monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "100")
        monkeypatch.setenv("RATE_LIMIT_CATALOGUE_REQUESTS_PER_MINUTE", "1")
        reset_settings_cache()
        dependency = rate_limit(scope="catalogue")

        await self._spend(make_request(host="198.51.100.1"), dependency)
        with pytest.raises(RateLimitedError):
            await self._spend(make_request(host="198.51.100.1"), dependency)

    async def test_the_auth_scope_spends_its_own_allowance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "100")
        monkeypatch.setenv("RATE_LIMIT_AUTH_REQUESTS_PER_MINUTE", "1")
        reset_settings_cache()
        dependency = rate_limit(scope="auth")

        await self._spend(make_request(host="198.51.100.2"), dependency)
        with pytest.raises(RateLimitedError):
            await self._spend(make_request(host="198.51.100.2"), dependency)

    async def test_an_explicit_limit_overrides_the_configured_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "1000")
        reset_settings_cache()
        dependency = rate_limit(limit=1)

        await self._spend(make_request(host="198.51.100.3"), dependency)
        with pytest.raises(RateLimitedError):
            await self._spend(make_request(host="198.51.100.3"), dependency)

    async def test_budgets_are_counted_per_client_not_globally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One noisy client must not lock everybody else out."""
        monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "1")
        reset_settings_cache()
        dependency = rate_limit()

        await self._spend(make_request(host="198.51.100.4"), dependency)
        await self._spend(make_request(host="198.51.100.5"), dependency)

    async def test_scopes_have_separate_counters_for_the_same_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "1")
        reset_settings_cache()

        await self._spend(make_request(host="198.51.100.6"), rate_limit(scope="auth"))
        await self._spend(make_request(host="198.51.100.6"), rate_limit(scope="default"))

    async def test_limiting_can_be_switched_off_entirely(self) -> None:
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
        monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "1")
        reset_settings_cache()
        try:
            dependency = rate_limit()

            for _ in range(5):
                await self._spend(make_request(), dependency)
        finally:
            monkeypatch.undo()
            reset_settings_cache()

    async def test_a_limited_response_asks_the_client_to_wait_a_minute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "1")
        reset_settings_cache()
        dependency = rate_limit()
        await self._spend(make_request(host="198.51.100.7"), dependency)

        with pytest.raises(RateLimitedError) as caught:
            await self._spend(make_request(host="198.51.100.7"), dependency)

        assert caught.value.retry_after == 60


class TestAdapterSelection:
    def test_the_memory_adapters_are_chosen_without_redis(self) -> None:
        assert type(_build_cache()).__name__ == "InMemoryCache"
        assert type(_build_queue()).__name__ == "InMemoryQueue"
        assert type(_build_storage()).__name__ == "LocalObjectStorage"

    def test_a_configured_queue_falls_back_to_memory_when_it_is_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`REDIS_URL` alone must not switch the queue on — the two are separate switches."""
        monkeypatch.setenv("QUEUE_ENABLED", "false")
        reset_settings_cache()

        assert type(_build_queue()).__name__ == "InMemoryQueue"

    def test_push_degrades_to_logging_when_fcm_is_not_configured(self) -> None:
        assert type(_build_push()).__name__ == "LoggingPush"

    def test_a_broken_fcm_configuration_does_not_stop_the_app_booting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fallback exists so a bad push credential cannot take down every route."""
        monkeypatch.setenv("FCM_PROJECT_ID", "not-a-real-project")
        monkeypatch.setenv("FCM_CREDENTIALS", "not-a-real-credential")
        reset_settings_cache()

        assert type(_build_push()).__name__ == "LoggingPush"


class TestAIProviderSelection:
    def test_ai_is_disabled_by_default(self) -> None:
        provider = _build_ai_provider()

        assert provider.name == "none"

    def test_ai_stays_disabled_without_an_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enabled-but-keyless must not construct a provider that fails on first use."""
        monkeypatch.setenv("AI_ENABLED", "true")
        monkeypatch.setenv("AI_PROVIDER", "anthropic")
        monkeypatch.setenv("AI_API_KEY", "")
        reset_settings_cache()

        assert _build_ai_provider().name == "none"

    def test_a_provider_named_none_is_disabled_even_with_a_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_ENABLED", "true")
        monkeypatch.setenv("AI_PROVIDER", "none")
        monkeypatch.setenv("AI_API_KEY", "sk-test-not-real")
        reset_settings_cache()

        assert _build_ai_provider().name == "none"

    async def test_the_service_is_built_even_when_ai_is_off(
        self, monkeypatch: pytest.MonkeyPatch, session: object
    ) -> None:
        """A dependency that raised here would break routes that never touch AI."""
        monkeypatch.setenv("AI_ENABLED", "false")
        reset_settings_cache()

        uow = cast("SqlAlchemyUnitOfWork", FakeUow(session=session))

        assert get_ai_service(uow, get_cache()) is not None


class TestTrustDefaults:
    def test_forwarded_headers_are_untrusted_unless_configured(self) -> None:
        assert get_settings().trust_forwarded_for is False


async def test_uow_rolls_back_when_a_handler_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dependency owns the session lifecycle, so a failure must not leave a write open."""
    from app.api import deps as deps_module

    closed: list[tuple[object, ...]] = []

    class RecordingUow:
        async def __aenter__(self) -> RecordingUow:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            closed.append(exc_info)

    monkeypatch.setattr(deps_module, "make_uow", RecordingUow)

    generator = cast("AsyncGenerator[None, None]", deps_module.uow_dep())
    await generator.__anext__()
    with pytest.raises(RuntimeError):
        await generator.athrow(RuntimeError("handler blew up"))

    assert len(closed) == 1
    assert closed[0][0] is RuntimeError


async def test_uow_closes_cleanly_when_the_handler_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path must also close, or every successful request leaks a session."""
    from app.api import deps as deps_module

    closed: list[tuple[object, ...]] = []

    class RecordingUow:
        async def __aenter__(self) -> RecordingUow:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            closed.append(exc_info)

    monkeypatch.setattr(deps_module, "make_uow", RecordingUow)

    generator = cast("AsyncGenerator[None, None]", deps_module.uow_dep())
    await generator.__anext__()
    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()

    assert closed == [(None, None, None)]


def test_the_composition_root_is_cleared_between_requests() -> None:
    """A cached adapter from a previous test would silently outlive the settings change."""
    from app.api.deps import reset_composition_root

    _build_cache()
    reset_composition_root()

    assert _build_cache.cache_info().currsize == 0


# =========================================================== rate-limit identity
class TestRateLimitPrincipal:
    """The budget follows the caller, not the NAT address they happen to share."""

    def test_an_authenticated_token_gets_its_own_stable_key(self) -> None:
        first = make_request(headers={"Authorization": "Bearer token-one"})
        same = make_request(headers={"Authorization": "Bearer token-one"})
        other = make_request(headers={"Authorization": "Bearer token-two"})

        assert rate_limit_principal(first) == rate_limit_principal(same)
        assert rate_limit_principal(first) != rate_limit_principal(other)
        assert rate_limit_principal(first).startswith("t:")

    def test_the_raw_token_never_appears_in_the_key(self) -> None:
        """The key is logged and stored in Redis; it must not become a credential leak."""
        token = "super-secret-access-token"
        request = make_request(headers={"Authorization": f"Bearer {token}"})

        assert token not in rate_limit_principal(request)

    def test_a_resolved_user_id_wins_over_the_ip(self) -> None:
        request = make_request(host="198.51.100.9")
        request.state.user_id = "11111111-1111-1111-1111-111111111111"

        assert rate_limit_principal(request).startswith("u:")

    def test_anonymous_traffic_falls_back_to_the_peer_address(self) -> None:
        assert rate_limit_principal(make_request(host="198.51.100.9")) == "ip:198.51.100.9"

    def test_a_non_bearer_authorization_header_is_not_trusted(self) -> None:
        request = make_request(host="198.51.100.9", headers={"Authorization": "Basic abc"})

        assert rate_limit_principal(request) == "ip:198.51.100.9"


class _BrokenCache:
    """A cache whose shared backend is down; only ``increment`` matters here."""

    def build_key(self, *parts: str) -> str:
        return ":".join(parts)

    async def increment(self, key: str, amount: int = 1, ttl_seconds: int | None = None) -> int:
        raise RuntimeError("redis unreachable")


async def test_a_cache_outage_degrades_the_limiter_instead_of_failing_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis being down must not turn every limited route into a 500.

    The fallback is a per-process counter, so the first call succeeds and the second
    is still refused: the caller sees a normal 429 rather than an internal error.
    """
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    reset_settings_cache()
    dependency = rate_limit(limit=1)

    await dependency(  # type: ignore[operator]
        request=make_request(host="198.51.100.10"),
        cache=_BrokenCache(),  # type: ignore[arg-type]
        settings=get_settings(),
    )
    with pytest.raises(RateLimitedError):
        await dependency(  # type: ignore[operator]
            request=make_request(host="198.51.100.10"),
            cache=_BrokenCache(),  # type: ignore[arg-type]
            settings=get_settings(),
        )
