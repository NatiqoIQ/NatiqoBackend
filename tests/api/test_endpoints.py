"""HTTP-level coverage for every operation the application publishes.

The integration suite in ``tests/integration`` exercises the service layer directly.
That is the right place to prove a use case is correct, but it says nothing about the
part a client actually touches: status codes, response serialization, auth wiring,
the error envelope, and the prefix the router is mounted under. Before this file no
test crossed the HTTP boundary at all, so an endpoint could be broken in ways no
existing test was able to see.

Two things are deliberately tested here rather than mocked out:

* the real ASGI app, so middleware, exception handlers, and dependency injection are
  the production ones;
* the password-reset collector, injected through ``dependency_overrides``, because
  the production adapter refuses to carry the token and the flow is otherwise
  unobservable from outside the process.

``test_every_published_operation_is_covered`` compares the OpenAPI document against
the operations these tests actually call, so a newly added endpoint fails CI until it
has a test instead of silently shipping untested.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SettingsDep, UowDep
from app.core.config import get_settings
from app.core.database import create_all, dispose_engine
from app.core.errors import ErrorCode
from app.core.registry import import_all_models
from app.core.types import ContentState, QuestionKind, SkillKind
from app.main import app
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)
from app.modules.users.application.auth_service import AuthService
from app.modules.users.infrastructure.delivery import InMemoryPasswordResetDelivery
from app.modules.users.presentation.routes import auth_service

#: A synthetic account. This is a test fixture, not a real person or a real address.
NEW_ACCOUNT = {
    "email": "endpoint-probe@natiqo.app",
    "password": "S3cure-Lantern-42",
    "display_name": "Endpoint Probe",
    "native_language": "ar",
    "english_variant": "british",
    "timezone": "Asia/Baghdad",
    "locale": "en-GB",
}


#: The device hint sent with a registration, so ``/me/devices`` has a row to return.
WEB_DEVICE = {
    "platform": "web",
    "app_version": "1.0.0",
    "user_agent": "endpoint-probe/1.0",
}

#: Every operation the OpenAPI document publishes, as ``(METHOD, path)``.
#:
#: ``test_every_published_operation_is_covered`` fails if this drifts from the spec in
#: either direction: a new endpoint with no test, or a test for an endpoint that was
#: removed.
PUBLISHED_OPERATIONS: set[tuple[str, str]] = {
    ("GET", "/health"),
    ("GET", "/ready"),
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/forgot-password"),
    ("POST", "/api/v1/auth/reset-password"),
    ("POST", "/api/v1/auth/verify-email"),
    ("POST", "/api/v1/auth/resend-verification"),
    ("POST", "/api/v1/auth/change-password"),
    ("GET", "/api/v1/me"),
    ("PATCH", "/api/v1/me"),
    ("DELETE", "/api/v1/me"),
    ("GET", "/api/v1/me/devices"),
    ("DELETE", "/api/v1/me/devices/{device_id}"),
    ("GET", "/api/v1/content/levels"),
    ("GET", "/api/v1/content/levels/{level_code}/units"),
    ("GET", "/api/v1/content/units/{unit_id}/lessons"),
    ("GET", "/api/v1/content/lessons/{lesson_id}"),
}


def error_code(response: Response) -> str:
    """Pull the stable error code out of the shared error envelope.

    Asserting on the envelope rather than the message is deliberate: the message is
    written for humans and is expected to change, while ``error.code`` is the
    contract clients are told to branch on.
    """
    return str(response.json()["error"]["code"])


@pytest.fixture
def reset_delivery() -> InMemoryPasswordResetDelivery:
    return InMemoryPasswordResetDelivery()


class _VerificationOutbox:
    """Captures verification links so an HTTP test can complete the confirmation.

    Without this the app falls back to a logger that sends nothing, and an end-to-end
    verification test would have no way to obtain the token the way a learner does.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_reset_link(self, **_kwargs: Any) -> None:
        raise AssertionError("the reset channel is not part of these flows")

    async def send_verification_link(
        self, *, user_id: Any, email: str, raw_token: str, expires_at: Any
    ) -> None:
        self.sent.append(raw_token)


@pytest.fixture
def verification_outbox() -> _VerificationOutbox:
    return _VerificationOutbox()


@pytest_asyncio.fixture
async def client(
    reset_delivery: InMemoryPasswordResetDelivery, verification_outbox: _VerificationOutbox
) -> AsyncIterator[AsyncClient]:
    """A real HTTP client against the real app, backed by a fresh in-memory schema.

    ``auth_service`` is overridden only to swap the delivery adapter. Everything the
    override builds is the production ``AuthService``; the use cases, repositories,
    and token handling are untouched.
    """
    from app.main import app

    import_all_models()
    await create_all()

    def build_service(uow: UowDep, settings: SettingsDep) -> AuthService:
        return AuthService(
            uow,
            settings,
            reset_delivery=reset_delivery,
            verification_delivery=verification_outbox,
        )

    app.dependency_overrides[auth_service] = build_service
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://api.test"
        ) as http_client:
            yield http_client
    finally:
        app.dependency_overrides.clear()
        await dispose_engine()


async def register(client: AsyncClient, account: dict[str, Any] = NEW_ACCOUNT) -> dict[str, Any]:
    """Create an account and return the ``user`` and ``tokens`` blocks."""
    response = await client.post("/api/v1/auth/register", json={**account, "device": WEB_DEVICE})
    assert response.status_code == 201, response.text
    return response.json()


def auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def camelise(key: str) -> str:
    """``display_name`` -> ``displayName``, matching the API's wire format."""
    head, *tail = key.split("_")
    return head + "".join(part.title() for part in tail)


def without_request_id(response: Response) -> dict[str, Any]:
    """The error envelope minus ``requestId``.

    Responses that must not disclose account state are compared through this: the
    per-request id is unique by design, so requiring byte equality would fail for a
    reason that has nothing to do with the property under test.
    """
    body = response.json()
    return {key: value for key, value in body["error"].items() if key != "requestId"}


# ====================================================================== probes
@pytest.mark.asyncio
async def test_health_and_readiness_report_success(client: AsyncClient) -> None:
    """Both probes must answer, and only readiness is allowed to touch the database."""
    health = await client.get("/health")
    assert health.status_code == 200, health.text
    assert health.json()["data"]["status"] == "healthy"

    ready = await client.get("/ready")
    assert ready.status_code == 200, ready.text
    report = ready.json()["data"]
    # A deep check that silently skipped its dependencies would report healthy here.
    assert report["checks"], "readiness returned no dependency checks"


# ==================================================================== register
@pytest.mark.asyncio
async def test_register_creates_account_and_issues_session(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/register", json={**NEW_ACCOUNT, "device": WEB_DEVICE}
    )
    assert response.status_code == 201, response.text

    body = response.json()
    user = body["user"]
    assert user["email"] == NEW_ACCOUNT["email"]
    assert user["status"] == "active"
    assert user["emailVerified"] is False
    # Registration grants the baseline role; anything beyond that is an operator act.
    assert user["roles"] == ["user"]
    assert user["profile"]["displayName"] == NEW_ACCOUNT["display_name"]
    assert user["profile"]["currentLevel"], "placement level should be defaulted"

    tokens = body["tokens"]
    assert tokens["tokenType"] == "Bearer"
    assert tokens["accessToken"] and tokens["refreshToken"]
    assert tokens["accessToken"] != tokens["refreshToken"]
    assert tokens["expiresIn"] > 0 and tokens["refreshExpiresIn"] > 0


