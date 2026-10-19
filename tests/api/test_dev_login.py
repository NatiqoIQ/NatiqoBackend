"""HTTP coverage for the development sign-in, ``POST /auth/dev-login``.

The route is mounted only while ``DEV_LOGIN_ENABLED`` is set, so both states need
their own app built with the flag decided: the suite's default app never mounts it,
and ``dev_client`` mounts it deliberately. Nothing here is about passwords as a
feature — the service has none — it is about the one gated backdoor that lets a
developer or QA enter the app without a provider client id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import create_all, dispose_engine
from app.core.registry import import_all_models
from app.main import app

#: The credentials the backdoor accepts by default (``DEV_ADMIN_*`` settings).
DEV_CREDENTIALS = {"username": "admin", "password": "admin"}

DEVICE = {"platform": "android", "app_version": "0.1.0"}

#: What ``DEV_LOGIN_TOKEN_TTL_SECONDS`` documents; a session that expires sooner
#: than this is the bug the setting exists to prevent.
DEV_SESSION_TTL = 315_360_000

#: A dashboard dev server. Deliberately not a port in ``CORS_ORIGINS``: Vite picks the
#: next free one, so the grant has to come from the loopback pattern instead.
DASHBOARD_ORIGIN = "http://localhost:5173"


def _headers(value: str | None) -> set[str]:
    return {item.strip().lower() for item in (value or "").split(",") if item.strip()}


@asynccontextmanager
async def _client_built_with(
    monkeypatch: pytest.MonkeyPatch, **env: str
) -> AsyncIterator[AsyncClient]:
    """An app assembled from a specific environment, without the suite's defaults hiding it.

    ``CORS_ORIGINS`` is cleared rather than left unset so a developer's own ``.env`` — which
    may well list ``http://localhost:5173`` — cannot make the production case pass for the
    wrong reason. The middleware stack is fixed when the app object is built, so every case
    here needs its own app.
    """
    from app.core.config import reset_settings_cache
    from app.main import create_app

    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    monkeypatch.setenv("CORS_ORIGINS", "")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    import_all_models()
    await create_all()
    built_app = create_app()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=built_app), base_url="http://api.test"
        ) as http_client:
            yield http_client
    finally:
        reset_settings_cache()
        await dispose_engine()


@pytest_asyncio.fixture
async def dev_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """A client against an app with the development sign-in actually mounted.

    Built as a second app for the same reason as ``cors_client`` in the main suite:
    the route is chosen when ``create_app`` assembles the router, so the flag has to
    be set before the app exists. Environment and settings cache are restored after,
    or every later test in the session would inherit a backdoor.
    """
    from app.core.config import reset_settings_cache
    from app.main import create_app

    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    reset_settings_cache()
    import_all_models()
    await create_all()
    dev_app = create_app()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=dev_app), base_url="http://api.test"
        ) as http_client:
            yield http_client
    finally:
        reset_settings_cache()
        await dispose_engine()


async def sign_in_as_admin(client: AsyncClient, **overrides: object) -> dict:
    response = await client.post(
        "/api/v1/auth/dev-login", json={**DEV_CREDENTIALS, "device": DEVICE, **overrides}
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_the_development_sign_in_issues_a_session_that_never_expires(
    dev_client: AsyncClient,
) -> None:
    body = await sign_in_as_admin(dev_client)

    assert set(body["user"]["roles"]) >= {"admin"}
    assert body["user"]["email"] == "admin@natiqo.dev"
    assert body["tokens"]["expiresIn"] == DEV_SESSION_TTL
    assert body["tokens"]["refreshExpiresIn"] == DEV_SESSION_TTL

    # The claims were minted from the account the response describes: the token must
    # authenticate as this very user, not merely look right in the body.
    me = await dev_client.get(
        "/api/v1/me",
        headers={"Authorization": f"Bearer {body['tokens']['accessToken']}"},
    )
    assert me.status_code == 200, me.text
    assert me.json()["id"] == body["user"]["id"]
    assert set(me.json()["roles"]) >= {"admin"}


@pytest.mark.asyncio
async def test_a_second_sign_in_returns_the_same_admin_account(
    dev_client: AsyncClient,
) -> None:
    """The fixed identity must be recognised, not recreated on every call.

    A subject that changed between runs — or a sign-in that forgot it — would mint a
    second account for the same address and fail the unique constraint, or silently
    split one dev user into many.
    """
    first = await sign_in_as_admin(dev_client)
    second = await sign_in_as_admin(dev_client)

    assert second["user"]["id"] == first["user"]["id"]
    assert set(second["user"]["roles"]) >= {"admin"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credentials",
    [
        {"username": "admin", "password": "wrong-password"},
        {"username": "root", "password": "admin"},
        {"username": "Admin", "password": "admin"},
    ],
)
async def test_a_wrong_credential_is_rejected(
    dev_client: AsyncClient, credentials: dict[str, str]
) -> None:
    response = await dev_client.post("/api/v1/auth/dev-login", json=credentials)

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"


@pytest.mark.asyncio
async def test_the_route_is_not_mounted_without_the_flag() -> None:
    """The suite's default app has the flag off: the path must not exist at all.

    Not a 401 — an absent route. That is the state a deployment chooses by setting
    ``DEV_LOGIN_ENABLED=false``, and it is the point of the flag: nothing is refused
    at request time because nothing is there to refuse.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        response = await client.post("/api/v1/auth/dev-login", json=DEV_CREDENTIALS)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


