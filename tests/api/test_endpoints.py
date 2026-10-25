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
* the ID-token verifier, injected through ``dependency_overrides``, because the
  production adapter fetches Google's and Apple's published keys over the network and
  a test has no business doing that.

``test_every_published_operation_is_covered`` compares the OpenAPI document against
the operations these tests actually call, so a newly added endpoint fails CI until it
has a test instead of silently shipping untested.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SettingsDep, UowDep, get_storage
from app.core.config import get_settings
from app.core.database import create_all, dispose_engine
from app.core.errors import ErrorCode
from app.core.registry import import_all_models
from app.core.types import ContentState, QuestionKind, SkillKind, UserStatus
from app.main import app
from app.modules.admin.infrastructure.models import AuditLogModel
from app.modules.ai.infrastructure.models import AiUsageRecord
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)
from app.modules.gamification.infrastructure.models import UserStatsModel
from app.modules.quiz.domain.entities import ProgressState, SessionStatus
from app.modules.quiz.domain.review import ReviewState
from app.modules.quiz.infrastructure.models import (
    LessonProgressModel,
    QuizAnswerModel,
    QuizSessionModel,
)
from app.modules.quiz.infrastructure.review_models import ReviewCardModel, ReviewLogModel
from app.modules.users.application.auth_service import AuthService
from app.modules.users.domain.identity import (
    IdentityProvider,
    IdentityVerificationError,
    VerifiedIdentity,
)
from app.modules.users.infrastructure.models import UserModel
from app.modules.users.presentation.routes import auth_service
from app.platform.storage.local import LocalObjectStorage

#: The account these tests sign in as. A test fixture, not a real person or address.
PROBE_EMAIL = "endpoint-probe@natiqo.app"
PROBE_NAME = "Endpoint Probe"
GOOGLE_SUBJECT = "endpoint-probe-google"
APPLE_SUBJECT = "endpoint-probe-apple"

#: The device hint sent with a sign-in, so ``/me/devices`` has a row to return.
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
    ("POST", "/api/v1/auth/google"),
    ("POST", "/api/v1/auth/apple"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/me"),
    ("PATCH", "/api/v1/me"),
    ("DELETE", "/api/v1/me"),
    ("GET", "/api/v1/me/devices"),
    ("DELETE", "/api/v1/me/devices/{device_id}"),
    ("GET", "/api/v1/content/tree"),
    ("GET", "/api/v1/content/lessons/{lesson_id}"),
    ("POST", "/api/v1/quiz/sessions"),
    ("GET", "/api/v1/quiz/sessions/{session_id}"),
    ("POST", "/api/v1/quiz/sessions/{session_id}"),
    ("GET", "/api/v1/progress"),
    ("GET", "/api/v1/progress/unlocks"),
    ("GET", "/api/v1/progress/units"),
    ("GET", "/api/v1/review/due"),
    ("GET", "/api/v1/review/stats"),
    ("GET", "/api/v1/review/preview"),
    ("POST", "/api/v1/review/cards/{card_id}/answer"),
    ("POST", "/api/v1/ai/explain"),
    ("POST", "/api/v1/ai/tutor"),
    ("GET", "/api/v1/ai/tutor/{conversation_id}"),
    ("GET", "/api/v1/ai/quota"),
    ("GET", "/api/v1/gamification/summary"),
    ("GET", "/api/v1/achievements"),
    ("POST", "/api/v1/achievements/{code}/claim"),
    ("GET", "/api/v1/streak"),
    ("POST", "/api/v1/streak/freeze"),
    ("GET", "/api/v1/admin/content/levels"),
    ("POST", "/api/v1/admin/content/levels"),
    ("GET", "/api/v1/admin/content/levels/{level_id}"),
    ("PATCH", "/api/v1/admin/content/levels/{level_id}"),
    ("DELETE", "/api/v1/admin/content/levels/{level_id}"),
    ("GET", "/api/v1/admin/content/levels/{level_id}/units"),
    ("POST", "/api/v1/admin/content/levels/{level_id}/units"),
    ("GET", "/api/v1/admin/content/units/{unit_id}"),
    ("PATCH", "/api/v1/admin/content/units/{unit_id}"),
    ("DELETE", "/api/v1/admin/content/units/{unit_id}"),
    ("GET", "/api/v1/admin/content/units/{unit_id}/lessons"),
    ("POST", "/api/v1/admin/content/units/{unit_id}/lessons"),
    ("GET", "/api/v1/admin/content/lessons/{lesson_id}"),
    ("PATCH", "/api/v1/admin/content/lessons/{lesson_id}"),
    ("DELETE", "/api/v1/admin/content/lessons/{lesson_id}"),
    ("POST", "/api/v1/admin/content/lessons/{lesson_id}/publish"),
    ("POST", "/api/v1/admin/content/lessons/{lesson_id}/questions"),
    ("POST", "/api/v1/admin/content/lessons/{lesson_id}/questions/reorder"),
    ("POST", "/api/v1/admin/content/lessons/{lesson_id}/questions/bulk"),
    ("GET", "/api/v1/admin/content/lessons/{lesson_id}/questions/export"),
    ("POST", "/api/v1/admin/content/lessons/{lesson_id}/questions/import"),
    ("GET", "/api/v1/admin/content/questions/{question_id}"),
    ("POST", "/api/v1/admin/content/questions/{question_id}/duplicate"),
    ("PATCH", "/api/v1/admin/content/questions/{question_id}"),
    ("DELETE", "/api/v1/admin/content/questions/{question_id}"),
    ("GET", "/api/v1/admin/stats/overview"),
    ("GET", "/api/v1/admin/users"),
    ("POST", "/api/v1/admin/users"),
    ("POST", "/api/v1/media/presign"),
    ("POST", "/api/v1/media/confirm"),
    ("GET", "/api/v1/media/{media_id}"),
}


def error_code(response: Response) -> str:
    """Pull the stable error code out of the shared error envelope.

    Asserting on the envelope rather than the message is deliberate: the message is
    written for humans and is expected to change, while ``error.code`` is the
    contract clients are told to branch on.
    """
    return str(response.json()["error"]["code"])


class FakeVerifier:
    """A provider that trusts exactly the tokens a test hands it.

    The real adapters verify a signature against Google's or Apple's published keys and
    are covered by their own tests. What this file is about is the HTTP surface — status
    codes, serialization, auth wiring — and none of that is improved by a network call.
    """

    def __init__(self, provider: IdentityProvider) -> None:
        self.provider = provider
        self._identities: dict[str, VerifiedIdentity] = {}

    def trust(self, token: str, identity: VerifiedIdentity) -> None:
        self._identities[token] = identity

    async def verify(self, id_token: str) -> VerifiedIdentity:
        try:
            return self._identities[id_token]
        except KeyError:
            raise IdentityVerificationError("unknown_token") from None


#: Module-level so the fixture and the ``sign_in`` helper address the same instance.
#: ``trust`` is deterministic for a given token, so no test can affect another's result.
GOOGLE = FakeVerifier(IdentityProvider.GOOGLE)
APPLE = FakeVerifier(IdentityProvider.APPLE)