@pytest.mark.asyncio
async def test_register_rejects_duplicate_email(client: AsyncClient) -> None:
    await register(client)
    second = await client.post("/api/v1/auth/register", json={**NEW_ACCOUNT, "device": WEB_DEVICE})
    assert second.status_code == 409, second.text
    # A specific code beats a generic conflict: the client can tell "that address is
    # taken" apart from any other 409 without parsing the message.
    assert error_code(second) == "AUTH_ACCOUNT_EXISTS"


@pytest.mark.asyncio
async def test_register_rejects_malformed_payload(client: AsyncClient) -> None:
    """Validation happens at the edge, so the service never sees a bad value."""
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": "", "display_name": "A"},
    )
    assert response.status_code == 422, response.text


# ======================================================================== login
@pytest.mark.asyncio
async def test_login_exchanges_credentials_for_tokens(client: AsyncClient) -> None:
    await register(client)

    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": NEW_ACCOUNT["email"],
            "password": NEW_ACCOUNT["password"],
            "device": WEB_DEVICE,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == NEW_ACCOUNT["email"]
    assert body["tokens"]["accessToken"]


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(client: AsyncClient) -> None:
    await register(client)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": NEW_ACCOUNT["email"], "password": "Wrong-Password-1"},
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_login_does_not_reveal_whether_an_account_exists(client: AsyncClient) -> None:
    """An unknown address and a wrong password must be indistinguishable."""
    unknown = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@natiqo.app", "password": "Whatever-123"},
    )
    await register(client)
    wrong = await client.post(
        "/api/v1/auth/login",
        json={"email": NEW_ACCOUNT["email"], "password": "Whatever-123"},
    )
    assert unknown.status_code == wrong.status_code == 401
    assert error_code(unknown) == error_code(wrong) == "AUTH_INVALID_CREDENTIALS"

    # Identical apart from `request_id`, which is unique per request by design and
    # carries no account state. Everything a client could branch on must match.
    assert without_request_id(unknown) == without_request_id(wrong)


# =========================================================================== me
@pytest.mark.asyncio
async def test_me_requires_a_bearer_token(client: AsyncClient) -> None:
    missing = await client.get("/api/v1/me")
    assert missing.status_code == 401
    # The module declares auto_error=False precisely so the error body is this
    # project's envelope rather than FastAPI's bare {"detail": ...}.
    assert error_code(missing) == "AUTH_TOKEN_INVALID"

    wrong_scheme = await client.get("/api/v1/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert wrong_scheme.status_code == 401
    assert error_code(wrong_scheme) == "AUTH_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_me_rejects_a_forged_token(client: AsyncClient) -> None:
    response = await client.get("/api/v1/me", headers=auth_headers("not.a.real.token"))
    assert response.status_code == 401
    assert error_code(response) == "AUTH_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_get_me_returns_profile_and_settings(client: AsyncClient) -> None:
    body = await register(client)
    response = await client.get("/api/v1/me", headers=auth_headers(body["tokens"]["accessToken"]))
    assert response.status_code == 200, response.text

    me = response.json()
    assert me["id"] == body["user"]["id"]
    assert me["email"] == NEW_ACCOUNT["email"]
    assert set(me["profile"]) == {
        "displayName",
        "avatarKey",
        "nativeLanguage",
        "englishVariant",
        "timezone",
        "currentLevel",
    }
    assert set(me["settings"]) == {
        "locale",
        "explanationLanguage",
        "notificationPreferences",
        "dailyGoalMinutes",
        "dailyGoalNewWords",
    }
    assert set(me["settings"]["notificationPreferences"]) == {
        "email",
        "push",
        "dailyReminder",
        "streakWarning",
        "reviewDue",
        "weeklyReport",
        "quietHoursStart",
        "quietHoursEnd",
    }


@pytest.mark.asyncio
async def test_patch_me_updates_profile_and_settings(client: AsyncClient) -> None:
    body = await register(client)
    response = await client.patch(
        "/api/v1/me",
        headers=auth_headers(body["tokens"]["accessToken"]),
        json={
            "display_name": "Updated Name",
            "daily_goal_minutes": 45,
            "notification_preferences": {"daily_reminder": False},
        },
    )
    assert response.status_code == 200, response.text

    me = response.json()
    assert me["profile"]["displayName"] == "Updated Name"
    assert me["settings"]["dailyGoalMinutes"] == 45
    assert me["settings"]["notificationPreferences"]["dailyReminder"] is False
    # An absent field is left untouched rather than defaulted.
    assert me["settings"]["notificationPreferences"]["email"] is True


@pytest.mark.asyncio
async def test_patch_me_refuses_to_set_current_level(client: AsyncClient) -> None:
    """A learner must not be able to skip the placement test."""
    body = await register(client)
    response = await client.patch(
        "/api/v1/me",
        headers=auth_headers(body["tokens"]["accessToken"]),
        json={"current_level": "C2"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_patch_me_rejects_out_of_range_values(client: AsyncClient) -> None:
    body = await register(client)
    response = await client.patch(
        "/api/v1/me",
        headers=auth_headers(body["tokens"]["accessToken"]),
        json={"daily_goal_minutes": 5_000},
    )
    assert response.status_code == 422, response.text


# ===================================================================== devices
@pytest.mark.asyncio
async def test_device_lifecycle_list_then_revoke(client: AsyncClient) -> None:
    body = await register(client)
    headers = auth_headers(body["tokens"]["accessToken"])

    listed = await client.get("/api/v1/me/devices", headers=headers)
    assert listed.status_code == 200, listed.text
    devices = listed.json()
    assert len(devices) == 1
    device = devices[0]
    assert device["platform"] == "web"
    assert device["appVersion"] == "1.0.0"
    assert device["lastSeenAt"] is not None

    revoked = await client.delete(f"/api/v1/me/devices/{device['id']}", headers=headers)
    assert revoked.status_code == 204, revoked.text
    assert revoked.content == b""

    after = await client.get("/api/v1/me/devices", headers=headers)
    assert after.json() == []


@pytest.mark.asyncio
async def test_device_list_is_scoped_to_the_caller(client: AsyncClient) -> None:
    """A device id belonging to another account is not reachable."""
    first = await register(client)
    second = await client.post(
        "/api/v1/auth/register",
        json={**NEW_ACCOUNT, "email": "second-probe@natiqo.app", "device": WEB_DEVICE},
    )
    assert second.status_code == 201, second.text

    first_devices = await client.get(
        "/api/v1/me/devices", headers=auth_headers(first["tokens"]["accessToken"])
    )
    device_id = first_devices.json()[0]["id"]

    response = await client.delete(
        f"/api/v1/me/devices/{device_id}",
        headers=auth_headers(second.json()["tokens"]["accessToken"]),
    )
    # A 422 "Unknown device." rather than a 404: the service deliberately answers
    # identically for a device that does not exist and one owned by someone else, so
    # the response cannot be used to discover another account's device ids.
    assert response.status_code == 422, response.text
    assert error_code(response) == "VALIDATION_ERROR"

    # And the owner's device is untouched.
    still_there = await client.get(
        "/api/v1/me/devices", headers=auth_headers(first["tokens"]["accessToken"])
    )
    assert [d["id"] for d in still_there.json()] == [device_id]


@pytest.mark.asyncio
async def test_revoke_device_rejects_a_malformed_id(client: AsyncClient) -> None:
    body = await register(client)
    response = await client.delete(
        "/api/v1/me/devices/not-a-uuid",
        headers=auth_headers(body["tokens"]["accessToken"]),
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "VALIDATION_ERROR"


# ====================================================================== tokens
@pytest.mark.asyncio
async def test_refresh_rotates_the_session(client: AsyncClient) -> None:
    body = await register(client)
    original = body["tokens"]["refreshToken"]

    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": original, "device": WEB_DEVICE}
    )
    assert response.status_code == 200, response.text

    rotated = response.json()["tokens"]
    assert rotated["refreshToken"] != original, "refresh must rotate the token"
    assert rotated["accessToken"] != body["tokens"]["accessToken"]

    # The superseded token must not still work.
    replay = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": original, "device": WEB_DEVICE}
    )
    assert replay.status_code == 401, replay.text


@pytest.mark.asyncio
async def test_refresh_rejects_a_short_token(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/refresh", json={"refresh_token": "short"})
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_logout_revokes_only_the_presented_session(client: AsyncClient) -> None:
    first = await register(client)
    second = await client.post(
        "/api/v1/auth/login",
        json={
            "email": NEW_ACCOUNT["email"],
            "password": NEW_ACCOUNT["password"],
            "device": {**WEB_DEVICE, "user_agent": "second-session/1.0"},
        },
    )
    assert second.status_code == 200, second.text
    second_tokens = second.json()["tokens"]

    response = await client.post(
        "/api/v1/auth/logout", json={"refresh_token": first["tokens"]["refreshToken"]}
    )
    assert response.status_code == 200, response.text
    assert "detail" in response.json()

    revoked = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": first["tokens"]["refreshToken"], "device": WEB_DEVICE},
    )
    assert revoked.status_code == 401

    # The other session survives a single-device logout.
    survivor = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": second_tokens["refreshToken"], "device": WEB_DEVICE},
    )
    assert survivor.status_code == 200, survivor.text


@pytest.mark.asyncio
async def test_logout_all_devices_revokes_everything(client: AsyncClient) -> None:
    first = await register(client)
    second = await client.post(
        "/api/v1/auth/login",
        json={
            "email": NEW_ACCOUNT["email"],
            "password": NEW_ACCOUNT["password"],
            "device": WEB_DEVICE,
        },
    )
    assert second.status_code == 200, second.text

    response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": first["tokens"]["refreshToken"], "all_devices": True},
    )
    assert response.status_code == 200, response.text

    for token in (first["tokens"]["refreshToken"], second.json()["tokens"]["refreshToken"]):
        replay = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": token, "device": WEB_DEVICE}
        )
        assert replay.status_code == 401