# ============================================================== cross-origin access
#
# The backdoor is used from a browser: a dashboard dev server calls it from another
# origin. ``JSON`` bodies make that a preflighted request, and when the origin is not
# granted the browser reports a bare network error — no status code, nothing in the
# API's access log — which reads exactly like a broken server. These tests pin the
# grant so that failure mode cannot come back silently.


@pytest.mark.asyncio
async def test_a_preflight_from_the_dashboard_dev_server_is_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _client_built_with(monkeypatch) as client:
        preflight = await client.options(
            "/api/v1/auth/dev-login",
            headers={
                "Origin": DASHBOARD_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,authorization",
            },
        )

    assert preflight.status_code == 200, preflight.text
    assert preflight.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN
    assert preflight.headers["access-control-allow-credentials"] == "true"
    assert "post" in _headers(preflight.headers.get("access-control-allow-methods"))
    granted = _headers(preflight.headers.get("access-control-allow-headers"))
    assert {"content-type", "authorization"} <= granted, granted


@pytest.mark.asyncio
async def test_the_dev_sign_in_response_is_readable_from_that_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preflight is only half of it: the actual response must carry the grant too."""
    async with _client_built_with(monkeypatch) as client:
        response = await client.post(
            "/api/v1/auth/dev-login",
            json={**DEV_CREDENTIALS, "device": DEVICE},
            headers={"Origin": DASHBOARD_ORIGIN},
        )

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN
    assert response.json()["tokens"]["accessToken"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1:8100", "http://localhost:3000", "http://[::1]:5173"],
)
async def test_any_loopback_port_is_granted(monkeypatch: pytest.MonkeyPatch, origin: str) -> None:
    """A dev server's port is not knowable in advance, so the pattern covers the host."""
    async with _client_built_with(monkeypatch) as client:
        preflight = await client.options(
            "/api/v1/auth/dev-login",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )

    assert preflight.headers["access-control-allow-origin"] == origin


@pytest.mark.asyncio
async def test_the_loopback_grant_disappears_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allowance exists *for the development backdoor*, so it must not outlive it.

    Production keeps an exact allowlist. A pattern matching every loopback port there
    would let anything running on the host — a malicious local page, a compromised
    browser extension's content script origin — read authenticated responses.
    """
    async with _client_built_with(monkeypatch, APP_ENV="production") as client:
        preflight = await client.options(
            "/api/v1/auth/dev-login",
            headers={"Origin": DASHBOARD_ORIGIN, "Access-Control-Request-Method": "POST"},
        )

    # No CORS middleware at all here — there is no pattern and no allowlist — so the
    # preflight reaches routing and is refused as a method the route does not serve.
    assert preflight.status_code == 405, preflight.text
    assert "access-control-allow-origin" not in preflight.headers


@pytest.mark.asyncio
async def test_a_configured_pattern_is_honoured_over_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setting reaches ``CORSMiddleware``, including where the loopback grant does not.

    A preview deployment per branch is the case ``CORS_ORIGINS`` cannot express: the
    hostname is minted with the branch. Production is used here precisely because it
    gets no loopback pattern, so the grant can only have come from the setting.
    """
    async with _client_built_with(
        monkeypatch,
        APP_ENV="production",
        CORS_ALLOW_ORIGIN_REGEX=r"https://[a-z0-9-]+\.preview\.natiqo\.app",
    ) as client:
        preflight = await client.options(
            "/api/v1/auth/dev-login",
            headers={
                "Origin": "https://pr-7.preview.natiqo.app",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert preflight.status_code == 200, preflight.text
    assert preflight.headers["access-control-allow-origin"] == "https://pr-7.preview.natiqo.app"


@pytest.mark.asyncio
async def test_no_loopback_grant_when_the_backdoor_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no dev sign-in there is nothing to reach, so the widened grant goes too."""
    async with _client_built_with(monkeypatch, DEV_LOGIN_ENABLED="false") as client:
        preflight = await client.options(
            "/api/v1/auth/dev-login",
            headers={"Origin": DASHBOARD_ORIGIN, "Access-Control-Request-Method": "POST"},
        )

    # 404, not 405: the router itself is absent, so there is no route to preflight for.
    assert preflight.status_code == 404, preflight.text
    assert "access-control-allow-origin" not in preflight.headers
