"""HTTP coverage for the development sign-in, ``POST /auth/dev-login``.

The route is mounted only while ``DEV_LOGIN_ENABLED`` is set, so both states need
their own app built with the flag decided: the suite's default app never mounts it,
and ``dev_client`` mounts it deliberately. Nothing here is about passwords as a
feature — the service has none — it is about the one gated backdoor that lets a
developer or QA enter the app without a provider client id.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

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

    assert set(body["user"]["roles"]) >= {"admin", "support"}
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
    assert set(me.json()["roles"]) >= {"admin", "support"}


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
    assert set(second["user"]["roles"]) >= {"admin", "support"}


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