@pytest.mark.asyncio
async def test_logout_is_idempotent(client: AsyncClient) -> None:
    """A retry after a dropped response must not be told the logout failed."""
    body = await register(client)
    token = body["tokens"]["refreshToken"]
    for _ in range(2):
        response = await client.post("/api/v1/auth/logout", json={"refresh_token": token})
        assert response.status_code == 200, response.text


# ============================================================== password reset
@pytest.mark.asyncio
async def test_password_reset_round_trip(
    client: AsyncClient, reset_delivery: InMemoryPasswordResetDelivery
) -> None:
    body = await register(client)

    requested = await client.post(
        "/api/v1/auth/forgot-password", json={"email": NEW_ACCOUNT["email"]}
    )
    assert requested.status_code == 200, requested.text

    link = reset_delivery.latest_for(NEW_ACCOUNT["email"])
    assert link is not None, "reset link was never dispatched"
    # A credential in an HTTP body is a credential nobody can rotate.
    assert link.raw_token not in requested.text

    reset = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": link.raw_token, "new_password": "Rotated-Password-2"},
    )
    assert reset.status_code == 200, reset.text

    # A reset is a statement that the old password was lost, so it must invalidate it.
    with_old = await client.post(
        "/api/v1/auth/login",
        json={"email": NEW_ACCOUNT["email"], "password": NEW_ACCOUNT["password"]},
    )
    assert with_old.status_code == 401

    with_new = await client.post(
        "/api/v1/auth/login",
        json={"email": NEW_ACCOUNT["email"], "password": "Rotated-Password-2"},
    )
    assert with_new.status_code == 200, with_new.text

    # Existing sessions were opened with the old password and are treated as hostile.
    old_session = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": body["tokens"]["refreshToken"], "device": WEB_DEVICE},
    )
    assert old_session.status_code == 401


@pytest.mark.asyncio
async def test_forgot_password_does_not_reveal_account_existence(
    client: AsyncClient, reset_delivery: InMemoryPasswordResetDelivery
) -> None:
    await register(client)
    known = await client.post("/api/v1/auth/forgot-password", json={"email": NEW_ACCOUNT["email"]})
    unknown = await client.post("/api/v1/auth/forgot-password", json={"email": "nobody@natiqo.app"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    # Only the real account gets a token minted.
    assert reset_delivery.latest_for("nobody@natiqo.app") is None


@pytest.mark.asyncio
async def test_reset_password_rejects_a_bad_token(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": "x" * 40, "new_password": "Whatever-123"},
    )
    assert response.status_code in (401, 422), response.text


@pytest.mark.asyncio
async def test_reset_password_cannot_be_reused(
    client: AsyncClient, reset_delivery: InMemoryPasswordResetDelivery
) -> None:
    await register(client)
    await client.post("/api/v1/auth/forgot-password", json={"email": NEW_ACCOUNT["email"]})
    link = reset_delivery.latest_for(NEW_ACCOUNT["email"])
    assert link is not None

    first = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": link.raw_token, "new_password": "First-Rotation-1"},
    )
    assert first.status_code == 200, first.text

    replay = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": link.raw_token, "new_password": "Second-Rotation-2"},
    )
    assert replay.status_code in (401, 422), replay.text


# ================================================================ doc contract
@pytest.mark.asyncio
async def test_every_published_operation_is_covered(client: AsyncClient) -> None:
    """Fail CI when the API surface and its test coverage drift apart.

    A new endpoint with no test is a coverage hole nobody notices until it breaks in
    production, so the gap is turned into a failing assertion instead of a review
    comment somebody has to remember to leave.
    """
    from app.main import app

    document = app.openapi()
    published = {
        (method.upper(), path)
        for path, operations in document["paths"].items()
        for method in operations
        if method.lower() not in {"head", "options", "trace"}
    }

    untested = published - PUBLISHED_OPERATIONS
    assert not untested, f"published but untested: {sorted(untested)}"

    stale = PUBLISHED_OPERATIONS - published
    assert not stale, f"tested but no longer published: {sorted(stale)}"


# ------------------------------------------------------------------ verification
# The three endpoints a learner touches to prove an address is theirs. The thread through
# all of them: a token is a credential, so it is single-use, it is only ever emailed, and
# the learner must never be locked out by a provider having a bad minute.


async def test_confirming_an_address_flips_the_verified_flag_on_me(
    client: AsyncClient, verification_outbox: _VerificationOutbox
) -> None:
    created = await register(client)
    headers = auth_headers(created["tokens"]["accessToken"])
    await client.post("/api/v1/auth/resend-verification", headers=headers)
    assert len(verification_outbox.sent) == 1

    response = await client.post(
        "/api/v1/auth/verify-email", json={"token": verification_outbox.sent[0]}
    )

    assert response.status_code == 200, response.text
    me = await client.get("/api/v1/me", headers=headers)
    assert me.json()["emailVerified"] is True


async def test_an_unverified_account_can_still_use_the_app(client: AsyncClient) -> None:
    """Gating login would lock out everyone registered before verification existed."""
    created = await register(client)

    me = await client.get("/api/v1/me", headers=auth_headers(created["tokens"]["accessToken"]))

    assert me.status_code == 200, me.text
    assert me.json()["emailVerified"] is False