def identity_for(
    provider: IdentityProvider = IdentityProvider.GOOGLE, **overrides: Any
) -> VerifiedIdentity:
    return VerifiedIdentity(
        **{
            "provider": provider,
            "subject": GOOGLE_SUBJECT if provider is IdentityProvider.GOOGLE else APPLE_SUBJECT,
            "email": PROBE_EMAIL,
            "email_verified": True,
            "display_name": PROBE_NAME,
            **overrides,
        }
    )


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """A real HTTP client against the real app, backed by a fresh in-memory schema.

    ``auth_service`` is overridden only to swap the ID-token verifier. Everything the
    override builds is the production ``AuthService``; the use cases, repositories and
    token handling are untouched.
    """
    import_all_models()
    await create_all()

    def build_service(uow: UowDep, settings: SettingsDep) -> AuthService:
        return AuthService(
            uow,
            settings,
            verifiers={IdentityProvider.GOOGLE: GOOGLE, IdentityProvider.APPLE: APPLE},
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


async def sign_in(
    client: AsyncClient,
    verifier: FakeVerifier | None = None,
    *,
    token: str | None = None,
    device: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    **identity_overrides: Any,
) -> dict[str, Any]:
    """Sign in and return the ``user`` and ``tokens`` blocks.

    The first sign-in for an identity creates the account, so this is also how a test
    gets a learner — there is no separate registration step to fake.
    """
    chosen = verifier or GOOGLE
    # Comfortably over the schema's minimum length, so a test that means to exercise
    # sign-in cannot fail on the shape of its own fixture.
    id_token = token or f"{chosen.provider.value}-id-token-probe"
    chosen.trust(id_token, identity_for(chosen.provider, **identity_overrides))

    response = await client.post(
        f"/api/v1/auth/{chosen.provider.value}",
        json={"id_token": id_token, "device": device or WEB_DEVICE, **(body or {})},
    )
    assert response.status_code == 200, response.text
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
    # Named, not merely counted: the queue probe was added because the worker is a
    # separate process, and a worker nobody can see going down is a worker nobody
    # notices is down.
    assert {check["name"] for check in report["checks"]} >= {"database", "cache", "queue"}


# ================================================================== sign in
@pytest.mark.asyncio
async def test_google_sign_in_creates_the_account_and_issues_a_session(
    client: AsyncClient,
) -> None:
    body = await sign_in(client)

    user = body["user"]
    assert user["email"] == PROBE_EMAIL
    assert user["status"] == "active"
    # The provider vouched for the address, so the account records it as confirmed.
    assert user["emailVerified"] is True
    # Sign-in creates a plain account; becoming an admin is an operator act.
    assert user["isAdmin"] is False
    assert user["profile"]["displayName"] == PROBE_NAME
    assert user["profile"]["currentLevel"], "placement level should be defaulted"

    tokens = body["tokens"]
    assert tokens["tokenType"] == "Bearer"
    assert tokens["accessToken"] and tokens["refreshToken"]
    assert tokens["accessToken"] != tokens["refreshToken"]
    assert tokens["expiresIn"] > 0 and tokens["refreshExpiresIn"] > 0


@pytest.mark.asyncio
async def test_signing_in_again_reuses_the_same_account(client: AsyncClient) -> None:
    first = await sign_in(client)
    second = await sign_in(client)

    assert second["user"]["id"] == first["user"]["id"]
    # A fresh session every time; the account is what must not be duplicated.
    assert second["tokens"]["refreshToken"] != first["tokens"]["refreshToken"]


@pytest.mark.asyncio
async def test_apple_sign_in_links_to_the_same_account_by_verified_email(
    client: AsyncClient,
) -> None:
    """The same learner on two platforms must land on one account."""
    google = await sign_in(client)

    apple = await sign_in(client, APPLE, token="apple-id-token-probe")

    assert apple["user"]["id"] == google["user"]["id"]


@pytest.mark.asyncio
async def test_an_untrusted_token_signs_nobody_in(client: AsyncClient) -> None:
    """A token the verifier has never seen is a token nobody proved they own."""
    response = await client.post(
        "/api/v1/auth/google",
        json={"id_token": "forged-token-nobody-owns", "device": WEB_DEVICE},
    )

    assert response.status_code == 401, response.text
    assert error_code(response) == "AUTH_IDENTITY_INVALID"


@pytest.mark.asyncio
async def test_sign_in_rejects_a_malformed_payload(client: AsyncClient) -> None:
    """Validation happens at the edge, so the service never sees a bad value."""
    response = await client.post("/api/v1/auth/google", json={"id_token": "short"})

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_an_unverified_provider_email_cannot_claim_an_account(
    client: AsyncClient,
) -> None:
    """Otherwise a provider account carrying someone else's address takes it over."""
    await sign_in(client)
    APPLE.trust(
        "unverified-apple-token", identity_for(IdentityProvider.APPLE, email_verified=False)
    )

    response = await client.post(
        "/api/v1/auth/apple",
        json={"id_token": "unverified-apple-token", "device": WEB_DEVICE},
    )

    assert response.status_code == 401, response.text
    assert error_code(response) == "AUTH_IDENTITY_CONFLICT"


@pytest.mark.asyncio
async def test_no_credential_is_accepted_or_stored(client: AsyncClient) -> None:
    """There is no password field left on the wire, so nothing can send one."""
    await sign_in(client)

    response = await client.post(
        "/api/v1/auth/google",
        json={
            "id_token": "anything",
            "email": PROBE_EMAIL,
            "password": "whatever-1234",
            "device": WEB_DEVICE,
        },
    )

    assert response.status_code == 422, response.text


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
    body = await sign_in(client)
    response = await client.get("/api/v1/me", headers=auth_headers(body["tokens"]["accessToken"]))
    assert response.status_code == 200, response.text

    me = response.json()
    assert me["id"] == body["user"]["id"]
    assert me["email"] == PROBE_EMAIL
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
    body = await sign_in(client)
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
    body = await sign_in(client)
    response = await client.patch(
        "/api/v1/me",
        headers=auth_headers(body["tokens"]["accessToken"]),
        json={"current_level": "C2"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_patch_me_rejects_out_of_range_values(client: AsyncClient) -> None:
    body = await sign_in(client)
    response = await client.patch(
        "/api/v1/me",
        headers=auth_headers(body["tokens"]["accessToken"]),
        json={"daily_goal_minutes": 5_000},
    )
    assert response.status_code == 422, response.text


# ===================================================================== devices
@pytest.mark.asyncio
async def test_device_lifecycle_list_then_revoke(client: AsyncClient) -> None:
    body = await sign_in(client)
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
    first = await sign_in(client)
    second = await sign_in(
        client,
        token="second-probe-token",
        subject="second-probe",
        email="second-probe@natiqo.app",
    )

    first_devices = await client.get(
        "/api/v1/me/devices", headers=auth_headers(first["tokens"]["accessToken"])
    )
    device_id = first_devices.json()[0]["id"]

    response = await client.delete(
        f"/api/v1/me/devices/{device_id}",
        headers=auth_headers(second["tokens"]["accessToken"]),
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
    body = await sign_in(client)
    response = await client.delete(
        "/api/v1/me/devices/not-a-uuid",
        headers=auth_headers(body["tokens"]["accessToken"]),
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "VALIDATION_ERROR"


# ====================================================================== tokens
@pytest.mark.asyncio
async def test_refresh_rotates_the_session(client: AsyncClient) -> None:
    body = await sign_in(client)
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
    first = await sign_in(client)
    second = await sign_in(client, device={**WEB_DEVICE, "user_agent": "second-session/1.0"})
    second_tokens = second["tokens"]

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
    first = await sign_in(client)
    second = await sign_in(client)

    response = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": first["tokens"]["refreshToken"], "all_devices": True},
    )
    assert response.status_code == 200, response.text

    for token in (first["tokens"]["refreshToken"], second["tokens"]["refreshToken"]):
        replay = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": token, "device": WEB_DEVICE}
        )
        assert replay.status_code == 401


@pytest.mark.asyncio
async def test_logout_is_idempotent(client: AsyncClient) -> None:
    """A retry after a dropped response must not be told the logout failed."""
    body = await sign_in(client)
    token = body["tokens"]["refreshToken"]
    for _ in range(2):
        response = await client.post("/api/v1/auth/logout", json={"refresh_token": token})
        assert response.status_code == 200, response.text


# ==================================================== removed credential surface
# Every endpoint that existed to create or manage a password is gone. These checks are
# the regression guard: a route reintroduced by accident would restore an authentication
# path this service has deliberately stopped supporting, and nothing else in the suite
# would notice, because no other test asks for these paths at all.


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/forgot-password",
        "/api/v1/auth/reset-password",
        "/api/v1/auth/verify-email",
        "/api/v1/auth/resend-verification",
        "/api/v1/auth/change-password",
    ],
)
@pytest.mark.asyncio
async def test_the_password_endpoints_are_gone(client: AsyncClient, path: str) -> None:
    response = await client.post(path, json={})

    assert response.status_code == 404, response.text


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
# The address is confirmed by the provider, not by a link. There is no token to email,
# no link to expire, and no flow that can be turned into an address-enumeration oracle.


async def test_a_verified_provider_address_is_recorded_as_confirmed(client: AsyncClient) -> None:
    created = await sign_in(client)

    me = await client.get("/api/v1/me", headers=auth_headers(created["tokens"]["accessToken"]))

    assert me.status_code == 200, me.text
    assert me.json()["emailVerified"] is True


async def test_an_unverified_provider_address_still_works(client: AsyncClient) -> None:
    """Confirmation is a nudge the client renders, never a gate on using the app."""
    created = await sign_in(client, token="unverified-google", email_verified=False)

    me = await client.get("/api/v1/me", headers=auth_headers(created["tokens"]["accessToken"]))

    assert me.status_code == 200, me.text
    assert me.json()["emailVerified"] is False


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
    created = await sign_in(client)
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
    created = await sign_in(client)
    await client.delete("/api/v1/me", headers=auth_headers(created["tokens"]["accessToken"]))

    relogin = await client.post(
        "/api/v1/auth/google",
        json={"id_token": "google-id-token-probe", "device": WEB_DEVICE},
    )

    assert relogin.status_code == 401, relogin.text
    assert error_code(relogin) == ErrorCode.USER_DELETED.value


@pytest.mark.asyncio
async def test_deleting_twice_still_answers_the_same_way(client: AsyncClient) -> None:
    """A retried delete must not turn a dropped response into a visible error."""
    created = await sign_in(client)
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
async def test_a_pending_verification_status_is_not_a_gate(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Seeded and pre-feature accounts sit in ``pending_verification``.

    Refusing them would lock out every account created before the status existed, which is
    the opposite of what "the address flag is a nudge, not a gate" means. The status stays
    truthful — it is what ``/me`` reports — and the account keeps working.
    """
    created = await sign_in(client)
    # Written through the ORM rather than raw SQL: SQLite stores this UUID as 32 hex
    # characters with no dashes, so the id the API reports does not match the column and
    # a hand-written UPDATE silently changes zero rows.
    row = await content_db.get(UserModel, uuid.UUID(created["user"]["id"]))
    assert row is not None, "the fixture could not find the account it just created"
    row.status = UserStatus.PENDING_VERIFICATION.value
    await content_db.commit()
    content_db.expunge_all()

    relogin = await client.post(
        "/api/v1/auth/google",
        json={"id_token": "google-id-token-probe", "device": WEB_DEVICE},
    )
    assert relogin.status_code == 200, relogin.text

    me = await client.get(
        "/api/v1/me", headers=auth_headers(relogin.json()["tokens"]["accessToken"])
    )
    assert me.status_code == 200, me.text
    assert me.json()["status"] == "pending_verification"


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
PUBLIC_QUESTION_FIELDS = {
    "id",
    "kind",
    "skill",
    "prompt",
    "choices",
    "points",
    "imageUrl",
    "audioUrl",
    # A clip that teaches the item — a signed sentence, a lip position — is an aid in the
    # same family as the image and the audio, and it is carried for exactly the same reason:
    # it says what is being asked without saying what the answer is.
    "videoUrl",
    # The items a matching question asks the learner to pair up. Public like a multiple
    # choice's options, and for the same reason: it says what is being asked and nothing about
    # what the answer is. The bank they are paired against is the half a shift in order could
    # give away, so it is the half the shared catalogue withholds.
    "prompts",
}

#: Never crosses the wire before an attempt, in either spelling.
#:
#: Both cases matter, and the snake_case half is the one this file got wrong at first: the
#: ORM and the read models name these fields ``correct_index`` and ``accepted_answers``,
#: while everything a client receives is camelCase. Checking only the snake_case names
#: therefore asserts the absence of a key that could never appear — so a real leak through a
#: response schema, which is where a leaked field would be declared, would have passed.
#: ``explanation`` is spelled the same on both sides of the boundary, so it appears once.
ANSWER_KEY_FIELDS = (
    "correctIndex",
    "correct_index",
    "acceptedAnswers",
    "accepted_answers",
    "explanation",
)

#: The admin schemas that are *meant* to carry the answer key: the editor's read model and
#: the two write bodies that author it. They are served only to a caller holding
#: ``isAdmin``, so they are exempt from the public-schema check below. Naming them
#: explicitly keeps the exemption a deliberate act rather than a blanket weakening.
ADMIN_ANSWER_KEY_SCHEMAS = frozenset(
    {
        "QuestionAdminResponse",
        "QuestionCreateRequest",
        "QuestionUpdateRequest",
        # The export/import pair carries the key by design: the package has to be able to
        # rebuild the lesson it came from, and it is only ever served behind the same role
        # gate as the editor's read model.
        "QuestionExportItem",
        "QuestionImportItem",
    }
)

#: The rationale the seeder plants. Its *absence* from a pre-attempt payload is a separate
#: assertion from the key check above: a rationale arriving under a name nobody listed would
#: hand out the answer with the key list still clean.
SEEDED_EXPLANATION = "A greeting."

#: The accepted answer planted on the free-text question. Deliberately a phrase that neither
#: the prompt nor the rationale contains, so that searching a body for it means something.
SEEDED_ACCEPTED_ANSWER = "pleased to meet you"


def _answer_key_keys(payload: Any) -> list[str]:
    """Every answer-key key in a decoded payload, at any depth.

    Recursive on purpose. The leak worth fearing is a nested one — a question inside a page's
    ``items``, a question inside a session's ``questions`` — and a check that reads only the
    top level is exactly the check a future envelope defeats.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ANSWER_KEY_FIELDS:
                found.append(key)
            found.extend(_answer_key_keys(value))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_answer_key_keys(item))
    return found


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
    session: AsyncSession,
    *,
    lessons: int = 2,
    with_explanation: bool = True,
    image_url: str | None = None,
    audio_url: str | None = None,
    video_url: str | None = None,
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
                        image_url=image_url,
                        audio_url=audio_url,
                        video_url=video_url,
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

    response = await client.get("/api/v1/content/tree")

    assert response.status_code == 200
    assert [item["code"] for item in response.json()["items"]] == ["A1", "A2"]


async def test_a_draft_level_is_absent_from_the_tree(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    await seed_catalogue(content_db)

    body = (await client.get("/api/v1/content/tree")).json()

    assert DRAFT_LEVEL_CODE not in [item["code"] for item in body["items"]]


async def test_the_tree_nests_units_and_lessons_with_their_counts(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """One response carries the whole hierarchy: level → units → lessons, each counted."""
    await seed_catalogue(content_db)

    body = (await client.get("/api/v1/content/tree")).json()

    a1 = body["items"][0]
    assert a1["code"] == "A1"
    assert a1["unitCount"] == 1, "the draft unit must not be counted"
    (unit,) = a1["units"]
    assert unit["title"] == "Introductions"
    assert unit["lessonCount"] == 2
    assert [lesson["title"] for lesson in unit["lessons"]] == ["Lesson 1", "Lesson 2"]
    assert [lesson["questionCount"] for lesson in unit["lessons"]] == [1, 1]


async def test_the_tree_covers_every_level_in_one_response(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The tree replaced a paginated walk, so there is nothing left to page through.

    Asserting the shape has no ``meta`` is the point: a client must not be left looking for
    a cursor that no longer exists.
    """
    await seed_catalogue(content_db, lessons=3)

    body = (await client.get("/api/v1/content/tree")).json()

    assert [item["code"] for item in body["items"]] == ["A1", "A2"]
    assert all(len(level["units"]) == 1 for level in body["items"])
    assert all(len(level["units"][0]["lessons"]) == 3 for level in body["items"])
    assert "meta" not in body


async def test_the_lesson_detail_carries_its_questions(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    ids = await seed_catalogue(content_db)

    body = (await client.get(f"/api/v1/content/lessons/{ids['A1-lesson-1']}")).json()

    assert body["title"] == "Lesson 1"
    assert body["questionCount"] == 1
    assert [question["prompt"] for question in body["questions"]] == ["Which one means 'hello'?"]


async def test_the_lesson_detail_carries_the_question_image_aid(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The visual aid is public with the question: it illustrates the taught word
    before an attempt, so unlike the explanation it is not withheld."""
    image = "https://cdn.test/hello.png"
    ids = await seed_catalogue(content_db, image_url=image)

    body = (await client.get(f"/api/v1/content/lessons/{ids['A1-lesson-1']}")).json()

    assert body["questions"][0]["imageUrl"] == image


async def test_a_question_without_an_image_serialises_a_null(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The field is always present — ``null`` when there is no aid — so a client relies
    on one response shape rather than probing for a missing key."""
    ids = await seed_catalogue(content_db)

    body = (await client.get(f"/api/v1/content/lessons/{ids['A1-lesson-1']}")).json()

    assert body["questions"][0]["imageUrl"] is None


async def test_the_lesson_detail_carries_the_question_audio_aid(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The audio aid is public with the question, like the image: it pronounces the
    taught word before an attempt rather than revealing the answer."""
    audio = "https://cdn.test/hello.mp3"
    ids = await seed_catalogue(content_db, audio_url=audio)

    body = (await client.get(f"/api/v1/content/lessons/{ids['A1-lesson-1']}")).json()

    assert body["questions"][0]["audioUrl"] == audio


async def test_the_lesson_detail_carries_the_question_video_aid(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    video = "https://cdn.test/hello.mp4"
    ids = await seed_catalogue(content_db, video_url=video)

    body = (await client.get(f"/api/v1/content/lessons/{ids['A1-lesson-1']}")).json()

    assert body["questions"][0]["videoUrl"] == video


async def test_a_question_without_a_video_serialises_a_null(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    ids = await seed_catalogue(content_db)

    body = (await client.get(f"/api/v1/content/lessons/{ids['A1-lesson-1']}")).json()

    assert body["questions"][0]["videoUrl"] is None


async def test_a_question_without_audio_serialises_a_null(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    ids = await seed_catalogue(content_db)

    body = (await client.get(f"/api/v1/content/lessons/{ids['A1-lesson-1']}")).json()

    assert body["questions"][0]["audioUrl"] is None


async def test_shipped_curriculum_media_is_served_same_origin(client: AsyncClient) -> None:
    """The aids the seed references are served by this app at ``/media``, so a lesson
    never depends on a third-party host. This is the end-to-end proof the mount and the
    stored path agree."""
    response = await client.get("/media/images/a1-greetings-hello-q1.jpg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


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


async def test_an_ordering_question_hides_its_choices_in_the_catalogue(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """An ordering question stores its choices in the answer order, so the shared,
    cacheable catalogue must show none of them. The answerable, shuffled list belongs to
    the quiz session, which has a per-learner seed to reshuffle against."""
    ids = await seed_catalogue(content_db)
    lesson = await content_db.get(LessonModel, ids["A2-lesson-1"])
    assert lesson is not None
    questions = (
        await content_db.scalars(select(QuestionModel).where(QuestionModel.lesson_id == lesson.id))
    ).all()
    questions[0].kind = QuestionKind.ORDERING.value
    questions[0].choices = ["First", "Second", "Third", "Fourth"]
    questions[0].correct_index = None
    questions[0].accepted_answers = []
    await content_db.commit()

    response = await client.get(f"/api/v1/content/lessons/{lesson.id}")
    question = response.json()["questions"][0]

    assert set(question) == PUBLIC_QUESTION_FIELDS
    assert question["kind"] == QuestionKind.ORDERING.value
    assert question["choices"] == []


async def test_a_matching_question_shows_its_prompts_and_hides_its_bank(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Half of a matching question is public and half of it is not.

    The prompts are, for the same reason a multiple choice shows its options: knowing that
    ``Dog`` is to be paired with something says nothing about what. The bank is the half a
    shift in order could give away — the order it is stored in is the order of the pairs — and
    this is a shared, cacheable read with no per-learner seed to shuffle against, so it waits
    for the session. Both halves are asserted here so neither drifts into the other's slot.
    """
    ids = await seed_catalogue(content_db)
    lesson = await content_db.get(LessonModel, ids["A2-lesson-1"])
    assert lesson is not None
    questions = (
        await content_db.scalars(select(QuestionModel).where(QuestionModel.lesson_id == lesson.id))
    ).all()
    questions[0].kind = QuestionKind.MATCHING.value
    questions[0].choices = ["kelb", "qitt", "far"]
    questions[0].correct_index = None
    questions[0].accepted_answers = ["Dog => kelb", "Cat => qitt"]
    await content_db.commit()

    response = await client.get(f"/api/v1/content/lessons/{lesson.id}")
    question = response.json()["questions"][0]

    assert set(question) == PUBLIC_QUESTION_FIELDS
    assert question["kind"] == QuestionKind.MATCHING.value
    assert question["prompts"] == ["Dog", "Cat"]
    assert question["choices"] == []
    for leaked in ANSWER_KEY_FIELDS:
        assert leaked not in question, f"{leaked} would hand out the pairing"


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
    registered = await sign_in(client)
    tokens = registered["tokens"]

    probes = [
        await client.get("/api/v1/content/tree"),
        await client.get(f"/api/v1/content/lessons/{uuid.uuid4()}"),
        await client.get("/health"),
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
    body = {
        "display_name": PROBE_NAME,
        "native_language": "ar",
        "english_variant": "british",
        "timezone": "Asia/Baghdad",
    }

    camel = {camelise(key): value for key, value in body.items()}
    snake = dict(body)
    mixed = dict(camel)
    mixed["displayName"] = body["display_name"]

    # Distinct identities: the three payloads must differ only in spelling, and signing in
    # three times as the same learner would reuse one account and prove nothing about
    # parsing.
    for label, payload in (("camel", camel), ("snake", snake), ("mixed", mixed)):
        email = f"{label}-input@natiqo.app"
        token = f"{label}-input-token"
        GOOGLE.trust(token, identity_for(subject=f"{label}-input", email=email))
        response = await client.post(
            "/api/v1/auth/google", json={"id_token": token, **payload, "device": WEB_DEVICE}
        )
        assert response.status_code == 200, f"{label} input rejected: {response.text}"
        assert response.json()["user"]["email"] == email
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

    first = await client.get("/api/v1/content/tree")
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag, "a cacheable route must return a validator"
    assert "max-age=60" in first.headers["cache-control"]

    again = await client.get("/api/v1/content/tree", headers={"If-None-Match": etag})
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
    stale = await client.get("/api/v1/content/tree")
    current = stale.headers["etag"]

    response = await client.get(
        "/api/v1/content/tree", headers={"If-None-Match": '"a-tag-from-a-future-deploy"'}
    )
    assert response.status_code == 200
    assert response.json() == stale.json()
    assert current  # the current tag is unchanged by this


@pytest.mark.asyncio
async def test_two_different_catalogue_resources_do_not_share_a_validator(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """ETags are per resource. Sharing one would make a client's 304s unsound."""
    ids = await seed_catalogue(content_db)
    tree = await client.get("/api/v1/content/tree")
    lesson = await client.get(f"/api/v1/content/lessons/{ids['A1-lesson-1']}")
    assert tree.headers["etag"] != lesson.headers["etag"]

    # Offering the tree's tag against a lesson must therefore miss.
    crossed = await client.get(
        f"/api/v1/content/lessons/{ids['A1-lesson-1']}",
        headers={"If-None-Match": tree.headers["etag"]},
    )
    assert crossed.status_code == 200


@pytest.mark.asyncio
async def test_a_miss_is_not_cacheable(client: AsyncClient) -> None:
    """A 404 here means "not published", not "never will be".

    Caching it for the freshness window would let an editor publish a lesson and have
    learners keep the miss — the content exists, the phone simply refuses to ask again.
    """
    response = await client.get(f"/api/v1/content/lessons/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers


@pytest.mark.asyncio
async def test_authenticated_data_is_never_cached(client: AsyncClient) -> None:
    """A profile on a shared phone's HTTP cache is a leak waiting for the next user."""
    body = await sign_in(client)
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

    statuses = [(await client.get("/api/v1/content/tree")).status_code for _ in range(5)]

    assert statuses[:3] == [200, 200, 200], "the budget must actually admit traffic"
    assert statuses[3:] == [429, 429]

    blocked = await client.get("/api/v1/content/tree")
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
        "/api/v1/content/tree",
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

    response = await client.get("/api/v1/content/tree/", params={"limit": 1})

    assert response.status_code == 307
    assert response.headers["location"].endswith("/api/v1/content/tree?limit=1"), (
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
    malformed = await client.get("/api/v1/content/lessons/not-a-uuid")
    well_formed_but_absent = await client.get(f"/api/v1/content/lessons/{uuid.uuid4()}")

    assert malformed.status_code == 422
    assert error_code(malformed) == "VALIDATION_ERROR"
    assert well_formed_but_absent.status_code == 404
    assert error_code(well_formed_but_absent) == "LESSON_NOT_FOUND"


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

    rejected = await client.get("/api/v1/content/lessons/not-a-uuid")
    body = rejected.json()

    assert rejected.status_code == 422
    assert set(body) == {"error"}
    assert set(body["error"]) <= set(envelope["properties"])
    assert isinstance(body["error"]["code"], str)
    assert isinstance(body["error"]["message"], str)


# ==================================================================== quiz engine
# The attempt engine's HTTP surface: the status codes, the auth gate, and the one
# contract that matters most — an answer key never reaches a client that has not
# answered. The use cases themselves are exercised in ``tests/integration``.


async def _quiz_context(
    client: AsyncClient, content_db: AsyncSession
) -> tuple[dict[str, str], uuid.UUID]:
    """A signed-in learner and a published lesson for them to attempt."""
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])
    ids = await seed_catalogue(content_db)
    return headers, ids["A1-lesson-1"]


async def _start_quiz(
    client: AsyncClient, headers: dict[str, str], lesson_id: uuid.UUID
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/quiz/sessions",
        json={"mode": "lesson", "lessonId": str(lesson_id)},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_quiz_session_needs_a_token(client: AsyncClient) -> None:
    """A session is account data, so the whole prefix is gated."""
    response = await client.post(
        "/api/v1/quiz/sessions", json={"mode": "lesson", "lessonId": str(uuid.uuid4())}
    )

    assert response.status_code == 401


async def test_starting_a_session_never_returns_the_answer_key(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The whole point of the slice, asserted at the wire layer."""
    headers, lesson_id = await _quiz_context(client, content_db)

    body = await _start_quiz(client, headers, lesson_id)

    assert body["session"]["status"] == "active"
    assert body["session"]["totalCount"] == 1
    assert body["session"]["answeredCount"] == 0
    question = body["questions"][0]
    assert set(question) == PUBLIC_QUESTION_FIELDS | {"position"}
    for leaked in ANSWER_KEY_FIELDS:
        assert leaked not in question


async def test_answering_is_graded_on_the_server(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The verdict, the correct answer and the rationale all come back from the answer."""
    headers, lesson_id = await _quiz_context(client, content_db)
    started = await _start_quiz(client, headers, lesson_id)

    response = await client.post(
        f"/api/v1/quiz/sessions/{started['session']['id']}",
        json={
            "op": "answer",
            "questionId": started["questions"][0]["id"],
            "answer": {"choice": "Hello"},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()["result"]
    assert body["isCorrect"] is True
    assert body["correctAnswer"] == {"choice": "Hello"}
    assert body["explanation"] == "A greeting."
    assert body["pointsAwarded"] == 1
    assert body["isComplete"] is True


async def test_a_correct_retry_earns_half_and_replaces_the_answer(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A missed question may be answered again, and only the score changes.

    The seeded question is worth one point, so half rounds down to nothing — the documented
    consequence of the halving rule, and the sharpest form of "the first try is the one that
    pays". Just as important is that the retry lands on the *same* answer: ``answeredCount``
    stays at one, so a session cannot be inflated by trying again.
    """
    headers, lesson_id = await _quiz_context(client, content_db)
    started = await _start_quiz(client, headers, lesson_id)
    session_id = started["session"]["id"]
    question_id = started["questions"][0]["id"]
    wrong = await client.post(
        f"/api/v1/quiz/sessions/{session_id}",
        json={"op": "answer", "questionId": question_id, "answer": {"choice": "Goodbye"}},
        headers=headers,
    )
    assert wrong.status_code == 200, wrong.text
    assert wrong.json()["result"]["pointsAwarded"] == 0

    retry = await client.post(
        f"/api/v1/quiz/sessions/{session_id}",
        json={"op": "answer", "questionId": question_id, "answer": {"choice": "Hello"}},
        headers=headers,
    )

    assert retry.status_code == 200, retry.text
    body = retry.json()["result"]
    assert body["isCorrect"] is True
    assert body["pointsAwarded"] == 0  # half of one point, rounded down
    assert body["correctAnswer"] == {"choice": "Hello"}
    assert body["answeredCount"] == 1
    assert body["correctCount"] == 1


async def test_a_question_answered_correctly_is_closed(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Only a *wrong* answer re-opens a question; a right one is settled for good."""
    headers, lesson_id = await _quiz_context(client, content_db)
    started = await _start_quiz(client, headers, lesson_id)
    session_id = started["session"]["id"]
    question_id = started["questions"][0]["id"]
    first = await client.post(
        f"/api/v1/quiz/sessions/{session_id}",
        json={"op": "answer", "questionId": question_id, "answer": {"choice": "Hello"}},
        headers=headers,
    )
    assert first.status_code == 200, first.text

    response = await client.post(
        f"/api/v1/quiz/sessions/{session_id}",
        json={"op": "answer", "questionId": question_id, "answer": {"choice": "Hello"}},
        headers=headers,
    )

    assert response.status_code == 409
    assert error_code(response) == "QUESTION_ALREADY_ANSWERED"


async def test_resuming_a_session_reports_its_state(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    headers, lesson_id = await _quiz_context(client, content_db)
    started = await _start_quiz(client, headers, lesson_id)

    response = await client.get(
        f"/api/v1/quiz/sessions/{started['session']['id']}", headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["session"]["answeredCount"] == 0


async def test_completing_a_session_scores_it(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    headers, lesson_id = await _quiz_context(client, content_db)
    started = await _start_quiz(client, headers, lesson_id)
    session_id = started["session"]["id"]
    await client.post(
        f"/api/v1/quiz/sessions/{session_id}",
        json={
            "op": "answer",
            "questionId": started["questions"][0]["id"],
            "answer": {"choice": "Hello"},
        },
        headers=headers,
    )

    response = await client.post(
        f"/api/v1/quiz/sessions/{session_id}", json={"op": "complete"}, headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()["result"]
    assert body["scorePercent"] == 100
    assert body["session"]["status"] == "completed"


async def test_abandoning_a_session_ends_it(client: AsyncClient, content_db: AsyncSession) -> None:
    headers, lesson_id = await _quiz_context(client, content_db)
    started = await _start_quiz(client, headers, lesson_id)

    response = await client.post(
        f"/api/v1/quiz/sessions/{started['session']['id']}",
        json={"op": "abandon"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["status"] == "abandoned"


async def test_an_unknown_session_is_not_found(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    headers, _ = await _quiz_context(client, content_db)

    response = await client.get(f"/api/v1/quiz/sessions/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404
    assert error_code(response) == "QUIZ_SESSION_NOT_FOUND"


async def test_a_multi_select_answer_is_graded_over_the_wire(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The multi-select contract end to end: choices in, a verdict and the key out.

    Pins the one new request field (``answer.choices``) at the wire layer, where an
    ``extra="forbid"`` schema would otherwise reject it as a 422 rather than grade it.
    """
    headers, lesson_id = await _quiz_context(client, content_db)
    question = QuestionModel(
        id=uuid.uuid4(),
        lesson_id=lesson_id,
        position=2,
        kind=QuestionKind.MULTI_SELECT.value,
        skill=SkillKind.VOCABULARY.value,
        prompt="Which of these can greet someone?",
        choices=["Hello", "Good morning", "Goodbye"],
        accepted_answers=["Hello", "Good morning"],
        correct_index=None,
        explanation="Both are greetings.",
        points=2,
    )
    content_db.add(question)
    await content_db.commit()

    started = await _start_quiz(client, headers, lesson_id)
    session_id = started["session"]["id"]
    served = next(q for q in started["questions"] if q["id"] == str(question.id))
    assert served["kind"] == "multi_select"
    for leaked in ANSWER_KEY_FIELDS:
        assert leaked not in served

    response = await client.post(
        f"/api/v1/quiz/sessions/{session_id}",
        json={
            "op": "answer",
            "questionId": str(question.id),
            "answer": {"choices": ["Hello", "Good morning"]},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isCorrect"] is True
    assert result["pointsAwarded"] == 2
    assert set(result["correctAnswer"]["choices"].split(", ")) == {"Hello", "Good morning"}


async def test_an_ordering_answer_is_graded_over_the_wire(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The ordering contract end to end: a sequence in, a verdict and the key out.

    Pins the one new request field (``answer.order``) at the wire layer, and the invariant
    that the served sequence is a de-arrangement of the answer — never the answer itself.
    """
    headers, lesson_id = await _quiz_context(client, content_db)
    authored = ["Hello!", "How are you?", "I am fine.", "Goodbye!"]
    question = QuestionModel(
        id=uuid.uuid4(),
        lesson_id=lesson_id,
        position=3,
        kind=QuestionKind.ORDERING.value,
        skill=SkillKind.READING.value,
        prompt="Put the lines of the greeting in order.",
        choices=list(authored),
        accepted_answers=[],
        correct_index=None,
        explanation="A greeting opens, asks, answers, then closes.",
        points=2,
    )
    content_db.add(question)
    await content_db.commit()

    started = await _start_quiz(client, headers, lesson_id)
    session_id = started["session"]["id"]
    served = next(q for q in started["questions"] if q["id"] == str(question.id))
    assert served["kind"] == "ordering"
    assert sorted(served["choices"]) == sorted(authored)
    assert served["choices"] != authored
    for leaked in ANSWER_KEY_FIELDS:
        assert leaked not in served

    response = await client.post(
        f"/api/v1/quiz/sessions/{session_id}",
        json={
            "op": "answer",
            "questionId": str(question.id),
            "answer": {"order": authored},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isCorrect"] is True
    assert result["pointsAwarded"] == 2
    assert result["correctAnswer"]["order"] == "Hello! → How are you? → I am fine. → Goodbye!"


#: A matching question's pairs, as a wrong answer and as the key. Declared once so the tests
#: below cannot disagree about what the question is while agreeing about its shape.
_MATCHING_PAIRS = (("Monday", "1"), ("Tuesday", "2"), ("Wednesday", "3"))


def _matching_question(lesson_id: uuid.UUID, *, position: int) -> QuestionModel:
    """A matching row as the seed would write it: pairs stored, ``choices`` as the bank."""
    return QuestionModel(
        id=uuid.uuid4(),
        lesson_id=lesson_id,
        position=position,
        kind=QuestionKind.MATCHING.value,
        skill=SkillKind.VOCABULARY.value,
        prompt="Match each day to its place in the week.",
        choices=["1", "2", "3", "4"],
        accepted_answers=[f"{left} => {right}" for left, right in _MATCHING_PAIRS],
        correct_index=None,
        explanation="The week starts on Monday.",
        points=2,
    )


async def test_a_matching_answer_is_graded_over_the_wire(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The matching contract end to end: a set of pairs in, a verdict and the key out.

    Pins the two new surface fields — ``prompts`` on the served question and
    ``answer.matching`` on the request — and the invariant that keeps the pairing server-side:
    the bank is reordered so that no option stands opposite the prompt it answers, which is
    the arrangement the authored order would produce.
    """
    headers, lesson_id = await _quiz_context(client, content_db)
    question = _matching_question(lesson_id, position=4)
    content_db.add(question)
    await content_db.commit()

    started = await _start_quiz(client, headers, lesson_id)
    session_id = started["session"]["id"]
    served = next(q for q in started["questions"] if q["id"] == str(question.id))
    assert served["kind"] == "matching"
    assert served["prompts"] == [left for left, _ in _MATCHING_PAIRS]
    assert sorted(served["choices"]) == ["1", "2", "3", "4"]
    assert all(
        served["choices"][position] != dict(_MATCHING_PAIRS)[prompt]
        for position, prompt in enumerate(served["prompts"])
    )
    for leaked in ANSWER_KEY_FIELDS:
        assert leaked not in served

    response = await client.post(
        f"/api/v1/quiz/sessions/{session_id}",
        json={
            "op": "answer",
            "questionId": str(question.id),
            "answer": {"matching": [f"{left} => {right}" for left, right in _MATCHING_PAIRS]},
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isCorrect"] is True
    assert result["pointsAwarded"] == 2
    assert result["correctAnswer"]["pairs"] == "Monday ⇒ 1; Tuesday ⇒ 2; Wednesday ⇒ 3"


async def test_a_wrong_matching_pairing_is_wrong_and_a_retry_is_worth_half(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Every prompt is answered with a real option; the pairings are simply wrong.

    That is an answer, not a malformed request — the distinction the grading rule exists to
    make — and the retry that follows it is the one place the halved-points rule can be seen
    over the wire for this kind.
    """
    headers, lesson_id = await _quiz_context(client, content_db)
    question = _matching_question(lesson_id, position=4)
    content_db.add(question)
    await content_db.commit()

    started = await _start_quiz(client, headers, lesson_id)
    session_id = started["session"]["id"]
    url = f"/api/v1/quiz/sessions/{session_id}"

    wrong = await client.post(
        url,
        json={
            "op": "answer",
            "questionId": str(question.id),
            "answer": {"matching": ["Monday => 2", "Tuesday => 1", "Wednesday => 3"]},
        },
        headers=headers,
    )

    assert wrong.status_code == 200, wrong.text
    assert wrong.json()["result"]["isCorrect"] is False
    assert wrong.json()["result"]["pointsAwarded"] == 0

    retry = await client.post(
        url,
        json={
            "op": "answer",
            "questionId": str(question.id),
            "answer": {"matching": [f"{left} => {right}" for left, right in _MATCHING_PAIRS]},
        },
        headers=headers,
    )

    assert retry.status_code == 200, retry.text
    assert retry.json()["result"]["isCorrect"] is True
    assert retry.json()["result"]["pointsAwarded"] == 1  # half of the question's 2


async def test_a_matching_submission_the_payload_could_not_produce_is_rejected(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """An entry with no arrow, and a prompt this question does not have: both are 422s."""
    headers, lesson_id = await _quiz_context(client, content_db)
    question = _matching_question(lesson_id, position=4)
    content_db.add(question)
    await content_db.commit()

    started = await _start_quiz(client, headers, lesson_id)
    session_id = started["session"]["id"]
    url = f"/api/v1/quiz/sessions/{session_id}"

    for label, pairs in (
        ("no arrow", ["Monday 1", "Tuesday => 2", "Wednesday => 3"]),
        ("unknown prompt", ["Monday => 1", "Tuesday => 2", "Friday => 3"]),
        ("incomplete", ["Monday => 1", "Tuesday => 2"]),
    ):
        response = await client.post(
            url,
            json={
                "op": "answer",
                "questionId": str(question.id),
                "answer": {"matching": pairs},
            },
            headers=headers,
        )

        assert response.status_code == 422, f"{label}: {response.text}"


async def test_a_mis_ordered_ordering_answer_is_rejected_over_the_wire(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A sequence with a text that is not part of the question is a 422, not a wrong."""
    headers, lesson_id = await _quiz_context(client, content_db)
    question = QuestionModel(
        id=uuid.uuid4(),
        lesson_id=lesson_id,
        position=3,
        kind=QuestionKind.ORDERING.value,
        skill=SkillKind.READING.value,
        prompt="Put the lines of the greeting in order.",
        choices=["Hello!", "How are you?", "I am fine.", "Goodbye!"],
        accepted_answers=[],
        correct_index=None,
        explanation="A greeting opens, asks, answers, then closes.",
        points=2,
    )
    content_db.add(question)
    await content_db.commit()

    started = await _start_quiz(client, headers, lesson_id)
    session_id = started["session"]["id"]

    response = await client.post(
        f"/api/v1/quiz/sessions/{session_id}",
        json={
            "op": "answer",
            "questionId": str(question.id),
            "answer": {"order": ["Hello!", "How are you?", "I am fine.", "Nonsense!"]},
        },
        headers=headers,
    )

    assert response.status_code == 422, response.text


# =========================================== the answer key never crosses the wire before
# The invariant this section exists for: until a learner has committed to an answer, no
# endpoint may hand out ``correctIndex``, ``acceptedAnswers`` or ``explanation`` — for any
# question, through any route, at any nesting depth. It is stated once here and checked
# against the whole surface a client can read, rather than one route at a time, because the
# question being asked is "is there *any* way to read the answer key early?" and only the
# whole surface answers it.
#
# Three things make the check evidence rather than aspiration:
#
# * the sweeps walk every read available before an answer — the public catalogue, the session
#   a client opens, and the session it re-opens — scanning each decoded payload recursively;
# * the fixture is asserted to carry a real answer key, so "found nothing" cannot mean
#   "there was nothing to find";
# * the *values* are searched for as well as the keys, because the rationale is a sentence,
#   and the failure worth catching is that sentence arriving under some other name.


async def _question_of(session: AsyncSession, lesson_id: uuid.UUID) -> QuestionModel:
    """The one seeded question of a lesson."""
    return (
        await session.scalars(select(QuestionModel).where(QuestionModel.lesson_id == lesson_id))
    ).one()


async def _seed_catalogue_with_a_key(session: AsyncSession) -> dict[str, uuid.UUID]:
    """The seeder, plus a free-text question whose accepted answer is a real phrase.

    Both gradeable shapes have to be present with a populated key. A multiple-choice row
    stores ``accepted_answers = []`` by nature, so a sweep seeded with only that kind could
    not fail the accepted-answer half of the check even if the field were leaking.
    """
    ids = await seed_catalogue(session)
    question = await _question_of(session, ids["A2-lesson-1"])
    question.kind = QuestionKind.FILL_BLANK.value
    question.choices = []
    question.correct_index = None
    question.accepted_answers = [SEEDED_ACCEPTED_ANSWER]
    await session.commit()
    return ids


async def test_the_seed_really_carries_an_answer_key(content_db: AsyncSession) -> None:
    """Stops the sweeps below from passing vacuously.

    "Nothing leaked" is only evidence if there was a key to leak. A renamed column or an
    emptied seeder would leave every assertion in this section green and worthless, so the
    fixture's own answer key — an index, a rationale and an accepted phrase — is asserted
    first.
    """
    ids = await _seed_catalogue_with_a_key(content_db)

    multiple_choice = await _question_of(content_db, ids["A1-lesson-1"])
    free_text = await _question_of(content_db, ids["A2-lesson-1"])

    assert multiple_choice.correct_index == 0
    assert multiple_choice.explanation == SEEDED_EXPLANATION
    assert free_text.accepted_answers == [SEEDED_ACCEPTED_ANSWER]


async def test_no_pre_attempt_read_carries_the_answer_key(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Every public read available before an answer, checked as one surface.

    A list rather than a test per route, because a *new* route has to be a deliberate
    omission from this list rather than an unnoticed one: adding a question to a payload
    somewhere in the catalogue fails here without anyone having to remember to look.
    """
    ids = await _seed_catalogue_with_a_key(content_db)
    reads = (
        ("catalogue tree", "/api/v1/content/tree"),
        ("lesson detail", f"/api/v1/content/lessons/{ids['A1-lesson-1']}"),
        ("free-text lesson detail", f"/api/v1/content/lessons/{ids['A2-lesson-1']}"),
    )

    for label, path in reads:
        response = await client.get(path)

        assert response.status_code == 200, f"{label}: {response.status_code} {response.text}"
        assert _answer_key_keys(response.json()) == [], f"{label} leaked an answer key"
        assert SEEDED_EXPLANATION not in response.text, f"{label} quoted the rationale"
        assert SEEDED_ACCEPTED_ANSWER not in response.text, f"{label} quoted the accepted answer"


async def test_no_session_read_before_an_answer_carries_the_answer_key(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The two authenticated reads a client makes before answering: open, then re-open.

    A session serves the same questions the catalogue does through a different projection,
    so it is checked separately — they are two places that can each leak on their own, and
    the session payload is the one a client holds while the answer is still open.
    """
    headers, lesson_id = await _quiz_context(client, content_db)
    started = await client.post(
        "/api/v1/quiz/sessions",
        json={"mode": "lesson", "lessonId": str(lesson_id)},
        headers=headers,
    )
    assert started.status_code == 201, started.text

    resumed = await client.get(
        f"/api/v1/quiz/sessions/{started.json()['session']['id']}", headers=headers
    )
    assert resumed.status_code == 200, resumed.text

    for label, response in (("session start", started), ("session resume", resumed)):
        assert _answer_key_keys(response.json()) == [], f"{label} leaked an answer key"
        assert SEEDED_EXPLANATION not in response.text, f"{label} quoted the rationale"


def test_the_published_schema_declares_no_pre_attempt_answer_key_field() -> None:
    """The spec is part of the public surface: clients are generated from it.

    ``/openapi.json`` is unauthenticated, so a schema declaring ``correctIndex`` would
    advertise the field to every caller and every generated client whether or not a response
    ever sets it. The two pre-attempt question schemas are checked by name, and the two names
    that never belong on the wire are checked across every schema in the document.

    ``QuestionAdminResponse`` and the two admin question bodies are the deliberate
    exception: they are served only behind the ``isAdmin`` gate, and the person
    fixing a mis-keyed question is exactly who needs the key. Every learner-facing schema must
    still declare neither field, so the allowlist names the exception rather than weakening
    the check.
    """
    schemas = app.openapi()["components"]["schemas"]

    for name in ("QuestionResponse", "SessionQuestionResponse"):
        leaked = set(schemas[name]["properties"]) & set(ANSWER_KEY_FIELDS)
        assert not leaked, f"{name} declares {sorted(leaked)}"

    for name, schema in schemas.items():
        if name in ADMIN_ANSWER_KEY_SCHEMAS:
            continue
        rendered = json.dumps(schema)
        for never_public in ("correctIndex", "acceptedAnswers"):
            assert never_public not in rendered, f"{name} declares {never_public}"


# ==================================================================== progress
# The read-only progress surface: the auth gate, the wire shape, and the property that
# ties it to the attempt engine — every figure is restated from a session the learner
# actually finished. The use cases are exercised in ``tests/integration``; what is pinned
# here is the HTTP contract a mobile client is generated from.


async def _learner_with_catalogue(
    client: AsyncClient, content_db: AsyncSession
) -> tuple[dict[str, str], dict[str, uuid.UUID]]:
    """A signed-in learner and the seeded published curriculum."""
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])
    ids = await seed_catalogue(content_db)
    return headers, ids


async def _complete_a_lesson(
    client: AsyncClient, headers: dict[str, str], lesson_id: uuid.UUID
) -> uuid.UUID:
    """Attempt a lesson end to end and return the completed session's id.

    Driven through the real endpoints rather than seeded rows: the point of these tests is
    that the progress a client reads is the progress the engine wrote.
    """
    started = await _start_quiz(client, headers, lesson_id)
    session_id = started["session"]["id"]
    for question in started["questions"]:
        answered = await client.post(
            f"/api/v1/quiz/sessions/{session_id}",
            json={"op": "answer", "questionId": question["id"], "answer": {"choice": "Hello"}},
            headers=headers,
        )
        assert answered.status_code == 200, answered.text
    completed = await client.post(
        f"/api/v1/quiz/sessions/{session_id}", json={"op": "complete"}, headers=headers
    )
    assert completed.status_code == 200, completed.text
    return uuid.UUID(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/progress",
        "/api/v1/progress/unlocks",
        "/api/v1/progress/units",
    ],
)
async def test_progress_reads_need_a_token(client: AsyncClient, path: str) -> None:
    """Progress is account data, so the whole prefix is gated like ``/me``."""
    response = await client.get(path)

    assert response.status_code == 401, response.text
    assert error_code(response) == "AUTH_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_a_brand_new_learner_has_an_all_zero_summary(client: AsyncClient) -> None:
    """The zeros are derived from an empty record, not defaulted into place."""
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    response = await client.get("/api/v1/progress", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "totalXp": 0,
        "lessonsCompleted": 0,
        "lessonsInProgress": 0,
        "lessonsStarted": 0,
        "correctCount": 0,
        "answeredCount": 0,
        "accuracyPercent": 0,
        "currentStreakDays": 0,
        "longestStreakDays": 0,
        "lastActivityAt": None,
        "xp": {"items": [], "meta": {"limit": 20, "hasMore": False, "nextCursor": None}},
    }


@pytest.mark.asyncio
async def test_progress_responses_are_never_cached(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A progress body on a shared phone's HTTP cache is the next user's problem."""
    headers, _ = await _learner_with_catalogue(client, content_db)

    response = await client.get("/api/v1/progress", headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_the_unlock_map_lists_the_published_curriculum_in_order(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Every published lesson, in curriculum order, with the drafts gone."""
    headers, _ = await _learner_with_catalogue(client, content_db)

    response = await client.get("/api/v1/progress/unlocks", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4
    assert [item["levelCode"] for item in body["items"]] == ["A1", "A1", "A2", "A2"]
    assert [item["title"] for item in body["items"]] == [
        "Lesson 1",
        "Lesson 2",
        "Lesson 1",
        "Lesson 2",
    ]
    assert all(item["state"] == "available" for item in body["items"])
    # The sequential rule: the first unit and first lesson are open, subsequent lessons
    # are locked until predecessor lessons are completed, and subsequent units until checkpoint.
    assert [item["locked"] for item in body["items"]] == [False, True, True, True]
    assert [item["isUnlocked"] for item in body["items"]] == [True, False, False, False]
    assert [item["isCompleted"] for item in body["items"]] == [False, False, False, False]


@pytest.mark.asyncio
async def test_a_lesson_with_no_record_reads_available_in_the_unlock_map(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    headers, ids = await _learner_with_catalogue(client, content_db)

    body = (await client.get("/api/v1/progress/unlocks", headers=headers)).json()

    first = next(item for item in body["items"] if item["lessonId"] == str(ids["A1-lesson-1"]))
    assert first["levelCode"] == "A1"
    assert first["title"] == "Lesson 1"
    assert first["state"] == "available"
    assert first["locked"] is False
    assert first["isUnlocked"] is True
    assert first["isCompleted"] is False
    assert (first["bestScore"], first["attempts"], first["xpEarned"]) == (0, 0, 0)
    assert first["completedAt"] is None


@pytest.mark.asyncio
async def test_an_unknown_lesson_is_absent_from_the_unlock_map(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The map lists the curriculum; a lesson that is not in it simply is not there."""
    headers, _ = await _learner_with_catalogue(client, content_db)

    body = (await client.get("/api/v1/progress/unlocks", headers=headers)).json()

    assert str(uuid.uuid4()) not in {item["lessonId"] for item in body["items"]}


@pytest.mark.asyncio
async def test_a_draft_lesson_is_absent_from_the_unlock_map(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Draft visibility is inherited, so a lesson under an unpublished unit never appears."""
    headers, ids = await _learner_with_catalogue(client, content_db)
    draft = LessonModel(
        id=uuid.uuid4(),
        unit_id=ids["A1-unit"],
        title="Draft",
        summary="Not ready.",
        position=50,
        estimated_minutes=5,
        state=ContentState.REVIEW.value,
    )
    content_db.add(draft)
    await content_db.commit()

    body = (await client.get("/api/v1/progress/unlocks", headers=headers)).json()

    assert str(draft.id) not in {item["lessonId"] for item in body["items"]}
    assert body["total"] == 4, "the draft must not be counted"


async def _pass_the_first_unit(
    client: AsyncClient, headers: dict[str, str], ids: dict[str, uuid.UUID]
) -> None:
    """Finish both A1 lessons, which is what makes its checkpoint ready to sit."""
    await _complete_a_lesson(client, headers, ids["A1-lesson-1"])
    await _complete_a_lesson(client, headers, ids["A1-lesson-2"])


async def _sit_the_first_checkpoint(
    client: AsyncClient, headers: dict[str, str], ids: dict[str, uuid.UUID], choice: str
) -> dict[str, Any]:
    """Open the A1 checkpoint, answer every question with ``choice``, and complete it."""
    started = await client.post(
        "/api/v1/quiz/sessions",
        json={"mode": "checkpoint", "unitId": str(ids["A1-unit"])},
        headers=headers,
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session"]["id"]
    for question in started.json()["questions"]:
        answered = await client.post(
            f"/api/v1/quiz/sessions/{session_id}",
            json={"op": "answer", "questionId": question["id"], "answer": {"choice": choice}},
            headers=headers,
        )
        assert answered.status_code == 200, answered.text
    completed = await client.post(
        f"/api/v1/quiz/sessions/{session_id}", json={"op": "complete"}, headers=headers
    )
    assert completed.status_code == 200, completed.text
    return completed.json()["result"]


@pytest.mark.asyncio
async def test_the_units_read_lists_each_unit_with_its_checkpoint_standing(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The unit list: the curriculum in order, the lock, and how the checkpoint stands."""
    headers, ids = await _learner_with_catalogue(client, content_db)

    body = (await client.get("/api/v1/progress/units", headers=headers)).json()

    assert body["total"] == 2
    assert [item["levelCode"] for item in body["items"]] == ["A1", "A2"]
    assert [item["title"] for item in body["items"]] == ["Introductions", "Introductions"]
    first, second = body["items"]
    assert first["unitId"] == str(ids["A1-unit"])
    assert (first["locked"], first["passed"]) == (False, False)
    assert (first["lessonsTotal"], first["lessonsCompleted"]) == (2, 0)
    assert (first["checkpointReady"], first["bestScore"], first["attempts"]) == (False, 0, 0)
    assert first["passedAt"] is None
    # The sequential rule: the second unit is shut until the first is passed.
    assert (second["locked"], second["checkpointReady"]) == (True, False)


@pytest.mark.asyncio
async def test_a_checkpoint_becomes_ready_only_when_every_lesson_is_finished(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """``lessonsCompleted`` counts up and ``checkpointReady`` flips at the last lesson."""
    headers, ids = await _learner_with_catalogue(client, content_db)
    await _complete_a_lesson(client, headers, ids["A1-lesson-1"])

    after_one = (await client.get("/api/v1/progress/units", headers=headers)).json()["items"][0]
    assert (after_one["lessonsCompleted"], after_one["checkpointReady"]) == (1, False)

    await _complete_a_lesson(client, headers, ids["A1-lesson-2"])

    after_both = (await client.get("/api/v1/progress/units", headers=headers)).json()["items"][0]
    assert (after_both["lessonsCompleted"], after_both["checkpointReady"]) == (2, True)


@pytest.mark.asyncio
async def test_a_locked_units_lesson_cannot_be_started(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The hard lock: the second unit's lessons are refused while the first is unpassed."""
    headers, ids = await _learner_with_catalogue(client, content_db)

    response = await client.post(
        "/api/v1/quiz/sessions",
        json={"mode": "lesson", "lessonId": str(ids["A2-lesson-1"])},
        headers=headers,
    )

    assert response.status_code == 403, response.text
    assert error_code(response) == "LESSON_LOCKED"


@pytest.mark.asyncio
async def test_a_locked_lesson_cannot_be_started_before_previous_lesson_is_completed(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The sequential lesson lock: lesson 2 is refused while lesson 1 is uncompleted."""
    headers, ids = await _learner_with_catalogue(client, content_db)

    response = await client.post(
        "/api/v1/quiz/sessions",
        json={"mode": "lesson", "lessonId": str(ids["A1-lesson-2"])},
        headers=headers,
    )

    assert response.status_code == 403, response.text
    assert error_code(response) == "LESSON_LOCKED"


@pytest.mark.asyncio
async def test_a_checkpoint_is_refused_until_the_lessons_are_done(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Opening the checkpoint early is a conflict, reported with how much is left."""
    headers, ids = await _learner_with_catalogue(client, content_db)
    await _complete_a_lesson(client, headers, ids["A1-lesson-1"])

    response = await client.post(
        "/api/v1/quiz/sessions",
        json={"mode": "checkpoint", "unitId": str(ids["A1-unit"])},
        headers=headers,
    )

    assert response.status_code == 409, response.text
    assert error_code(response) == "CHECKPOINT_NOT_READY"
    assert response.json()["error"]["details"]["lessons_remaining"] == 1


@pytest.mark.asyncio
async def test_a_checkpoint_on_a_locked_unit_is_refused(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A shut unit's checkpoint is a lock error, before any readiness is even considered."""
    headers, ids = await _learner_with_catalogue(client, content_db)

    response = await client.post(
        "/api/v1/quiz/sessions",
        json={"mode": "checkpoint", "unitId": str(ids["A2-unit"])},
        headers=headers,
    )

    assert response.status_code == 403, response.text
    assert error_code(response) == "LESSON_LOCKED"


@pytest.mark.asyncio
async def test_passing_a_checkpoint_unlocks_the_next_unit(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A pass records the unit and opens the next one — on the unit list and the map."""
    headers, ids = await _learner_with_catalogue(client, content_db)
    await _pass_the_first_unit(client, headers, ids)

    result = await _sit_the_first_checkpoint(client, headers, ids, "Hello")

    assert (result["passed"], result["scorePercent"]) == (True, 100)
    assert result["unlockedUnitId"] == str(ids["A2-unit"])
    assert result["correctQuestionIds"], "a full pass names the questions that were right"
    assert result["missedQuestionIds"] == []
    assert result["retryAvailable"] is False

    units = (await client.get("/api/v1/progress/units", headers=headers)).json()["items"]
    first, second = units
    assert (first["passed"], first["bestScore"], first["attempts"]) == (True, 100, 1)
    assert first["passedAt"] is not None
    assert second["locked"] is False, "passing A1 must open A2"

    unlocks = (await client.get("/api/v1/progress/unlocks", headers=headers)).json()
    a2 = [item for item in unlocks["items"] if item["levelCode"] == "A2"]
    assert [item["locked"] for item in a2] == [False, True]
    assert [item["isUnlocked"] for item in a2] == [True, False]

    # A checkpoint is a gate, not practice: it adds a completion to the feed but no XP, so
    # the learner's total is still exactly the two points the two lessons paid.
    progress = (await client.get("/api/v1/progress", headers=headers)).json()
    assert progress["totalXp"] == 2
    checkpoint_row = next(row for row in progress["xp"]["items"] if row["lessonId"] is None)
    assert checkpoint_row["unitId"] == str(ids["A1-unit"])
    assert checkpoint_row["xpAwarded"] == 0


@pytest.mark.asyncio
async def test_failing_a_checkpoint_leaves_the_next_unit_shut(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A miss keeps the unit unpassed, names what to review, and leaves the gate closed."""
    headers, ids = await _learner_with_catalogue(client, content_db)
    await _pass_the_first_unit(client, headers, ids)

    result = await _sit_the_first_checkpoint(client, headers, ids, "Goodbye")

    assert result["passed"] is False
    assert result["unlockedUnitId"] is None
    # A checkpoint seals its questions, so there is no in-sitting retry to offer: the
    # learner re-sits the gate from the start, and only the new sitting is re-answerable.
    assert result["retryAvailable"] is False
    assert result["correctQuestionIds"] == []
    assert len(result["missedQuestionIds"]) == result["session"]["totalCount"]

    units = (await client.get("/api/v1/progress/units", headers=headers)).json()["items"]
    assert (units[0]["passed"], units[0]["attempts"]) == (False, 1)
    assert units[1]["locked"] is True, "a failed checkpoint must not open the next unit"


@pytest.mark.asyncio
async def test_a_checkpoint_seals_each_question_on_its_first_answer(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A graded gate cannot be revised: the second answer is refused, right or wrong.

    The seal is what makes the 80% bar mean something. If a missed question could be
    re-answered in the same sitting, the score the gate reads would be the result of
    unlimited attempts rather than of one.
    """
    headers, ids = await _learner_with_catalogue(client, content_db)
    await _pass_the_first_unit(client, headers, ids)
    started = await client.post(
        "/api/v1/quiz/sessions",
        json={"mode": "checkpoint", "unitId": str(ids["A1-unit"])},
        headers=headers,
    )
    assert started.status_code == 201, started.text
    session_id = started.json()["session"]["id"]
    question = started.json()["questions"][0]
    answer = {"op": "answer", "questionId": question["id"], "answer": {"choice": "Goodbye"}}

    graded = await client.post(f"/api/v1/quiz/sessions/{session_id}", json=answer, headers=headers)
    assert graded.status_code == 200, graded.text
    assert graded.json()["result"]["isCorrect"] is False

    refused = await client.post(f"/api/v1/quiz/sessions/{session_id}", json=answer, headers=headers)
    assert refused.status_code == 409, refused.text
    assert error_code(refused) == "QUESTION_ALREADY_ANSWERED"


@pytest.mark.asyncio
async def test_replaying_a_finished_lesson_awards_no_xp(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """XP is for the first completion; a replay is recorded but pays nothing.

    Without this the ledger that backs the leaderboard could be farmed by looping a lesson
    the learner has already finished — the attempt count would climb while nothing new was
    learned.
    """
    headers, ids = await _learner_with_catalogue(client, content_db)
    await _complete_a_lesson(client, headers, ids["A1-lesson-1"])

    await _complete_a_lesson(client, headers, ids["A1-lesson-1"])

    progress = (await client.get("/api/v1/progress", headers=headers)).json()
    assert progress["totalXp"] == 1, "the replay must not add a second point"

    unlocks = (await client.get("/api/v1/progress/unlocks", headers=headers)).json()
    first = next(item for item in unlocks["items"] if item["lessonId"] == str(ids["A1-lesson-1"]))
    assert first["attempts"] == 2, "a replay is still an attempt worth recording"
    assert first["xpEarned"] == 1
    assert first["bestScore"] == 100

    # The second sitting is in the feed with nothing attached, so a learner can see the
    # replay happened without the total moving.
    feed = progress["xp"]["items"]
    assert len(feed) == 2
    assert sum(event["xpAwarded"] for event in feed) == 1


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_completing_a_lesson_flows_into_the_summary_and_the_feed(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """One finished attempt, read back through every progress endpoint.

    The single question is worth one point, so the full total is one: a figure that could
    only be right if the score were read from the engine's own row.
    """
    headers, ids = await _learner_with_catalogue(client, content_db)
    await _complete_a_lesson(client, headers, ids["A1-lesson-1"])

    body = (await client.get("/api/v1/progress", headers=headers)).json()
    assert body["totalXp"] == 1
    assert body["lessonsStarted"] == 1
    assert body["lessonsCompleted"] == 1
    assert body["lessonsInProgress"] == 0
    assert body["answeredCount"] == 1
    assert body["correctCount"] == 1
    assert body["accuracyPercent"] == 100
    assert body["currentStreakDays"] == 1
    assert body["longestStreakDays"] == 1
    assert body["lastActivityAt"] is not None

    unlocks = (await client.get("/api/v1/progress/unlocks", headers=headers)).json()
    first = next(item for item in unlocks["items"] if item["lessonId"] == str(ids["A1-lesson-1"]))
    assert first["state"] == "completed"
    assert first["bestScore"] == 100
    assert first["attempts"] == 1
    assert first["xpEarned"] == 1

    feed = body["xp"]
    assert len(feed["items"]) == 1
    event = feed["items"][0]
    assert event["lessonId"] == str(ids["A1-lesson-1"])
    assert event["xpAwarded"] == 1
    assert event["scorePercent"] == 100
    assert event["totalCount"] == 1


@pytest.mark.asyncio
async def test_an_abandoned_session_is_not_xp_history(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """An abandoned sitting earned nothing, so it is neither activity nor a feed row."""
    headers, ids = await _learner_with_catalogue(client, content_db)
    started = await _start_quiz(client, headers, ids["A1-lesson-1"])
    abandoned = await client.post(
        f"/api/v1/quiz/sessions/{started['session']['id']}",
        json={"op": "abandon"},
        headers=headers,
    )
    assert abandoned.status_code == 200, abandoned.text

    body = (await client.get("/api/v1/progress", headers=headers)).json()
    assert body["lastActivityAt"] is None
    assert body["totalXp"] == 0
    # Starting the lesson still counts it as begun, though it is not finished.
    assert body["lessonsInProgress"] == 1
    assert body["lessonsCompleted"] == 0

    feed = body["xp"]
    assert feed["items"] == []
    assert feed["meta"]["hasMore"] is False


@pytest.mark.asyncio
async def test_the_xp_feed_pages_with_a_cursor_without_overlap(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Two completions, read one per page, must each appear exactly once."""
    headers, ids = await _learner_with_catalogue(client, content_db)
    first = await _complete_a_lesson(client, headers, ids["A1-lesson-1"])
    second = await _complete_a_lesson(client, headers, ids["A1-lesson-2"])

    page_one = (await client.get("/api/v1/progress", params={"limit": 1}, headers=headers)).json()[
        "xp"
    ]
    assert len(page_one["items"]) == 1
    assert page_one["meta"]["hasMore"] is True
    cursor = page_one["meta"]["nextCursor"]
    assert cursor

    page_two = (
        await client.get("/api/v1/progress", params={"limit": 1, "cursor": cursor}, headers=headers)
    ).json()["xp"]
    assert page_two["meta"]["hasMore"] is False
    assert page_two["meta"]["nextCursor"] is None

    seen = [page_one["items"][0]["sessionId"], page_two["items"][0]["sessionId"]]
    assert set(seen) == {str(first), str(second)}
    assert len(set(seen)) == 2


@pytest.mark.asyncio
async def test_a_useless_progress_cursor_is_a_validation_error(
    client: AsyncClient,
) -> None:
    """Returning page one would look like a working feed, which is the trap."""
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    response = await client.get(
        "/api/v1/progress", params={"cursor": "not-a-cursor"}, headers=headers
    )

    assert response.status_code == 422, response.text
    assert error_code(response) == "VALIDATION_ERROR"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, 101])
async def test_the_progress_page_size_is_bounded(client: AsyncClient, limit: int) -> None:
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    response = await client.get("/api/v1/progress", params={"limit": limit}, headers=headers)

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_progress_is_scoped_to_the_caller(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """One learner's finished lesson must not colour another learner's map."""
    owner = await sign_in(client)
    owner_headers = auth_headers(owner["tokens"]["accessToken"])
    ids = await seed_catalogue(content_db)
    await _complete_a_lesson(client, owner_headers, ids["A1-lesson-1"])

    other = await sign_in(
        client,
        token="other-learner-token",
        subject="other-learner",
        email="other-learner@natiqo.app",
    )
    other_headers = auth_headers(other["tokens"]["accessToken"])

    body = (await client.get("/api/v1/progress", headers=other_headers)).json()
    assert body["totalXp"] == 0
    assert body["lessonsCompleted"] == 0

    unlocks = (await client.get("/api/v1/progress/unlocks", headers=other_headers)).json()
    other = next(item for item in unlocks["items"] if item["lessonId"] == str(ids["A1-lesson-1"]))
    assert other["state"] == "available"

    assert body["xp"]["items"] == []


# ====================================================================== review
# The spaced-repetition HTTP surface: the auth gate, the wire shape, and the two contracts
# worth pinning at the boundary — the queue serves the answer-free question a live session
# does, and answering returns the verdict plus the SM-2 schedule it derived. The use cases
# are exercised in ``tests/integration``; this is the HTTP contract a client is generated
# from.


async def _learner_with_a_card(
    client: AsyncClient,
    content_db: AsyncSession,
    *,
    lesson_key: str = "A1-lesson-1",
    due_at: datetime,
    repetitions: int = 0,
    interval_days: int = 0,
    lapses: int = 0,
    state: str = ReviewState.NEW.value,
) -> tuple[dict[str, str], uuid.UUID]:
    """A signed-in learner, the seeded catalogue, and one card planted for them.

    Planted rather than grown through the engine because the queue only shows cards that
    are *due*, and a freshly-seeded card is due tomorrow. Returns the caller's headers and
    the card id.
    """
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])
    ids = await seed_catalogue(content_db)
    question = await _question_of(content_db, ids[lesson_key])
    card = ReviewCardModel(
        id=uuid.uuid4(),
        user_id=uuid.UUID(account["user"]["id"]),
        question_id=question.id,
        ease_factor=2.5,
        interval_days=interval_days,
        repetitions=repetitions,
        lapses=lapses,
        due_at=due_at,
        last_reviewed_at=None,
        state=state,
    )
    content_db.add(card)
    await content_db.commit()
    return headers, card.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/review/due",
        "/api/v1/review/stats",
        "/api/v1/review/preview",
    ],
)
async def test_review_reads_need_a_token(client: AsyncClient, path: str) -> None:
    """Review figures are account data, so the read prefix is gated like ``/me``."""
    response = await client.get(path)

    assert response.status_code == 401, response.text
    assert error_code(response) == "AUTH_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_answering_a_review_needs_a_token(client: AsyncClient) -> None:
    response = await client.post(
        f"/api/v1/review/cards/{uuid.uuid4()}/answer", json={"choice": "Hello"}
    )

    assert response.status_code == 401, response.text
    assert error_code(response) == "AUTH_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_a_brand_new_learner_has_an_empty_review_surface(
    client: AsyncClient,
) -> None:
    """Every figure is derived from an empty card set, not defaulted into place."""
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    queue = await client.get("/api/v1/review/due", headers=headers)
    assert queue.status_code == 200, queue.text
    assert queue.json() == {"cards": [], "hasMore": False, "dueCount": 0}

    preview = await client.get("/api/v1/review/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json() == {"dueCount": 0}

    stats = await client.get("/api/v1/review/stats", headers=headers)
    assert stats.status_code == 200, stats.text
    assert stats.json() == {
        "totalCards": 0,
        "dueToday": 0,
        "future": 0,
        "reviewedTotal": 0,
        "retentionPercent": 0,
    }


@pytest.mark.asyncio
async def test_the_due_queue_serves_the_question_without_the_answer(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The whole reason the queue reuses the session projection: no key before an answer."""
    headers, card_id = await _learner_with_a_card(
        client,
        content_db,
        due_at=datetime.now(UTC) - timedelta(days=1),
        repetitions=2,
        interval_days=6,
        state=ReviewState.REVIEW.value,
    )

    response = await client.get("/api/v1/review/due", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["hasMore"] is False
    assert body["dueCount"] == 1
    (card,) = body["cards"]
    assert card["cardId"] == str(card_id)
    assert card["state"] == "review"
    assert card["intervalDays"] == 6
    assert card["repetitions"] == 2
    question = card["question"]
    assert set(question) == PUBLIC_QUESTION_FIELDS | {"position"}
    for leaked in ANSWER_KEY_FIELDS:
        assert leaked not in question
    assert SEEDED_EXPLANATION not in response.text


@pytest.mark.asyncio
async def test_answering_a_card_reports_the_verdict_and_the_new_schedule(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    headers, card_id = await _learner_with_a_card(
        client, content_db, due_at=datetime.now(UTC) - timedelta(days=1)
    )

    response = await client.post(
        f"/api/v1/review/cards/{card_id}/answer", json={"choice": "Hello"}, headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["isCorrect"] is True
    assert body["correctAnswer"] == {"choice": "Hello"}
    assert body["explanation"] == SEEDED_EXPLANATION
    assert body["pointsAwarded"] == 1
    assert body["quality"] == 5
    assert body["intervalDays"] == 1
    assert body["repetitions"] == 1
    assert body["lapses"] == 0
    assert body["state"] == "learning"
    assert body["dueAt"] is not None


@pytest.mark.asyncio
async def test_a_wrong_review_resets_the_schedule(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A failed recall throws the run away and brings the card back tomorrow."""
    headers, card_id = await _learner_with_a_card(
        client,
        content_db,
        due_at=datetime.now(UTC) - timedelta(days=1),
        repetitions=3,
        interval_days=15,
        state=ReviewState.REVIEW.value,
    )

    response = await client.post(
        f"/api/v1/review/cards/{card_id}/answer", json={"choice": "Goodbye"}, headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["isCorrect"] is False
    assert body["quality"] == 2
    assert body["intervalDays"] == 1
    assert body["repetitions"] == 0
    assert body["lapses"] == 1
    assert body["state"] == "relearning"


@pytest.mark.asyncio
async def test_a_strict_empty_queue_reports_nothing_due(client: AsyncClient) -> None:
    """``strict`` turns the blank screen into a code the client can branch on."""
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    response = await client.get("/api/v1/review/due", params={"strict": True}, headers=headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == "REVIEW_NOTHING_DUE"


@pytest.mark.asyncio
async def test_answering_an_unknown_card_is_not_found(client: AsyncClient) -> None:
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    response = await client.post(
        f"/api/v1/review/cards/{uuid.uuid4()}/answer", json={"choice": "Hello"}, headers=headers
    )

    assert response.status_code == 404, response.text
    assert error_code(response) == "NOT_FOUND"


@pytest.mark.asyncio
async def test_review_is_scoped_to_the_caller(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """One learner's card must not be readable or answerable by another."""
    owner_headers, card_id = await _learner_with_a_card(
        client, content_db, due_at=datetime.now(UTC) - timedelta(days=1)
    )
    other = await sign_in(
        client,
        token="review-other-token",
        subject="review-other",
        email="review-other@natiqo.app",
    )
    other_headers = auth_headers(other["tokens"]["accessToken"])

    queue = await client.get("/api/v1/review/due", headers=other_headers)
    assert queue.json()["cards"] == []
    assert queue.json()["dueCount"] == 0

    stolen = await client.post(
        f"/api/v1/review/cards/{card_id}/answer",
        json={"choice": "Hello"},
        headers=other_headers,
    )
    assert stolen.status_code == 404, stolen.text

    # The owner can still answer it; the refusal above changed nothing.
    mine = await client.post(
        f"/api/v1/review/cards/{card_id}/answer", json={"choice": "Hello"}, headers=owner_headers
    )
    assert mine.status_code == 200, mine.text


@pytest.mark.asyncio
async def test_completing_a_lesson_seeds_a_review_card(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The property that ties review to the engine: a correct answer schedules the question.

    The card lands due tomorrow, so it is counted ahead rather than as due today — which
    only holds if the due date came off the scheduler.
    """
    headers, ids = await _learner_with_catalogue(client, content_db)

    stats_before = (await client.get("/api/v1/review/stats", headers=headers)).json()
    assert stats_before["totalCards"] == 0

    await _complete_a_lesson(client, headers, ids["A1-lesson-1"])

    stats = (await client.get("/api/v1/review/stats", headers=headers)).json()
    assert stats["totalCards"] == 1
    assert stats["dueToday"] == 0
    assert stats["future"] == 1
    assert stats["reviewedTotal"] == 0


@pytest.mark.asyncio
async def test_review_responses_are_never_cached(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Review state on a shared phone's HTTP cache is the next user's problem."""
    headers, _ = await _learner_with_a_card(
        client, content_db, due_at=datetime.now(UTC) - timedelta(days=1)
    )

    response = await client.get("/api/v1/review/due", headers=headers)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"


# ========================================================================== ai
# The AI tutor HTTP surface. The suite runs with AI disabled (``conftest`` pins
# ``AI_ENABLED=false``), which is exactly the configuration worth pinning at the boundary:
# the whole design claim is that the tutor degrades to a deterministic, non-AI answer rather
# than failing, so a test that only ran against a live model would prove the opposite of the
# property that matters. Verdicts, provenance and the thread lifecycle are all checked here;
# the use cases themselves are exercised in ``tests/integration``.


#: The exact request shape ``/ai/explain`` expects. ``questionId`` plus a bare answer, the
#: same contract ``/quiz`` and ``/review`` keep: the key is never sent before an answer is.
def _explain_body(question_id: uuid.UUID, *, choice: str = "Hello") -> dict[str, Any]:
    return {"questionId": str(question_id), "answer": {"choice": choice}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/ai/explain"),
        ("POST", "/api/v1/ai/tutor"),
        ("GET", "/api/v1/ai/tutor/{conversation_id}"),
        ("GET", "/api/v1/ai/quota"),
    ],
)
async def test_the_ai_surface_needs_a_token(client: AsyncClient, method: str, path: str) -> None:
    """A thread and a quota are account data, so the whole prefix is gated like ``/me``.

    ``/explain`` earns its place in this list even though grading looks stateless: the
    rationale is metered against the caller's AI budget, so an anonymous caller must not be
    able to spend it.
    """
    url = path.replace("{conversation_id}", str(uuid.uuid4()))
    if method == "POST":
        response = await client.post(url, json={})
    else:
        response = await client.get(url)

    assert response.status_code == 401, response.text
    assert error_code(response) == "AUTH_TOKEN_INVALID"


@pytest.mark.asyncio
async def test_explaining_an_answer_grades_on_the_server(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """``/ai/explain`` returns the verdict, the key and the rationale — after the answer.

    With AI off the rationale is the authored CMS text, and ``source`` says ``authored``
    rather than passing a fallback off as model output. That provenance flag is the part a
    client renders, so it is asserted rather than the wording.
    """
    headers, ids = await _learner_with_catalogue(client, content_db)
    question = await _question_of(content_db, ids["A1-lesson-1"])

    response = await client.post(
        "/api/v1/ai/explain", json=_explain_body(question.id), headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["isCorrect"] is True
    assert body["correctAnswer"] == {"choice": "Hello"}
    assert body["explanation"] == SEEDED_EXPLANATION
    assert body["source"] == "authored"


@pytest.mark.asyncio
async def test_explaining_a_wrong_answer_reports_the_verdict_honestly(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A wrong pick is reported wrong, and the key is still returned only after the answer."""
    headers, ids = await _learner_with_catalogue(client, content_db)
    question = await _question_of(content_db, ids["A1-lesson-1"])

    response = await client.post(
        "/api/v1/ai/explain", json=_explain_body(question.id, choice="Goodbye"), headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["isCorrect"] is False
    assert body["correctAnswer"] == {"choice": "Hello"}


@pytest.mark.asyncio
async def test_explaining_an_unknown_question_is_not_found(client: AsyncClient) -> None:
    """An invented question id reads as absent, the same way a draft lesson does."""
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    response = await client.post(
        "/api/v1/ai/explain", json=_explain_body(uuid.uuid4()), headers=headers
    )

    assert response.status_code == 404, response.text
    assert error_code(response) == "QUESTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_explaining_rejects_a_malformed_body(client: AsyncClient) -> None:
    """The request schema is enforced at the edge: no question id, no grading."""
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    response = await client.post(
        "/api/v1/ai/explain", json={"answer": {"choice": "Hello"}}, headers=headers
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_a_tutor_thread_opens_then_continues(client: AsyncClient) -> None:
    """A first message opens a thread; a second continues it and the transcript keeps both.

    This is the contract that makes the tutor a tutor rather than a stateless prompt: the
    learner's earlier turns are persisted and readable, and a returning client resumes the
    same conversation instead of starting a new one.
    """
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    first = await client.post(
        "/api/v1/ai/tutor", json={"message": "How do I say hello?"}, headers=headers
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["source"] == "authored"
    assert first_body["reply"].strip() != ""
    conversation_id = first_body["conversationId"]

    read_back = await client.get(f"/api/v1/ai/tutor/{conversation_id}", headers=headers)
    assert read_back.status_code == 200, read_back.text
    body = read_back.json()
    assert body["id"] == conversation_id
    assert [turn["role"] for turn in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "How do I say hello?"

    second = await client.post(
        "/api/v1/ai/tutor",
        json={"message": "And how do I say goodbye?", "conversationId": conversation_id},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["conversationId"] == conversation_id

    grown = (await client.get(f"/api/v1/ai/tutor/{conversation_id}", headers=headers)).json()
    assert [turn["role"] for turn in grown["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_a_tutor_message_must_not_be_blank(client: AsyncClient) -> None:
    """An empty message is a 422 at the edge rather than an empty turn in the transcript."""
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    response = await client.post("/api/v1/ai/tutor", json={"message": "   "}, headers=headers)

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_reading_another_learners_thread_is_not_found(client: AsyncClient) -> None:
    """A conversation is scoped to its owner; another account's thread reads as absent."""
    owner = await sign_in(client)
    owner_headers = auth_headers(owner["tokens"]["accessToken"])
    opened = await client.post(
        "/api/v1/ai/tutor", json={"message": "Private question."}, headers=owner_headers
    )
    conversation_id = opened.json()["conversationId"]

    other = await sign_in(
        client,
        token="ai-other-id-token-probe",
        subject="ai-other",
        email="ai-other@natiqo.app",
    )
    other_headers = auth_headers(other["tokens"]["accessToken"])

    stolen = await client.get(f"/api/v1/ai/tutor/{conversation_id}", headers=other_headers)
    assert stolen.status_code == 404, stolen.text
    assert error_code(stolen) == "CONVERSATION_NOT_FOUND"

    continued = await client.post(
        "/api/v1/ai/tutor",
        json={"message": "Still here?", "conversationId": conversation_id},
        headers=other_headers,
    )
    assert continued.status_code == 404, continued.text

    # The owner can still read it; the refusals above changed nothing.
    mine = await client.get(f"/api/v1/ai/tutor/{conversation_id}", headers=owner_headers)
    assert mine.status_code == 200, mine.text


@pytest.mark.asyncio
async def test_reading_an_unknown_thread_is_not_found(client: AsyncClient) -> None:
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    response = await client.get(f"/api/v1/ai/tutor/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 404, response.text
    assert error_code(response) == "CONVERSATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_the_quota_reports_the_daily_allowance(client: AsyncClient) -> None:
    """The quota is readable before any AI call, and reports the feature as disabled.

    ``enabled`` is ``false`` because the suite runs with AI off; the budget figures must
    still be internally consistent, which is what a UI branches on to warn a learner before
    the allowance runs out.
    """
    account = await sign_in(client)
    headers = auth_headers(account["tokens"]["accessToken"])

    response = await client.get("/api/v1/ai/quota", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False
    assert body["allowed"] is True
    assert body["spentUsd"] == 0.0
    assert body["limitUsd"] >= 0.0
    assert body["remainingUsd"] == pytest.approx(body["limitUsd"] - body["spentUsd"])


# ============================================================== admin / CMS (Phase 8)
# The authoring surface. Where the public ``/content`` prefix only ever shows published
# rows, ``/admin`` shows the whole editorial pipeline and is the only place it can be
# changed. Two properties are pinned here that a service-level test cannot see: the admin
# gate is on the HTTP boundary (a plain account is a 403 envelope), and every mutation
# leaves an audit row in the same transaction — the "who changed this" half of the double
# protection the module docstrings describe.
#
# A token's admin claim is never trusted: ``require_admin`` reads the live account. That is
# what lets these tests promote a just-signed-in learner by writing the flag and then reuse
# the same access token, which is exactly the demotion-takes-effect-immediately behaviour
# the check exists to provide.

QUESTION_BODY: dict[str, Any] = {
    "kind": "multiple_choice",
    "skill": "vocabulary",
    "prompt": "Which one means 'hello'?",
    "choices": ["Hello", "Goodbye"],
    "correct_index": 0,
    "explanation": "Hello is a greeting.",
    "points": 1,
}


async def _grant_admin(
    client: AsyncClient,
    content_db: AsyncSession,
    *,
    token: str | None = None,
    **identity: Any,
) -> dict[str, str]:
    """Sign in, set the admin flag directly, and return auth headers.

    The database write is the point: ``require_admin`` reads the live account, so promoting
    a learner here is enough for the token already in hand to carry the new authority —
    no re-login, and no reliance on a stale claim.
    """
    account = await sign_in(client, token=token, **identity)
    row = await content_db.get(UserModel, uuid.UUID(account["user"]["id"]))
    assert row is not None, "the account the API just created is not in the database"
    row.is_admin = True
    await content_db.commit()
    content_db.expunge_all()
    return auth_headers(account["tokens"]["accessToken"])


async def _audit_actions(content_db: AsyncSession) -> list[str]:
    """Every action string written to ``audit_logs`` so far, oldest first."""
    content_db.expunge_all()
    result = await content_db.execute(
        select(AuditLogModel.action).order_by(AuditLogModel.created_at)
    )
    return [row[0] for row in result.all()]


async def _create_level(
    client: AsyncClient, headers: dict[str, str], code: str = "A1"
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/admin/content/levels",
        json={"code": code, "title": f"Level {code}", "description": "Authored by a test."},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _approve_and_publish(client: AsyncClient, headers: dict[str, str], path: str) -> None:
    """Walk an aggregate through ``review`` and ``approved`` to ``published``.

    The lifecycle forbids skipping a step, so a test that wants a published parent has to
    take each one — which doubles as cover for the state machine over HTTP.
    """
    for state in ("review", "approved", "published"):
        response = await client.patch(path, json={"state": state}, headers=headers)
        assert response.status_code == 200, response.text


async def _published_lesson(client: AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    """Author a level, unit and lesson and leave the lesson ``approved``, parents published."""
    level = await _create_level(client, headers)
    await _approve_and_publish(client, headers, f"/api/v1/admin/content/levels/{level['id']}")

    unit_response = await client.post(
        f"/api/v1/admin/content/levels/{level['id']}/units",
        json={"title": "Introductions"},
        headers=headers,
    )
    assert unit_response.status_code == 201, unit_response.text
    unit = unit_response.json()
    await _approve_and_publish(client, headers, f"/api/v1/admin/content/units/{unit['id']}")

    lesson_response = await client.post(
        f"/api/v1/admin/content/units/{unit['id']}/lessons",
        json={"title": "Saying hello", "summary": "Greetings.", "estimated_minutes": 5},
        headers=headers,
    )
    assert lesson_response.status_code == 201, lesson_response.text
    lesson = lesson_response.json()
    for state in ("review", "approved"):
        response = await client.patch(
            f"/api/v1/admin/content/lessons/{lesson['id']}", json={"state": state}, headers=headers
        )
        assert response.status_code == 200, response.text
    return {"level": level["id"], "unit": unit["id"], "lesson": lesson["id"]}


@pytest.mark.asyncio
async def test_admin_authors_a_level_and_the_trail_records_it(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Create, read back, and confirm the write left an audit row naming the actor."""
    headers = await _grant_admin(client, content_db)

    created = await _create_level(client, headers)
    assert created["code"] == "A1"
    assert created["state"] == "draft", "a new level is never born visible"
    assert created["position"] == 1

    fetched = await client.get(f"/api/v1/admin/content/levels/{created['id']}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["id"] == created["id"]

    page = (await client.get("/api/v1/admin/content/levels", headers=headers)).json()
    assert [level["id"] for level in page["items"]] == [created["id"]]
    assert page["meta"]["hasMore"] is False

    assert "content.level.create" in await _audit_actions(content_db)


@pytest.mark.asyncio
async def test_admin_updates_a_level_field_by_field_and_deletes_it(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """PATCH touches only the fields sent; DELETE removes the row and audits both moves."""
    headers = await _grant_admin(client, content_db)
    level = await _create_level(client, headers)

    patched = await client.patch(
        f"/api/v1/admin/content/levels/{level['id']}",
        json={"title": "Absolute beginner"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["title"] == "Absolute beginner"
    assert body["description"] == "Authored by a test.", "an absent field must not be cleared"

    removed = await client.delete(f"/api/v1/admin/content/levels/{level['id']}", headers=headers)
    assert removed.status_code == 204, removed.text
    assert removed.content == b""

    gone = await client.get(f"/api/v1/admin/content/levels/{level['id']}", headers=headers)
    assert gone.status_code == 404, gone.text
    assert error_code(gone) == "LEVEL_NOT_FOUND"

    actions = await _audit_actions(content_db)
    assert "content.level.update" in actions
    assert "content.level.delete" in actions


@pytest.mark.asyncio
async def test_admin_manages_units_under_a_level(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    headers = await _grant_admin(client, content_db)
    level = await _create_level(client, headers)

    created = await client.post(
        f"/api/v1/admin/content/levels/{level['id']}/units",
        json={"title": "Introductions"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    unit = created.json()
    assert unit["levelId"] == level["id"]
    assert unit["state"] == "draft"

    listed = await client.get(f"/api/v1/admin/content/levels/{level['id']}/units", headers=headers)
    assert [item["id"] for item in listed.json()["items"]] == [unit["id"]]

    fetched = await client.get(f"/api/v1/admin/content/units/{unit['id']}", headers=headers)
    assert fetched.status_code == 200, fetched.text

    patched = await client.patch(
        f"/api/v1/admin/content/units/{unit['id']}",
        json={"title": "First contact"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "First contact"

    removed = await client.delete(f"/api/v1/admin/content/units/{unit['id']}", headers=headers)
    assert removed.status_code == 204, removed.text


@pytest.mark.asyncio
async def test_admin_manages_lessons_including_the_answer_key_view(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The editor's lesson read carries the answer key; the public one never does."""
    headers = await _grant_admin(client, content_db)
    level = await _create_level(client, headers)
    unit = (
        await client.post(
            f"/api/v1/admin/content/levels/{level['id']}/units",
            json={"title": "Introductions"},
            headers=headers,
        )
    ).json()

    created = await client.post(
        f"/api/v1/admin/content/units/{unit['id']}/lessons",
        json={"title": "Saying hello", "summary": "Greetings.", "estimated_minutes": 5},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    lesson = created.json()

    await client.post(
        f"/api/v1/admin/content/lessons/{lesson['id']}/questions",
        json=QUESTION_BODY,
        headers=headers,
    )

    detail = await client.get(f"/api/v1/admin/content/lessons/{lesson['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["title"] == "Saying hello"
    (question,) = body["questions"]
    assert question["correctIndex"] == 0, "the editorial view must carry the answer key"
    assert question["prompt"] == QUESTION_BODY["prompt"]

    listed = await client.get(f"/api/v1/admin/content/units/{unit['id']}/lessons", headers=headers)
    assert [item["id"] for item in listed.json()["items"]] == [lesson["id"]]

    patched = await client.patch(
        f"/api/v1/admin/content/lessons/{lesson['id']}",
        json={"estimated_minutes": 8},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["estimatedMinutes"] == 8

    removed = await client.delete(f"/api/v1/admin/content/lessons/{lesson['id']}", headers=headers)
    assert removed.status_code == 204, removed.text


@pytest.mark.asyncio
async def test_admin_publishes_a_lesson_only_once_its_unit_is_published(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The dedicated publish endpoint, and the parent rule it cannot be used to skip."""
    headers = await _grant_admin(client, content_db)
    ids = await _published_lesson(client, headers)

    published = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/publish", headers=headers
    )
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"

    # A second publish is refused: the lifecycle runs one way.
    again = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/publish", headers=headers
    )
    assert again.status_code == 409, again.text
    assert error_code(again) == "INVALID_STATE_TRANSITION"


@pytest.mark.asyncio
async def test_admin_cannot_publish_a_lesson_into_a_draft_unit(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The aggregate refuses it, so the endpoint answers 409 rather than leaking a draft."""
    headers = await _grant_admin(client, content_db)
    level = await _create_level(client, headers)
    unit = (
        await client.post(
            f"/api/v1/admin/content/levels/{level['id']}/units",
            json={"title": "Introductions"},
            headers=headers,
        )
    ).json()
    lesson = (
        await client.post(
            f"/api/v1/admin/content/units/{unit['id']}/lessons",
            json={"title": "Saying hello", "summary": "Greetings.", "estimated_minutes": 5},
            headers=headers,
        )
    ).json()
    # Approve the lesson but leave its unit in draft.
    for state in ("review", "approved"):
        await client.patch(
            f"/api/v1/admin/content/lessons/{lesson['id']}", json={"state": state}, headers=headers
        )

    response = await client.post(
        f"/api/v1/admin/content/lessons/{lesson['id']}/publish", headers=headers
    )
    assert response.status_code == 409, response.text
    assert error_code(response) == "INVALID_STATE_TRANSITION"


@pytest.mark.asyncio
async def test_admin_manages_questions_and_reorders_them(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    headers = await _grant_admin(client, content_db)
    ids = await _published_lesson(client, headers)

    first = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions",
        json=QUESTION_BODY,
        headers=headers,
    )
    assert first.status_code == 201, first.text
    second_body = {**QUESTION_BODY, "prompt": "Which one means 'goodbye'?", "correct_index": 1}
    second = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions",
        json=second_body,
        headers=headers,
    )
    assert second.status_code == 201, second.text
    first_id, second_id = first.json()["id"], second.json()["id"]
    assert first.json()["position"] == 1
    assert second.json()["position"] == 2

    reordered = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions/reorder",
        json={"question_ids": [second_id, first_id]},
        headers=headers,
    )
    assert reordered.status_code == 200, reordered.text
    assert [item["id"] for item in reordered.json()["items"]] == [second_id, first_id]
    assert [item["position"] for item in reordered.json()["items"]] == [1, 2]

    patched = await client.patch(
        f"/api/v1/admin/content/questions/{first_id}",
        json={"prompt": "Which one greets someone?"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["prompt"] == "Which one greets someone?"

    # A question inside a lesson whose parents are published is live content: a learner may
    # be answering it right now, and the answer trail is keyed to it. Deleting it would
    # rewrite history, so the hard delete is refused and archiving is the way out.
    refused = await client.delete(f"/api/v1/admin/content/questions/{first_id}", headers=headers)
    assert refused.status_code == 409, refused.text
    assert error_code(refused) == "CONTENT_NOT_EDITABLE"
    assert "content.lesson.reorder_questions" in await _audit_actions(content_db)

    # A draft lesson is the one place a hard delete is still safe, so prove it still works.
    draft_level = await _create_level(client, headers, code="A2")
    draft_unit = (
        await client.post(
            f"/api/v1/admin/content/levels/{draft_level['id']}/units",
            json={"title": "Introductions"},
            headers=headers,
        )
    ).json()
    draft_lesson = (
        await client.post(
            f"/api/v1/admin/content/units/{draft_unit['id']}/lessons",
            json={"title": "Saying goodbye", "summary": "Farewells.", "estimated_minutes": 5},
            headers=headers,
        )
    ).json()
    draft_question = await client.post(
        f"/api/v1/admin/content/lessons/{draft_lesson['id']}/questions",
        json=QUESTION_BODY,
        headers=headers,
    )
    removed = await client.delete(
        f"/api/v1/admin/content/questions/{draft_question.json()['id']}", headers=headers
    )
    assert removed.status_code == 204, removed.text


@pytest.mark.asyncio
async def test_admin_rejects_a_partial_reorder(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A list that omits a question would silently drop it, so it is a 422."""
    headers = await _grant_admin(client, content_db)
    ids = await _published_lesson(client, headers)
    created = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions",
        json=QUESTION_BODY,
        headers=headers,
    )
    await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions",
        json=QUESTION_BODY,
        headers=headers,
    )

    response = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions/reorder",
        json={"question_ids": [created.json()["id"]]},
        headers=headers,
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_admin_lists_accounts_and_sets_the_admin_flag(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The account surface: a paged list with a total, and a grant/revoke of the flag."""
    headers = await _grant_admin(client, content_db)
    target = await sign_in(
        client, token="target-id-token-probe", email="target@natiqo.app", subject="target"
    )
    target_id = target["user"]["id"]

    listed = await client.get("/api/v1/admin/users", headers=headers)
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["total"] >= 2
    assert target_id in [user["id"] for user in page["items"]]
    assert page["hasMore"] is False

    granted = await client.post(
        "/api/v1/admin/users",
        json={"userId": target_id, "isAdmin": True},
        headers=headers,
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["isAdmin"] is True

    revoked = await client.post(
        "/api/v1/admin/users",
        json={"userId": target_id, "isAdmin": False},
        headers=headers,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["isAdmin"] is False

    unknown_user = await client.post(
        "/api/v1/admin/users",
        json={"userId": str(uuid.uuid4()), "isAdmin": True},
        headers=headers,
    )
    assert unknown_user.status_code == 404, unknown_user.text
    assert error_code(unknown_user) == "USER_NOT_FOUND"

    assert "user.is_admin.update" in await _audit_actions(content_db)


@pytest.mark.asyncio
async def test_the_admin_surface_requires_an_admin_account(client: AsyncClient) -> None:
    """The whole ``/admin`` prefix answers only to the admin flag, enforced at the boundary."""
    learner = await sign_in(client)
    learner_headers = auth_headers(learner["tokens"]["accessToken"])

    refused_read = await client.get("/api/v1/admin/content/levels", headers=learner_headers)
    assert refused_read.status_code == 403, refused_read.text
    assert error_code(refused_read) == "AUTH_ADMIN_REQUIRED"

    refused_users = await client.get("/api/v1/admin/users", headers=learner_headers)
    assert refused_users.status_code == 403, refused_users.text


@pytest.mark.asyncio
async def test_admin_refuses_a_duplicate_level_code_or_a_taken_position(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    headers = await _grant_admin(client, content_db)
    await _create_level(client, headers, code="A1")

    duplicate = await client.post(
        "/api/v1/admin/content/levels",
        json={"code": "A1", "title": "Another A1"},
        headers=headers,
    )
    assert duplicate.status_code == 409, duplicate.text
    assert error_code(duplicate) == "CONFLICT"

    collided = await client.post(
        "/api/v1/admin/content/levels",
        json={"code": "A2", "title": "Level A2", "position": 1},
        headers=headers,
    )
    assert collided.status_code == 409, collided.text


@pytest.mark.asyncio
async def test_admin_refuses_a_question_that_cannot_be_answered(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A multiple-choice item with no correct index is not answerable, so it is a 422."""
    headers = await _grant_admin(client, content_db)
    ids = await _published_lesson(client, headers)

    response = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions",
        json={**QUESTION_BODY, "correct_index": None},
        headers=headers,
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_admin_reads_an_unknown_content_id_as_not_found(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """An id that was never issued is a 404 for an editor, the same answer a learner gets."""
    headers = await _grant_admin(client, content_db)
    response = await client.get(f"/api/v1/admin/content/levels/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 404, response.text
    assert error_code(response) == "LEVEL_NOT_FOUND"


MULTI_SELECT_BODY: dict[str, Any] = {
    "kind": "multi_select",
    "skill": "vocabulary",
    "prompt": "Select the greetings.",
    "choices": ["Hello", "Goodbye", "Hi"],
    "accepted_answers": ["Hello", "Hi"],
    "explanation": "Two of these greet someone.",
    "points": 2,
}


async def _add_question(
    client: AsyncClient,
    headers: dict[str, str],
    lesson_id: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/admin/content/lessons/{lesson_id}/questions",
        json=body or QUESTION_BODY,
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_lesson(
    client: AsyncClient, headers: dict[str, str], unit_id: str, title: str = "Another lesson"
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/admin/content/units/{unit_id}/lessons",
        json={"title": title, "summary": "A second lesson.", "estimated_minutes": 5},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.asyncio
async def test_admin_reads_one_question_by_id(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The editor's projection, reachable without pulling its whole lesson."""
    headers = await _grant_admin(client, content_db)
    ids = await _published_lesson(client, headers)
    created = await _add_question(client, headers, ids["lesson"])

    response = await client.get(f"/api/v1/admin/content/questions/{created['id']}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == created["id"]
    assert response.json()["correctIndex"] == 0, "the editorial view must carry the key"

    missing = await client.get(f"/api/v1/admin/content/questions/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404, missing.text
    assert error_code(missing) == "QUESTION_NOT_FOUND"


@pytest.mark.asyncio
async def test_admin_duplicates_a_question_beside_it_and_into_another_lesson(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """A copy is an independent row: new id, its own slot, the original untouched."""
    headers = await _grant_admin(client, content_db)
    ids = await _published_lesson(client, headers)
    source = await _add_question(client, headers, ids["lesson"])

    response = await client.post(
        f"/api/v1/admin/content/questions/{source['id']}/duplicate", json={}, headers=headers
    )
    assert response.status_code == 201, response.text
    copy = response.json()
    assert copy["id"] != source["id"]
    assert copy["position"] == source["position"] + 1, "a copy lands beside the original"
    assert copy["prompt"] == source["prompt"]
    assert copy["correctIndex"] == source["correctIndex"], "the key travels with the copy"

    other = await _add_lesson(client, headers, ids["unit"])
    moved = await client.post(
        f"/api/v1/admin/content/questions/{source['id']}/duplicate",
        json={"lessonId": other["id"]},
        headers=headers,
    )
    assert moved.status_code == 201, moved.text
    assert moved.json()["lessonId"] == other["id"]
    assert moved.json()["position"] == 1

    # The original lesson still holds exactly the two questions it had.
    detail = await client.get(f"/api/v1/admin/content/lessons/{ids['lesson']}", headers=headers)
    assert [question["id"] for question in detail.json()["questions"]] == [
        source["id"],
        copy["id"],
    ]

    taken = await client.post(
        f"/api/v1/admin/content/questions/{source['id']}/duplicate",
        json={"position": 1},
        headers=headers,
    )
    assert taken.status_code == 409, taken.text
    assert error_code(taken) == "CONFLICT"
    assert "content.question.duplicate" in await _audit_actions(content_db)


@pytest.mark.asyncio
async def test_admin_bulk_creates_questions_or_writes_nothing(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """All-or-nothing, and the failure names every item that was wrong."""
    headers = await _grant_admin(client, content_db)
    ids = await _published_lesson(client, headers)

    accepted = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions/bulk",
        json={
            "questions": [
                QUESTION_BODY,
                {**QUESTION_BODY, "prompt": "Which one means 'goodbye'?", "correct_index": 1},
                {**QUESTION_BODY, "prompt": "Which one means 'thanks'?"},
            ]
        },
        headers=headers,
    )
    assert accepted.status_code == 201, accepted.text
    body = accepted.json()
    assert body["total"] == 3 and body["accepted"] == 3 and body["dryRun"] is False
    assert [item["position"] for item in body["items"]] == [1, 2, 3]

    rejected = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions/bulk",
        json={
            "questions": [
                {**QUESTION_BODY, "prompt": "A fourth item."},
                {**QUESTION_BODY, "prompt": "Not answerable.", "correct_index": None},
            ]
        },
        headers=headers,
    )
    assert rejected.status_code == 422, rejected.text
    failures = rejected.json()["error"]["details"]["failures"]
    assert [failure["index"] for failure in failures] == [1]
    assert failures[0]["problems"], "a rejection must say what is wrong"

    detail = await client.get(f"/api/v1/admin/content/lessons/{ids['lesson']}", headers=headers)
    assert len(detail.json()["questions"]) == 3, "the rejected batch must not half-write"
    assert "content.question.bulk_create" in await _audit_actions(content_db)


@pytest.mark.asyncio
async def test_a_bulk_dry_run_validates_without_writing(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """An import preview must not leave a lesson behind when it only meant to check."""
    headers = await _grant_admin(client, content_db)
    ids = await _published_lesson(client, headers)

    preview = await client.post(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions/bulk",
        json={"questions": [QUESTION_BODY, MULTI_SELECT_BODY], "dryRun": True},
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["accepted"] == 2
    assert preview.json()["items"] == []

    detail = await client.get(f"/api/v1/admin/content/lessons/{ids['lesson']}", headers=headers)
    assert detail.json()["questions"] == []


@pytest.mark.asyncio
async def test_admin_exports_a_lesson_and_imports_it_elsewhere(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """The round trip: a package rebuilds the same questions, keys included."""
    headers = await _grant_admin(client, content_db)
    ids = await _published_lesson(client, headers)
    await _add_question(client, headers, ids["lesson"])
    await _add_question(client, headers, ids["lesson"], MULTI_SELECT_BODY)

    exported = await client.get(
        f"/api/v1/admin/content/lessons/{ids['lesson']}/questions/export", headers=headers
    )
    assert exported.status_code == 200, exported.text
    package = exported.json()
    assert package["version"] == 1
    assert package["lessonId"] == ids["lesson"]
    assert [item["kind"] for item in package["questions"]] == [
        "multiple_choice",
        "multi_select",
    ]
    for item in package["questions"]:
        # A package is a template, not a set of rows: identity and slot stay behind.
        assert "id" not in item and "position" not in item
    # Authored with capitals, stored folded: the entity normalises the key, so the package
    # carries the comparable form rather than the spelling the author happened to type.
    assert package["questions"][1]["acceptedAnswers"] == ["hello", "hi"]

    other = await _add_lesson(client, headers, ids["unit"], title="Imported into")
    # The package is posted back verbatim: its metadata keys are ignored, which is what lets
    # a caller feed an export file straight into import without editing it first.
    imported = await client.post(
        f"/api/v1/admin/content/lessons/{other['id']}/questions/import",
        json=package,
        headers=headers,
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["total"] == 2
    assert [item["position"] for item in imported.json()["items"]] == [1, 2]

    # The lesson the package came from is unchanged.
    original = await client.get(f"/api/v1/admin/content/lessons/{ids['lesson']}", headers=headers)
    assert len(original.json()["questions"]) == 2

    unsupported = await client.post(
        f"/api/v1/admin/content/lessons/{other['id']}/questions/import",
        json={"version": 99, "questions": package["questions"]},
        headers=headers,
    )
    assert unsupported.status_code == 422, unsupported.text
    assert unsupported.json()["error"]["details"]["field"] == "version"

    actions = await _audit_actions(content_db)
    assert "content.question.import" in actions


async def _record_learning(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    lesson_id: uuid.UUID,
    question_ids: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """One finished sitting: a completed session, a right and a wrong answer, a lesson.

    Written directly rather than driven through ``/quiz`` because the overview is about
    *counting what exists*, not about the engine that produced it — and the engine already
    has its own suite.
    """
    now = datetime.now(UTC)
    session = QuizSessionModel(
        id=uuid.uuid4(),
        user_id=user_id,
        mode="lesson",
        lesson_id=lesson_id,
        status=SessionStatus.COMPLETED.value,
        question_ids=[str(question_id) for question_id in question_ids],
        started_at=now,
        expires_at=now,
        completed_at=now,
        correct_count=1,
        xp_awarded=15,
    )
    db.add_all(
        [
            session,
            QuizAnswerModel(
                id=uuid.uuid4(),
                session_id=session.id,
                question_id=question_ids[0],
                position=1,
                given_answer={"choice": "Hello"},
                is_correct=True,
                points_awarded=1,
                attempt=1,
                explanation="A greeting.",
                answered_at=now,
            ),
            QuizAnswerModel(
                id=uuid.uuid4(),
                session_id=session.id,
                question_id=question_ids[1],
                position=2,
                given_answer={"choice": "Goodbye"},
                is_correct=False,
                points_awarded=0,
                attempt=1,
                explanation="A farewell.",
                answered_at=now,
            ),
            LessonProgressModel(
                id=uuid.uuid4(),
                user_id=user_id,
                lesson_id=lesson_id,
                state=ProgressState.COMPLETED.value,
                best_score=50,
                attempts=1,
                xp_earned=15,
                completed_at=now,
            ),
        ]
    )
    await db.commit()


@pytest.mark.asyncio
async def test_admin_overview_aggregates_every_card_of_the_home_screen(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """One read answers the whole screen, and every figure is a counted one."""
    headers = await _grant_admin(client, content_db)
    learner = await sign_in(
        client,
        token="stats-learner-token",
        email="stats@natiqo.app",
        subject="stats-learner",
    )
    learner_id = uuid.UUID(learner["user"]["id"])

    ids = await _published_lesson(client, headers)
    first = await _add_question(client, headers, ids["lesson"])
    second = await _add_question(
        client, headers, ids["lesson"], {**QUESTION_BODY, "prompt": "Which one is a farewell?"}
    )
    await _record_learning(
        content_db,
        user_id=learner_id,
        lesson_id=uuid.UUID(ids["lesson"]),
        question_ids=(uuid.UUID(first["id"]), uuid.UUID(second["id"])),
    )

    # The three cards the sitting alone cannot fill: a streak with XP behind it, two
    # review grades (one passed, one not) so retention is a real ratio, and one metered
    # AI call. Written directly for the same reason the sitting is: this test is about
    # counting what exists, not about the engines that produced it.
    now = datetime.now(UTC)
    card = ReviewCardModel(
        id=uuid.uuid4(),
        user_id=learner_id,
        question_id=uuid.UUID(first["id"]),
        due_at=now,
        state=ReviewState.NEW.value,
    )
    content_db.add_all(
        [
            card,
            ReviewLogModel(
                id=uuid.uuid4(),
                user_id=learner_id,
                card_id=card.id,
                quality=4,
                previous_interval_days=0,
                new_interval_days=1,
                reviewed_at=now,
            ),
            ReviewLogModel(
                id=uuid.uuid4(),
                user_id=learner_id,
                card_id=card.id,
                quality=2,
                previous_interval_days=1,
                new_interval_days=0,
                reviewed_at=now,
            ),
            UserStatsModel(
                user_id=learner_id,
                xp_total=120,
                current_streak=3,
                longest_streak=9,
            ),
            AiUsageRecord(
                id=uuid.uuid4(),
                user_id=learner_id,
                feature="tutor",
                provider="anthropic",
                model="claude",
                input_tokens=60,
                output_tokens=40,
                latency_ms=900,
                cost_usd=Decimal("0.25"),
                status="ok",
                created_at=now,
            ),
        ]
    )
    await content_db.commit()

    # One draft level holding one draft unit, so the pipeline queue is non-empty.
    draft_level = await _create_level(client, headers, code="A2")
    await client.post(
        f"/api/v1/admin/content/levels/{draft_level['id']}/units",
        json={"title": "Still being written"},
        headers=headers,
    )

    response = await client.get("/api/v1/admin/stats/overview", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["windowDays"] == 7
    assert body["users"] == {"total": 2, "new": 2, "active": 2, "deleted": 0}
    assert body["learning"] == {
        "lessonsCompleted": 1,
        "sessionsCompleted": 1,
        "sessionsActive": 0,
        "answersGraded": 2,
        "correctAnswers": 1,
        "accuracyPercent": 50,
        "xpAwarded": 15,
    }
    # The lesson was approved but not published, so it is neither live nor a draft.
    assert body["content"] == {
        "levels": 2,
        "units": 2,
        "lessons": 1,
        "publishedLessons": 0,
        "drafts": 2,
        "inReview": 0,
    }
    questions = body["questions"]
    assert questions["total"] == 2
    assert questions["published"] == 0, "a question's lesson is approved, not published"
    assert questions["byKind"]["multiple_choice"] == 2
    assert set(questions["byKind"]) == {kind.value for kind in QuestionKind}
    assert questions["bySkill"]["vocabulary"] == 2
    assert set(questions["bySkill"]) == {skill.value for skill in SkillKind}
    # 50 from the one finished lesson; 4 and 2 average to 3.0 with one of two passing.
    assert body["performance"] == {
        "averageScore": 50,
        "reviewsTotal": 2,
        "reviewsPassed": 1,
        "retentionPercent": 50,
        "averageQuality": 3.0,
    }
    assert body["gamification"] == {
        "xpTotal": 120,
        "activeStreaks": 1,
        "longestStreak": 9,
        "achievementsUnlocked": 0,
    }
    assert body["ai"] == {
        "requests": 1,
        "errors": 0,
        "tokens": 100,
        "costUsd": 0.25,
        "conversations": 0,
    }

    series = body["series"]
    # The window's endpoints are partial days, so a seven-day window is eight points —
    # the first is today minus seven, the last is today, both still real days.
    assert len(series) == body["windowDays"] + 1
    assert series[0]["day"] == (now - timedelta(days=7)).date().isoformat()
    assert series[-1]["day"] == now.date().isoformat()
    assert series[-1] == {
        "day": now.date().isoformat(),
        "newUsers": 2,
        "sessionsCompleted": 1,
        "xpAwarded": 15,
        "aiRequests": 1,
    }
    # The chart's columns must add up to the card beside it, or the screen contradicts
    # itself depending on which half is read.
    assert sum(point["newUsers"] for point in series) == body["users"]["new"]
    assert (
        sum(point["sessionsCompleted"] for point in series) == body["learning"]["sessionsCompleted"]
    )
    assert sum(point["xpAwarded"] for point in series) == body["learning"]["xpAwarded"]
    assert sum(point["aiRequests"] for point in series) == body["ai"]["requests"]

    wider = await client.get("/api/v1/admin/stats/overview?window=30", headers=headers)
    assert wider.status_code == 200, wider.text
    assert wider.json()["windowDays"] == 30
    # Only the activity figure follows the window; the rest are lifetime totals.
    assert wider.json()["content"] == body["content"]
    assert len(wider.json()["series"]) == 31


@pytest.mark.asyncio
async def test_the_overview_is_admin_only_and_bounded(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    """Account data is not a learner's to read, and the window has sane limits."""
    learner = await sign_in(
        client,
        token="stats-learner-token",
        email="stats-learner@natiqo.app",
        subject="stats-learner",
    )
    refused = await client.get(
        "/api/v1/admin/stats/overview", headers=auth_headers(learner["tokens"]["accessToken"])
    )
    assert refused.status_code == 403, refused.text
    assert error_code(refused) == "AUTH_ADMIN_REQUIRED"

    admin = await _grant_admin(
        client,
        content_db,
        token="stats-admin-two-token",
        email="stats-admin-two@natiqo.app",
        subject="stats-admin-two",
    )
    for window in (0, 91):
        response = await client.get(f"/api/v1/admin/stats/overview?window={window}", headers=admin)
        assert response.status_code == 422, response.text


# ------------------------------------------------------------------------- media
# The media flow has the client upload *directly* to storage, so these tests need the same
# adapter the service will read from. ``media_storage`` points the storage port at a
# per-test directory; a fake would let the test agree with itself about what "the object
# exists" means, which is the one thing confirmation has to get right.

#: A real PNG header, so the sniffer names it rather than guessing from a key extension.
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64
#: A shell script that a caller would like the server to believe is an image.
SCRIPT_BYTES = b"#!/bin/sh\necho not-an-image\n"


@pytest_asyncio.fixture
async def media_storage(client: AsyncClient, tmp_path: Path) -> LocalObjectStorage:
    """Point the storage port at a fresh directory and hand back the adapter.

    Depends on ``client`` so the app (and its schema) is up first; the override is cleared
    by the ``client`` fixture's teardown, like every other dependency override here.
    """
    storage = LocalObjectStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    return storage


async def _media_headers(client: AsyncClient, **identity: Any) -> dict[str, str]:
    account = await sign_in(client, **identity)
    return auth_headers(account["tokens"]["accessToken"])


async def _presign(
    client: AsyncClient, headers: dict[str, str], *, kind: str, content_type: str
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/media/presign",
        json={"kind": kind, "contentType": content_type},
        headers=headers,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


@pytest.mark.asyncio
async def test_media_presign_issues_a_grant_and_the_headers_it_is_bound_to(
    client: AsyncClient,
) -> None:
    headers = await _media_headers(client)
    body = await _presign(client, headers, kind="avatar", content_type="image/png")

    assert re.fullmatch(r"avatar/[0-9a-f]{32}\.png", body["mediaKey"])
    assert body["mediaKey"] in body["uploadUrl"]
    assert body["requiredHeaders"] == {"Content-Type": "image/png"}
    assert body["kind"] == "avatar"
    assert body["expiresInSeconds"] > 0


@pytest.mark.asyncio
async def test_media_presign_refuses_a_type_the_kind_does_not_allow(
    client: AsyncClient,
) -> None:
    headers = await _media_headers(client)
    response = await client.post(
        "/api/v1/media/presign",
        json={"kind": "avatar", "contentType": "application/pdf"},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "MEDIA_TYPE_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_media_presign_rejects_an_unknown_kind(client: AsyncClient) -> None:
    """An unknown kind is a schema failure at the edge, not a dictionary miss deep inside."""
    headers = await _media_headers(client)
    response = await client.post(
        "/api/v1/media/presign",
        json={"kind": "video", "contentType": "video/mp4"},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_media_presign_needs_a_token(client: AsyncClient) -> None:
    response = await client.post(
        "/api/v1/media/presign", json={"kind": "avatar", "contentType": "image/png"}
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_media_confirm_records_what_the_bytes_proved(
    client: AsyncClient, media_storage: LocalObjectStorage
) -> None:
    headers = await _media_headers(client)
    grant = await _presign(client, headers, kind="avatar", content_type="image/png")
    await media_storage.put(grant["mediaKey"], PNG_BYTES, content_type="image/png")

    response = await client.post(
        "/api/v1/media/confirm", json={"mediaKey": grant["mediaKey"]}, headers=headers
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["contentType"] == "image/png"
    assert body["sizeBytes"] == len(PNG_BYTES)
    assert body["kind"] == "avatar"
    assert body["url"].startswith("local://")
    assert re.fullmatch(r"[0-9a-f-]{36}", body["id"])


@pytest.mark.asyncio
async def test_media_confirm_refuses_a_file_whose_bytes_betray_the_claim(
    client: AsyncClient, media_storage: LocalObjectStorage
) -> None:
    """The declared type got the key; the bytes decide whether it is stored."""
    headers = await _media_headers(client)
    grant = await _presign(client, headers, kind="avatar", content_type="image/png")
    await media_storage.put(grant["mediaKey"], SCRIPT_BYTES, content_type="image/png")

    response = await client.post(
        "/api/v1/media/confirm", json={"mediaKey": grant["mediaKey"]}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "MEDIA_TYPE_NOT_ALLOWED"
    assert not await media_storage.exists(grant["mediaKey"])


@pytest.mark.asyncio
async def test_media_confirm_refuses_an_upload_that_never_arrived(client: AsyncClient) -> None:
    headers = await _media_headers(client)
    grant = await _presign(client, headers, kind="image", content_type="image/jpeg")

    response = await client.post(
        "/api/v1/media/confirm", json={"mediaKey": grant["mediaKey"]}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "MEDIA_UPLOAD_FAILED"


@pytest.mark.asyncio
async def test_media_read_re_signs_a_url_for_a_stored_asset(
    client: AsyncClient, media_storage: LocalObjectStorage
) -> None:
    headers = await _media_headers(client)
    grant = await _presign(client, headers, kind="avatar", content_type="image/png")
    await media_storage.put(grant["mediaKey"], PNG_BYTES, content_type="image/png")
    created = (
        await client.post(
            "/api/v1/media/confirm", json={"mediaKey": grant["mediaKey"]}, headers=headers
        )
    ).json()

    response = await client.get(f"/api/v1/media/{created['id']}", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == created["id"]
    assert response.json()["url"].startswith("local://")


@pytest.mark.asyncio
async def test_media_read_refuses_an_unknown_id(client: AsyncClient) -> None:
    headers = await _media_headers(client)
    response = await client.get(f"/api/v1/media/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 404, response.text
    assert error_code(response) == "MEDIA_NOT_FOUND"


@pytest.mark.asyncio
async def test_media_read_rejects_a_non_uuid_id_before_any_lookup(client: AsyncClient) -> None:
    headers = await _media_headers(client)
    response = await client.get("/api/v1/media/not-a-uuid", headers=headers)
    assert response.status_code == 422, response.text