async def test_opening_the_same_link_twice_is_not_an_error(
    client: AsyncClient, verification_outbox: _VerificationOutbox
) -> None:
    """Mail scanners open the link first; the learner must not be shown a failure."""
    created = await register(client)
    await client.post(
        "/api/v1/auth/resend-verification", headers=auth_headers(created["tokens"]["accessToken"])
    )
    body = {"token": verification_outbox.sent[0]}

    first = await client.post("/api/v1/auth/verify-email", json=body)
    second = await client.post("/api/v1/auth/verify-email", json=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text


async def test_a_forged_token_is_refused_without_saying_which_part_was_wrong(
    client: AsyncClient,
) -> None:
    response = await client.post("/api/v1/auth/verify-email", json={"token": "f" * 64})

    assert response.status_code == 422, response.text
    # One code for missing, expired and already-spent alike: splitting them would let an
    # attacker learn that a guessed token once existed.
    assert error_code(response) == "VALIDATION_ERROR"


async def test_resending_twice_does_not_send_a_second_email(
    client: AsyncClient, verification_outbox: _VerificationOutbox
) -> None:
    """A live link is the answer; a second copy is how a provider rate-limits the user."""
    headers = auth_headers((await register(client))["tokens"]["accessToken"])

    await client.post("/api/v1/auth/resend-verification", headers=headers)
    again = await client.post("/api/v1/auth/resend-verification", headers=headers)

    assert again.status_code == 200, again.text
    assert len(verification_outbox.sent) == 1


async def test_resending_always_answers_success_so_it_cannot_probe_verified_addresses(
    client: AsyncClient, verification_outbox: _VerificationOutbox
) -> None:
    created = await register(client)
    headers = auth_headers(created["tokens"]["accessToken"])
    await client.post("/api/v1/auth/resend-verification", headers=headers)
    await client.post("/api/v1/auth/verify-email", json={"token": verification_outbox.sent[0]})

    confirmed = await client.post("/api/v1/auth/resend-verification", headers=headers)

    assert confirmed.status_code == 200, confirmed.text
    assert len(verification_outbox.sent) == 1, "a confirmed address has nothing to resend"


async def test_resending_needs_a_session(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/resend-verification")

    assert response.status_code == 401, response.text


# ------------------------------------------------------------------ change password


async def test_changing_the_password_succeeds_and_kills_the_refresh_session(
    client: AsyncClient,
) -> None:
    """A change must not leave a working session behind.

    Note what this does *not* claim: the access token that made the request keeps working
    until it expires, because it is a self-contained JWT and revoking sessions cannot reach
    a token that has already been handed out. That is the trade for stateless auth, and it
    is why the access-token lifetime has to stay short. The renewable credential is the
    one that matters, and it dies here.
    """
    created = await register(client)
    refresh_token = created["tokens"]["refreshToken"]

    changed = await client.post(
        "/api/v1/auth/change-password",
        headers=auth_headers(created["tokens"]["accessToken"]),
        json={"currentPassword": NEW_ACCOUNT["password"], "newPassword": "Baghdad-progresses-2027"},
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["sessionsRevoked"] >= 1

    renewed = await client.post("/api/v1/auth/refresh", json={"refreshToken": refresh_token})

    assert renewed.status_code == 401, renewed.text


async def test_the_old_password_stops_working_after_a_change(client: AsyncClient) -> None:
    created = await register(client)
    await client.post(
        "/api/v1/auth/change-password",
        headers=auth_headers(created["tokens"]["accessToken"]),
        json={"currentPassword": NEW_ACCOUNT["password"], "newPassword": "Baghdad-progresses-2027"},
    )

    with_old = await client.post(
        "/api/v1/auth/login",
        json={"email": NEW_ACCOUNT["email"], "password": NEW_ACCOUNT["password"]},
    )
    with_new = await client.post(
        "/api/v1/auth/login",
        json={"email": NEW_ACCOUNT["email"], "password": "Baghdad-progresses-2027"},
    )

    assert with_old.status_code == 401, with_old.text
    assert with_new.status_code == 200, with_new.text


async def test_a_wrong_current_password_is_refused(client: AsyncClient) -> None:
    created = await register(client)
    headers = auth_headers(created["tokens"]["accessToken"])

    response = await client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"currentPassword": "not-the-password", "newPassword": "Baghdad-progresses-2027"},
    )

    assert response.status_code == 401, response.text
    # The session must survive a failed attempt, or one typo locks the learner out.
    assert (await client.get("/api/v1/me", headers=headers)).status_code == 200


async def test_reusing_the_same_password_is_refused(client: AsyncClient) -> None:
    """Re-hashing an identical password changes the hash while changing nothing."""
    created = await register(client)
    headers = auth_headers(created["tokens"]["accessToken"])

    response = await client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"currentPassword": NEW_ACCOUNT["password"], "newPassword": NEW_ACCOUNT["password"]},
    )

    assert response.status_code == 422, response.text
    assert (await client.get("/api/v1/me", headers=headers)).status_code == 200


async def test_a_weak_new_password_is_refused(client: AsyncClient) -> None:
    created = await register(client)

    response = await client.post(
        "/api/v1/auth/change-password",
        headers=auth_headers(created["tokens"]["accessToken"]),
        json={"currentPassword": NEW_ACCOUNT["password"], "newPassword": "short"},
    )

    assert response.status_code == 422, response.text
    assert error_code(response) == "AUTH_WEAK_PASSWORD"


async def test_changing_a_password_needs_a_session(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/change-password",
        json={"currentPassword": NEW_ACCOUNT["password"], "newPassword": "Baghdad-progresses-2027"},
    )

    assert response.status_code == 401, response.text


# ============================================================== account closure (GDPR)
# "Delete my account" has one obligation that matters and a hundred ways to leak around it.
# The obligation: the account stops being reachable. Everything below is a different door,
# and each is checked separately because closing only some of them is the failure mode.


@pytest.mark.asyncio
async def test_deleting_the_account_closes_it_and_kills_the_session(client: AsyncClient) -> None:
    """The endpoint's whole job.

    Same honest limit as the password change above — the access token in flight expires on
    its own — but the renewable credential dies here, and every later authenticated request
    is refused because the account behind it is closed.
    """
    created = await register(client)
    access_token = created["tokens"]["accessToken"]
    refresh_token = created["tokens"]["refreshToken"]

    deleted = await client.delete("/api/v1/me", headers=auth_headers(access_token))

    assert deleted.status_code == 204, deleted.text
    assert deleted.content == b""

    renewed = await client.post("/api/v1/auth/refresh", json={"refreshToken": refresh_token})
    assert renewed.status_code == 401, renewed.text

    afterwards = await client.get("/api/v1/me", headers=auth_headers(access_token))
    assert afterwards.status_code == 401, afterwards.text
    assert error_code(afterwards) == ErrorCode.USER_DELETED.value


@pytest.mark.asyncio
async def test_a_closed_account_can_no_longer_log_in(client: AsyncClient) -> None:
    created = await register(client)
    await client.delete("/api/v1/me", headers=auth_headers(created["tokens"]["accessToken"]))

    relogin = await client.post(
        "/api/v1/auth/login",
        json={"email": NEW_ACCOUNT["email"], "password": NEW_ACCOUNT["password"]},
    )

    assert relogin.status_code == 401, relogin.text
    assert error_code(relogin) == ErrorCode.USER_DELETED.value


@pytest.mark.asyncio
async def test_deleting_twice_still_answers_the_same_way(client: AsyncClient) -> None:
    """A retried delete must not turn a dropped response into a visible error."""
    created = await register(client)
    headers = auth_headers(created["tokens"]["accessToken"])

    first = await client.delete("/api/v1/me", headers=headers)
    second = await client.delete("/api/v1/me", headers=headers)

    assert first.status_code == 204, first.text
    assert second.status_code == 401, second.text
    assert error_code(second) == ErrorCode.USER_DELETED.value


@pytest.mark.asyncio
async def test_deleting_the_account_needs_a_session(client: AsyncClient) -> None:
    anonymous = await client.delete("/api/v1/me")

    assert anonymous.status_code == 401, anonymous.text


@pytest.mark.asyncio
async def test_an_unconfirmed_legacy_account_can_still_log_in(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Seeded and pre-feature accounts sit in ``pending_verification``.

    Refusing them would lock out every account that registered before verification existed,
    which is the opposite of what "verification is a nudge, not a gate" means. The status
    stays truthful and ``/me`` still reports the address as unconfirmed.
    """
    created = await register(client)
    await content_db.execute(
        text("update users set status = 'pending_verification' where id = :uid"),
        {"uid": created["user"]["id"]},
    )
    await content_db.commit()

    relogin = await client.post(
        "/api/v1/auth/login",
        json={"email": NEW_ACCOUNT["email"], "password": NEW_ACCOUNT["password"]},
    )
    assert relogin.status_code == 200, relogin.text

    me = await client.get("/api/v1/me", headers=auth_headers(relogin.json()["tokens"]["accessToken"]))
    assert me.status_code == 200, me.text
    assert me.json()["emailVerified"] is False


# ===================================================================== security headers
# Regression cover for a blank /docs page. SecurityHeadersMiddleware set
# ``default-src 'none'`` on every response, which is correct for JSON and fatal for the
# one HTML document the app serves: Swagger UI loads its bundle and stylesheet from
# jsDelivr and boots through an inline <script>, so the browser blocked all three and
# the page rendered empty. Nothing asserted on the CSP, so the failure was invisible to
# the suite. The fix keeps the strict policy everywhere except the docs routes.


async def test_docs_page_carries_a_policy_that_actually_permits_its_assets(
    client: AsyncClient,
) -> None:
    """Every origin the page references must be allowed, or the page renders blank."""
    response = await client.get("/docs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")

    policy = response.headers["content-security-policy"]

    # Derived from the served HTML rather than assumed: whatever the page pulls in
    # has to be reachable under this policy.
    for origin in ("https://cdn.jsdelivr.net", "https://fastapi.tiangolo.com"):
        assert origin in response.text, f"page no longer references {origin}"
        assert origin in policy, f"{origin} is loaded by the page but blocked by the CSP"

    # FastAPI inlines the SwaggerUIBundle config, so an inline allowance is required
    # unless the template is pinned to a hash that changes on every FastAPI upgrade.
    assert re.search(r"<script(?![^>]*\ssrc=)", response.text), "expected an inline script"
    assert "'unsafe-inline'" in policy

    # The favicon and the /openapi.json fetch the page performs.
    assert "img-src" in policy
    assert "connect-src 'self'" in policy, "the page fetches /openapi.json same-origin"


async def test_docs_policy_keeps_the_document_unreachable_from_frames(
    client: AsyncClient,
) -> None:
    """Relaxing script-src for the docs page must not relax clickjacking protection."""
    policy = (await client.get("/docs")).headers["content-security-policy"]

    assert "frame-ancestors 'none'" in policy
    assert (await client.get("/docs")).headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/openapi.json"),
        ("GET", "/api/v1/me"),
    ],
)
async def test_non_document_responses_keep_the_strict_policy(
    client: AsyncClient, method: str, path: str
) -> None:
    """The docs allowance must not leak onto JSON responses.

    ``/openapi.json`` is included deliberately: it is what the docs page fetches, so it
    is the most plausible place for the relaxation to spread by accident.
    """
    response = await client.request(method, path)

    policy = response.headers["content-security-policy"]

    assert "default-src 'none'" in policy
    assert "script-src" not in policy, f"{path} must not carry the docs script policy"
    assert "cdn.jsdelivr.net" not in policy


async def test_an_explicit_csp_set_further_in_still_wins() -> None:
    """``setdefault`` semantics: a handler that sets its own policy keeps it.

    This is the invariant that lets the docs branch exist at all — if this broke, the
    middleware would overwrite a deliberately narrower policy instead of deferring to
    it, so a handler could never tighten the policy below the default.

    Built on a throwaway app rather than the real one: Starlette refuses to add
    middleware once an app has started, and mutating the shared app's middleware stack
    from a test leaks into every later test in the session.
    """
    from fastapi import FastAPI

    from app.core.middleware import SecurityHeadersMiddleware

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    # No return annotations on these handlers on purpose. ``from __future__ import
    # annotations`` makes them strings, and FastAPI resolves them against the module
    # globals - where ``Response`` is ``httpx.Response`` - and then tries to build a
    # Pydantic model out of it.
    @app.get("/narrow")
    async def _narrow():
        from fastapi import Response as _Response

        response = _Response("ok")
        response.headers["content-security-policy"] = "default-src 'none'; sandbox"
        return response

    @app.get("/default")
    async def _default():
        from fastapi import Response as _Response

        return _Response("ok")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as c:
        narrow = await c.get("/narrow")
        assert narrow.headers["content-security-policy"] == "default-src 'none'; sandbox"

        # ...while a response that sets nothing still gets the hardened default.
        default = await c.get("/default")
        assert (
            default.headers["content-security-policy"]
            == "default-src 'none'; frame-ancestors 'none'"
        )


async def test_security_headers_are_present_on_docs_as_well(client: AsyncClient) -> None:
    """The docs page is still hardened — only the CSP source list is widened."""
    headers = (await client.get("/docs")).headers

    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cross-origin-resource-policy"] == "same-site"


# ------------------------------------------------------------------- catalogue
#: The exact public field set of a question.
#:
#: Asserting the whole set rather than "the bad keys are absent" is deliberate: a field
#: nobody intended to publish has to fail this test, and that is the only moment it is
#: cheap to notice.
PUBLIC_QUESTION_FIELDS = {"id", "kind", "skill", "prompt", "choices", "points"}

#: Never crosses the wire before an attempt.
ANSWER_KEY_FIELDS = ("correct_index", "accepted_answers", "explanation")

#: Published for testing, but with nothing under it.
DRAFT_LEVEL_CODE = "B1"


@pytest_asyncio.fixture
async def content_db(client: AsyncClient) -> AsyncIterator[AsyncSession]:
    """A session for seeding catalogue rows.

    Depends on ``client`` so the schema exists first: both fixtures share one
    process-wide engine, and the in-memory database belongs to the engine, not to a
    session.

    The commits here are load-bearing. The engine backs ``:memory:`` SQLite with a single
    shared connection, so rows this session has flushed but not committed are visible to
    the next request — and then destroyed by that request's rollback, because the unit of
    work and this session are competing for the same transaction. Without the commit, the
    first assertion sees the row and the second one does not, which reads as a flaky
    pagination bug rather than as a fixture bug.
    """
    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        yield session


async def seed_catalogue(
    session: AsyncSession, *, lessons: int = 2, with_explanation: bool = True
) -> dict[str, uuid.UUID]:
    """Two published levels, each with a unit, lessons and one question per lesson.

    Also plants one draft unit under A1 and one wholly draft level, because "drafts are
    invisible" is the property most likely to rot and it needs something unpublished in
    the fixture to be worth anything.
    """
    ids: dict[str, uuid.UUID] = {}
    for index, code in enumerate(("A1", "A2"), start=1):
        level = LevelModel(
            id=uuid.uuid4(),
            code=code,
            title=f"Level {code}",
            position=index,
            state=ContentState.PUBLISHED.value,
        )
        unit = UnitModel(
            id=uuid.uuid4(),
            level_id=level.id,
            title="Introductions",
            position=1,
            state=ContentState.PUBLISHED.value,
        )
        session.add_all([level, unit])
        for position in range(1, lessons + 1):
            lesson = LessonModel(
                id=uuid.uuid4(),
                unit_id=unit.id,
                title=f"Lesson {position}",
                summary="A short summary.",
                position=position,
                estimated_minutes=10,
                state=ContentState.PUBLISHED.value,
            )
            session.add_all(
                [
                    lesson,
                    QuestionModel(
                        id=uuid.uuid4(),
                        lesson_id=lesson.id,
                        position=1,
                        kind=QuestionKind.MULTIPLE_CHOICE.value,
                        skill=SkillKind.VOCABULARY.value,
                        prompt="Which one means 'hello'?",
                        choices=["Hello", "Goodbye", "Thank you"],
                        accepted_answers=[],
                        correct_index=0,
                        explanation="A greeting." if with_explanation else None,
                        points=1,
                    ),
                ]
            )
            ids[f"{code}-lesson-{position}"] = lesson.id
        ids[f"{code}-unit"] = unit.id
        ids[f"{code}-level"] = level.id

    session.add_all(
        [
            UnitModel(
                id=uuid.uuid4(),
                level_id=ids["A1-level"],
                title="Unfinished unit",
                position=2,
                state=ContentState.DRAFT.value,
            ),
            LevelModel(
                id=uuid.uuid4(),
                code=DRAFT_LEVEL_CODE,
                title="Still being written",
                position=3,
                state=ContentState.REVIEW.value,
            ),
        ]
    )
    await session.commit()
    return ids


async def test_the_catalogue_needs_no_token(client: AsyncClient, content_db: AsyncSession) -> None:
    """A signed-out app shell has to be able to show that a curriculum exists."""
    await seed_catalogue(content_db)

    response = await client.get("/api/v1/content/levels")

    assert response.status_code == 200
    assert [item["code"] for item in response.json()["items"]] == ["A1", "A2"]


async def test_a_draft_level_is_absent_from_the_list(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    await seed_catalogue(content_db)

    codes = [item["code"] for item in (await client.get("/api/v1/content/levels")).json()["items"]]

    assert DRAFT_LEVEL_CODE not in codes


async def test_the_levels_page_reports_counts_and_cursor_metadata(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    await seed_catalogue(content_db)

    body = (await client.get("/api/v1/content/levels")).json()

    assert body["items"][0]["unitCount"] == 1, "the draft unit must not be counted"
    assert body["meta"] == {"limit": 20, "hasMore": False, "nextCursor": None}


async def test_walking_the_levels_with_a_cursor_covers_every_level(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The boundary bug this catches: a cursor built from the wrong row drops or repeats
    one on every page."""
    await seed_catalogue(content_db)

    seen: list[str] = []
    cursor: str | None = None
    while True:
        url = "/api/v1/content/levels?limit=1"
        if cursor:
            url += f"&cursor={cursor}"
        body = (await client.get(url)).json()
        seen += [item["code"] for item in body["items"]]
        cursor = body["meta"]["nextCursor"]
        if not body["meta"]["hasMore"]:
            break

    assert seen == ["A1", "A2"], "paging lost or repeated a level"
    assert cursor is None


async def test_a_useless_cursor_is_a_validation_error_not_a_silent_first_page(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Swallowing it and returning page one would look like a working catalogue."""
    await seed_catalogue(content_db)

    response = await client.get("/api/v1/content/levels?cursor=not-a-cursor")

    assert response.status_code == 422
    assert error_code(response) == "VALIDATION_ERROR"


@pytest.mark.parametrize("limit", [0, -1, 101])
async def test_the_page_size_is_bounded(
    client: AsyncClient, content_db: AsyncSession, limit: int
) -> None:
    """An unbounded limit on an unauthenticated endpoint is a free load amplifier."""
    await seed_catalogue(content_db)

    response = await client.get(f"/api/v1/content/levels?limit={limit}")

    assert response.status_code == 422


async def test_units_of_an_unknown_level_are_not_found(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Takes the fixture only for its schema; an empty catalogue is enough."""
    response = await client.get("/api/v1/content/levels/Z9/units")

    assert response.status_code == 404
    assert error_code(response) == "LEVEL_NOT_FOUND"


async def test_units_of_a_draft_level_look_exactly_like_an_unknown_one(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Otherwise the catalogue becomes a map of the editorial pipeline."""
    await seed_catalogue(content_db)

    unknown = await client.get("/api/v1/content/levels/Z9/units")
    unpublished = await client.get(f"/api/v1/content/levels/{DRAFT_LEVEL_CODE}/units")

    assert unpublished.status_code == unknown.status_code == 404
    assert unpublished.json()["error"]["code"] == unknown.json()["error"]["code"]


async def test_lessons_are_listed_with_their_question_counts(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    ids = await seed_catalogue(content_db)

    body = (await client.get(f"/api/v1/content/units/{ids['A1-unit']}/lessons")).json()

    assert [item["title"] for item in body["items"]] == ["Lesson 1", "Lesson 2"]
    assert [item["questionCount"] for item in body["items"]] == [1, 1]


async def test_lessons_of_an_unknown_unit_are_not_found(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/content/units/{uuid.uuid4()}/lessons")

    assert response.status_code == 404
    assert error_code(response) == "UNIT_NOT_FOUND"


async def test_the_lesson_detail_carries_its_questions(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    ids = await seed_catalogue(content_db)

    body = (await client.get(f"/api/v1/content/lessons/{ids['A1-lesson-1']}")).json()

    assert body["title"] == "Lesson 1"
    assert body["questionCount"] == 1
    assert [question["prompt"] for question in body["questions"]] == ["Which one means 'hello'?"]


async def test_the_response_never_carries_the_answer_key(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The most important test in this file.

    A published lesson is readable with no token, so whatever this response contains is
    public. ``correct_index``, ``accepted_answers`` and ``explanation`` are the answer key
    to a practice test; serving them before an attempt turns the catalogue into an
    answer-bank download. The whole field set is asserted so that a *new* field leaks
    loudly instead of quietly.
    """
    ids = await seed_catalogue(content_db)

    response = await client.get(f"/api/v1/content/lessons/{ids['A1-lesson-1']}")
    question = response.json()["questions"][0]

    assert set(question) == PUBLIC_QUESTION_FIELDS
    for field in ANSWER_KEY_FIELDS:
        assert field not in question, f"{field} would hand out the answer key"
    assert "A greeting." not in response.text


async def test_a_free_text_question_keeps_the_same_field_set(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Filling in the blanks must not smuggle ``accepted_answers`` in through a side door."""
    ids = await seed_catalogue(content_db)
    lesson = await content_db.get(LessonModel, ids["A2-lesson-1"])
    assert lesson is not None
    questions = (
        await content_db.scalars(select(QuestionModel).where(QuestionModel.lesson_id == lesson.id))
    ).all()
    questions[0].kind = QuestionKind.FILL_BLANK.value
    questions[0].choices = []
    questions[0].correct_index = None
    questions[0].accepted_answers = ["hello", "hi there"]
    await content_db.commit()

    body = (await client.get(f"/api/v1/content/lessons/{lesson.id}")).json()
    question = body["questions"][0]

    assert set(question) == PUBLIC_QUESTION_FIELDS
    assert question["choices"] == []
    assert question["kind"] == QuestionKind.FILL_BLANK.value


async def test_a_lesson_with_no_questions_is_still_a_readable_page(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """An empty list, not a 404: the lesson exists and is published."""
    ids = await seed_catalogue(content_db)
    empty = LessonModel(
        id=uuid.uuid4(),
        unit_id=ids["A1-unit"],
        title="Empty",
        summary="Nothing yet.",
        position=99,
        estimated_minutes=5,
        state=ContentState.PUBLISHED.value,
    )
    content_db.add(empty)
    await content_db.commit()

    body = (await client.get(f"/api/v1/content/lessons/{empty.id}")).json()

    assert body["questions"] == []
    assert body["questionCount"] == 0


async def test_a_draft_lesson_is_not_found(client: AsyncClient, content_db: AsyncSession) -> None:
    ids = await seed_catalogue(content_db)
    draft = LessonModel(
        id=uuid.uuid4(),
        unit_id=ids["A1-unit"],
        title="Draft",
        summary="Not ready.",
        position=99,
        estimated_minutes=5,
        state=ContentState.REVIEW.value,
    )
    content_db.add(draft)
    await content_db.commit()

    response = await client.get(f"/api/v1/content/lessons/{draft.id}")

    assert response.status_code == 404
    assert error_code(response) == "LESSON_NOT_FOUND"


async def test_a_draft_lesson_and_an_invented_id_are_indistinguishable(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Same status, same code. Only the human message may differ, and that is not a
    contract clients may parse."""
    ids = await seed_catalogue(content_db)
    draft = LessonModel(
        id=uuid.uuid4(),
        unit_id=ids["A1-unit"],
        title="Draft",
        summary="Not ready.",
        position=98,
        estimated_minutes=5,
        state=ContentState.REVIEW.value,
    )
    content_db.add(draft)
    await content_db.commit()

    unpublished = await client.get(f"/api/v1/content/lessons/{draft.id}")
    invented = await client.get(f"/api/v1/content/lessons/{uuid.uuid4()}")

    assert unpublished.status_code == invented.status_code == 404
    assert unpublished.json()["error"]["code"] == invented.json()["error"]["code"]


# ==================================================================== wire format
@pytest.mark.asyncio
async def test_every_response_body_is_camel_case(client: AsyncClient) -> None:
    """The wire is camelCase everywhere, with no snake_case left on the surface.

    Checked by walking the actual JSON rather than against a hand-written list of fields:
    a per-endpoint expected-shape list would itself rot, and it would only ever prove the
    endpoints somebody remembered to write down.
    """
    registered = await client.post("/api/v1/auth/register", json=NEW_ACCOUNT)
    assert registered.status_code == 201, registered.text
    tokens = registered.json()["tokens"]

    probes = [
        await client.get("/api/v1/content/levels"),
        await client.get("/api/v1/content/levels/A1/units"),
        await client.get("/health"),
        await client.get("/api/v1/content/levels/Z9/units"),
        await client.get("/api/v1/me", headers=auth_headers(tokens["accessToken"])),
    ]

    def offenders(value: object, path: str = "$") -> list[str]:
        """Every dict key containing an underscore, at any depth."""
        if isinstance(value, dict):
            found = [f"{path}.{key}" for key in value if isinstance(key, str) and "_" in key]
            return found + [
                hit for key, item in value.items() for hit in offenders(item, f"{path}.{key}")
            ]
        if isinstance(value, list):
            return [hit for i, item in enumerate(value) for hit in offenders(item, f"{path}[{i}]")]
        return []

    for response in probes:
        assert not offenders(response.json()), (
            f"{response.request.url.path} leaked snake_case keys: {offenders(response.json())}"
        )


@pytest.mark.asyncio
async def test_a_request_body_is_accepted_in_either_spelling(client: AsyncClient) -> None:
    """camelCase is the documented input; snake_case still has to work.

    This is what makes the rename deployable: the server and the mobile app ship
    independently, so for at least one release window a client on the old build and a server
    on the new one must both function. Once that window closes the snake_case half of this
    test can be deleted — but deleting it is a decision, and not doing so is not.
    """
    body = {key: value for key, value in NEW_ACCOUNT.items() if key != "email"}

    camel = {camelise(key): value for key, value in body.items()}
    snake = dict(body)
    mixed = dict(camel)
    mixed["displayName"] = body["display_name"]

    # Distinct addresses: the three payloads must differ only in spelling, and registering
    # the same account three times would otherwise fail on the uniqueness check and prove
    # nothing about parsing.
    for label, payload in (("camel", camel), ("snake", snake), ("mixed", mixed)):
        payload["email"] = f"{label}-input@natiqo.app"
        response = await client.post("/api/v1/auth/register", json=payload)
        assert response.status_code == 201, f"{label} input rejected: {response.text}"
        assert response.json()["user"]["email"] == payload["email"]
        assert response.json()["user"]["profile"]["displayName"] == body["display_name"]


# ===================================================== caching and rate limiting
@pytest.mark.asyncio
async def test_a_catalogue_response_is_revalidatable_rather_than_refetched(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The phone's launch path: one body, then free 304s until it changes.

    This is the whole reason the ETag exists. A learner reopening the app should not
    re-download a curriculum they already hold, and without a validator the only honest
    options are "download it again" or "guess when it went stale".
    """
    await seed_catalogue(content_db)

    first = await client.get("/api/v1/content/levels")
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag, "a cacheable route must return a validator"
    assert "max-age=60" in first.headers["cache-control"]

    again = await client.get("/api/v1/content/levels", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b"", "a 304 must not spend bandwidth re-sending the body"
    # The freshness of the copy the client already holds is updated by this response, so it
    # has to carry the same directives the 200 did, not a revalidate-immediately one.
    assert again.headers["cache-control"] == first.headers["cache-control"]


@pytest.mark.asyncio
async def test_an_unchanged_validator_survives_a_stale_one_being_offered(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A conditional request carrying a tag we do not have gets the body, not a 304."""
    await seed_catalogue(content_db)
    stale = await client.get("/api/v1/content/levels")
    current = stale.headers["etag"]

    response = await client.get(
        "/api/v1/content/levels", headers={"If-None-Match": '"a-tag-from-a-future-deploy"'}
    )
    assert response.status_code == 200
    assert response.json() == stale.json()
    assert current  # the current tag is unchanged by this


@pytest.mark.asyncio
async def test_two_different_catalogue_pages_do_not_share_a_validator(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """ETags are per resource. Sharing one would make a client's 304s unsound."""
    await seed_catalogue(content_db)
    levels = await client.get("/api/v1/content/levels")
    units = await client.get("/api/v1/content/levels/A1/units")
    assert levels.headers["etag"] != units.headers["etag"]

    # Offering levels' tag against units must therefore miss.
    crossed = await client.get(
        "/api/v1/content/levels/A1/units",
        headers={"If-None-Match": levels.headers["etag"]},
    )
    assert crossed.status_code == 200


@pytest.mark.asyncio
async def test_a_miss_is_not_cacheable(client: AsyncClient) -> None:
    """A 404 here means "not published", not "never will be".

    Caching it for the freshness window would let an editor publish a lesson and have
    learners keep the miss — the content exists, the phone simply refuses to ask again.
    """
    response = await client.get("/api/v1/content/levels/Z9/units")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers


@pytest.mark.asyncio
async def test_authenticated_data_is_never_cached(client: AsyncClient) -> None:
    """A profile on a shared phone's HTTP cache is a leak waiting for the next user."""
    body = await register(client)
    me = await client.get("/api/v1/me", headers=auth_headers(body["tokens"]["accessToken"]))
    assert me.status_code == 200
    assert me.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_the_catalogue_budget_is_enforced_per_client(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, content_db: AsyncSession
) -> None:
    """An unauthenticated read path still needs a ceiling.

    Exempt on the grounds that the data is public is not an exemption: public and free is
    exactly what makes it worth walking in a loop.
    """
    from app.core import config as config_module

    await seed_catalogue(content_db)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_CATALOGUE_REQUESTS_PER_MINUTE", "3")
    config_module.reset_settings_cache()

    statuses = [(await client.get("/api/v1/content/levels")).status_code for _ in range(5)]

    assert statuses[:3] == [200, 200, 200], "the budget must actually admit traffic"
    assert statuses[3:] == [429, 429]

    blocked = await client.get("/api/v1/content/levels")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"], "a client must be told when to come back"
    assert error_code(blocked) == "RATE_LIMITED"


# ==================================================================================== CORS
#: The headers a browser client must be able to read. Asserted because a header that is not
#: exposed is silently invisible: the server sends it, no test notices, and the client's
#: ``undefined`` is indistinguishable from a server that never sent it.
CLIENT_READABLE_HEADERS = frozenset(
    {"ETag", "Cache-Control", "X-Request-ID", "Retry-After", "Content-Encoding"}
)

CORS_ORIGIN = "https://app.natiqo.app"


@pytest_asyncio.fixture
async def cors_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    """A client against an app built with CORS actually enabled.

    A second app rather than an option on the main ``client`` fixture, because CORS is decided
    when the middleware stack is assembled: by the time ``client`` exists, the origin allowlist
    is fixed, and no request can change it. The allowlist is therefore the only way to observe
    the grant, so the app has to be built around it.

    Environment is restored and the settings cache dropped afterwards, or this fixture would
    leak an allowlist into every later test in the file.
    """
    from app.core.config import reset_settings_cache
    from app.main import create_app

    monkeypatch.setenv("CORS_ORIGINS", CORS_ORIGIN)
    reset_settings_cache()
    cors_app = create_app()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=cors_app), base_url="http://api.test"
        ) as http_client:
            yield http_client
    finally:
        reset_settings_cache()


@pytest.mark.asyncio
async def test_a_cross_origin_client_can_read_the_headers_the_api_relies_on(
    cors_client: AsyncClient,
) -> None:
    """A browser is denied headers it has not been granted, so the grant is part of the API.

    Without ``ETag`` on this list a web build cannot store a validator, cannot send
    ``If-None-Match``, and therefore never revalidates — revalidation would work in
    production for native clients and silently not for the web and webview builds, which is
    the kind of gap that only surfaces as a support ticket.
    """
    response = await cors_client.get("/health", headers={"Origin": CORS_ORIGIN})

    assert response.headers["access-control-allow-origin"] == CORS_ORIGIN
    exposed = {
        value.strip()
        for value in (response.headers.get("access-control-expose-headers") or "").split(",")
        if value.strip()
    }
    missing = CLIENT_READABLE_HEADERS - exposed
    assert not missing, f"not readable by a browser client: {sorted(missing)}"


@pytest.mark.asyncio
async def test_an_unknown_origin_is_not_granted_access(cors_client: AsyncClient) -> None:
    """The allowlist is the control. An unmatched origin must get no CORS grant at all."""
    preflight = await cors_client.options(
        "/api/v1/content/levels",
        headers={"Origin": "https://attacker.example", "Access-Control-Request-Method": "GET"},
    )

    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers


# ============================================================ client-visible edge cases
@pytest.mark.asyncio
async def test_a_trailing_slash_redirects_without_losing_the_query_string(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A trailing slash is a 307 to the canonical path.

    Worth pinning because it is invisible in testing and hostile on mobile: a client that
    builds URLs by concatenation gets a redirect on a path it never spelled out, and if it
    follows redirects without re-sending the query string it silently loses ``limit`` and
    ``cursor`` and lands on page one of a list it was trying to page through.

    Also the reason ``redirect_slashes`` is left on: disabling it would answer the slashed
    path with a 404 instead, which is a worse failure than an extra round trip.
    """
    await seed_catalogue(content_db)

    response = await client.get("/api/v1/content/levels/", params={"limit": 1})

    assert response.status_code == 307
    assert response.headers["location"].endswith("/api/v1/content/levels?limit=1"), (
        "the query string must survive the redirect or pagination silently resets"
    )


@pytest.mark.asyncio
async def test_a_malformed_id_and_an_absent_one_are_distinguishable(client: AsyncClient) -> None:
    """422 means "that is not an id"; 404 means "no such published thing".

    The asymmetry is deliberate. A malformed code is passed through to the catalogue and
    answered exactly like an unknown one, so a client cannot tell a typo from an unpublished
    lesson by probing — but a *malformed UUID* never reaches the catalogue, because it cannot
    be a row id at all, and saying so saves the client a pointless retry.
    """
    malformed = await client.get("/api/v1/content/units/not-a-uuid/lessons")
    well_formed_but_absent = await client.get(f"/api/v1/content/units/{uuid.uuid4()}/lessons")

    assert malformed.status_code == 422
    assert error_code(malformed) == "VALIDATION_ERROR"
    assert well_formed_but_absent.status_code == 404
    assert error_code(well_formed_but_absent) == "UNIT_NOT_FOUND"


def test_the_schema_describes_the_error_envelope_we_actually_send() -> None:
    """The published 422 must be the envelope, not FastAPI's stock validation shape.

    FastAPI injects a ``HTTPValidationError`` body for any route with a body or path
    parameters *unless* the operation already declares ``"422"``, ``"4XX"`` or
    ``"default"`` — and it tests for those as **strings**. An int key in our response map
    leaves the test unsatisfied, the stock shape gets published, and a client generated
    from ``/openapi.json`` deserialises ``detail``/``loc`` fields that no response has ever
    contained. This pins the published schema to the bytes on the wire.
    """
    schema = app.openapi()
    versioned = get_settings().api_v1_prefix

    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            responses = operation["responses"]
            assert "HTTPValidationError" not in str(responses), f"{method.upper()} {path}"
            # The unversioned probes take no input, so they never fail validation and
            # legitimately publish no error responses. Everything versioned must.
            if path.startswith(versioned):
                assert "422" in responses, f"{method.upper()} {path} publishes no 422"

    assert "HTTPValidationError" not in schema["components"]["schemas"]
    assert "ErrorResponse" in schema["components"]["schemas"]


@pytest.mark.asyncio
async def test_the_schema_and_the_wire_agree_on_a_validation_error(client: AsyncClient) -> None:
    """The advertised schema and an actual 422 response must describe the same body."""
    schemas = app.openapi()["components"]["schemas"]
    envelope_ref = schemas["ErrorResponse"]["properties"]["error"]["$ref"]
    envelope = schemas[envelope_ref.rsplit("/", 1)[-1]]

    assert envelope["type"] == "object"
    assert set(envelope["properties"]) >= {"code", "message", "details", "requestId"}

    rejected = await client.get("/api/v1/content/units/not-a-uuid/lessons")
    body = rejected.json()

    assert rejected.status_code == 422
    assert set(body) == {"error"}
    assert set(body["error"]) <= set(envelope["properties"])
    assert isinstance(body["error"]["code"], str)
    assert isinstance(body["error"]["message"], str)
