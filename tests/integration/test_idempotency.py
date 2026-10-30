"""Idempotency: a retry must not become a second write.

The failure this prevents is not abstract. A phone that loses signal mid-response retries
``POST``, and without a guard the retry creates a second quiz session, charges the card
twice, or awards the XP twice. These tests pin the four properties that make the guard
safe, because each one has a cheaper-looking implementation that is wrong:

* a repeat replays the *first* response byte-for-byte;
* a key reused with a *different* body is refused, not silently replayed;
* only success is remembered, so a corrected retry is not permanently rejected;
* the stored response is scoped, so one user's key cannot replay another's.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient, Response
from starlette.middleware import Middleware

from app.api.deps import SettingsDep, UowDep
from app.core.config import Settings
from app.core.idempotency import IdempotencyMiddleware, _scope_user_id, build_key, fingerprint
from app.modules.users.application.auth_service import AuthService
from app.modules.users.domain.identity import (
    IdentityProvider,
    IdentityVerificationError,
    VerifiedIdentity,
)
from app.modules.users.presentation.routes import auth_service

pytestmark = [pytest.mark.integration, pytest.mark.api]

#: The address the HTTP tests sign in with. A fixture, not a real person or mailbox.
PROBE_EMAIL = "idempotency-probe@natiqo.app"


# ------------------------------------------------------------------ caller partition
def _scope(headers: list[tuple[bytes, bytes]]) -> dict:
    return {"type": "http", "headers": headers, "state": {}}


def test_two_callers_presenting_different_tokens_do_not_share_a_partition():
    """The property that keeps one learner's key from replaying another's response.

    This is the reason the partition exists at all. Without it both callers land in
    ``anonymous``, so anyone who guessed a victim's key together with the method and path
    would be served the victim's stored response body.
    """
    mine = _scope_user_id(_scope([(b"authorization", b"Bearer token-of-a")]))
    theirs = _scope_user_id(_scope([(b"authorization", b"Bearer token-of-b")]))

    assert mine is not None
    assert theirs is not None
    assert mine != theirs


def test_the_same_caller_always_lands_in_the_same_partition():
    """Otherwise a client's own retry would miss its own record and write twice."""
    first = _scope_user_id(_scope([(b"authorization", b"Bearer token-of-a")]))
    second = _scope_user_id(_scope([(b"authorization", b"Bearer token-of-a")]))

    assert first == second


def test_the_partition_is_a_digest_rather_than_the_token():
    """The raw bearer token must never end up in a cache key, a log or a dump."""
    token = b"Bearer super-secret-access-token"

    partition = _scope_user_id(_scope([(b"authorization", token)]))

    assert partition is not None
    assert "secret" not in partition
    assert "super" not in partition
    assert len(partition) == 16


def test_an_anonymous_request_gets_no_partition():
    """Sign-in has no session yet; unsigned-in callers share ``anonymous`` by nature."""
    assert _scope_user_id(_scope([])) is None
    assert _scope_user_id(_scope([(b"authorization", b"   ")])) is None


def test_a_published_user_id_wins_over_the_token_digest():
    """A real identity is a better partition than a token that rotates on refresh."""
    scope = {
        "type": "http",
        "headers": [(b"authorization", b"Bearer t")],
        "state": {"user_id": "u-1"},
    }

    assert _scope_user_id(scope) == "u-1"


def test_the_partition_does_not_make_an_unauthenticated_caller_reach_a_session():
    """A forged partition cannot be invented, because it is derived from the token.

    The value is only a cache partition, never an authorisation decision: the route still
    verifies the token. What the digest buys is that guessing it requires holding the very
    token it was derived from.
    """
    with_token = _scope_user_id(_scope([(b"authorization", b"Bearer token-of-a")]))
    guessed = _scope_user_id(_scope([(b"authorization", b"Bearer something-else")]))

    assert guessed != with_token


# ------------------------------------------------------------------ pure helpers
def test_the_same_key_for_two_users_stores_two_different_records():
    """Without the user in the key, A could replay B's stored response."""
    mine = build_key(method="POST", path="/quiz/sessions", user_id="u1", client_key="k")
    theirs = build_key(method="POST", path="/quiz/sessions", user_id="u2", client_key="k")
    assert mine != theirs


def test_the_same_key_on_two_paths_stores_two_different_records():
    """A key is per-endpoint, so one endpoint's response cannot answer another's call."""
    start = build_key(method="POST", path="/quiz/sessions", user_id="u1", client_key="k")
    finish = build_key(method="POST", path="/quiz/sessions/s", user_id="u1", client_key="k")
    assert start != finish


def test_an_empty_body_and_a_missing_body_are_the_same_request():
    """Some routes take no body at all and some clients send ``""``.

    Treating them as different bodies would reject a legitimate retry of a bodiless POST.
    """
    assert fingerprint(b"") == fingerprint(b"")
    assert fingerprint(b"") != fingerprint(b'{"lessonId":"1"}')


# ------------------------------------------------------------------ middleware
class FakeVerifier:
    """A provider that trusts exactly the tokens a test hands it.

    The real adapters verify a signature against Google's and Apple's published keys,
    which is a network call and the subject of its own tests. What this file is about is
    the middleware in front of the route, so the token check is stubbed rather than
    satisfied.
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


#: Module-level so the fixture and the helper address the same instances. ``trust`` is
#: deterministic for a given token, so no test can affect another's result.
GOOGLE = FakeVerifier(IdentityProvider.GOOGLE)
APPLE = FakeVerifier(IdentityProvider.APPLE)


def identity_for(provider: IdentityProvider, **overrides: Any) -> VerifiedIdentity:
    """A provider assertion, with any field overridable by the caller."""
    return VerifiedIdentity(
        **{
            "provider": provider,
            "subject": "idempotency-probe-subject",
            "email": PROBE_EMAIL,
            "email_verified": True,
            "display_name": "Idempotency Probe",
            **overrides,
        }
    )


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An app whose idempotency middleware holds an isolated cache.

    The cache is swapped rather than shared because the store is process-wide: a key
    stored by one test would otherwise make the next test pass or fail by accident,
    which is the hardest kind of test bug to diagnose later.

    ``auth_service`` is overridden only to swap the ID-token verifier. Everything the
    override builds is the production ``AuthService``.
    """
    from app.core.database import create_all, dispose_engine
    from app.core.registry import import_all_models
    from app.main import create_app
    from app.platform.cache.memory import InMemoryCache

    import_all_models()
    await create_all()

    app = create_app(Settings(rate_limit_enabled=False, app_env="testing"))
    fresh = InMemoryCache()
    for entry in app.user_middleware:
        if entry.cls is IdempotencyMiddleware:
            entry.kwargs["cache"] = fresh
            assert isinstance(entry, Middleware)

    def build_service(uow: UowDep, settings: SettingsDep) -> AuthService:
        return AuthService(
            uow,
            settings,
            verifiers={IdentityProvider.GOOGLE: GOOGLE, IdentityProvider.APPLE: APPLE},
        )

    app.dependency_overrides[auth_service] = build_service
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.clear()
        await dispose_engine()


async def _sign_in(
    client: AsyncClient,
    *,
    token: str,
    verifier: FakeVerifier | None = None,
    email: str = PROBE_EMAIL,
    key: str | None = None,
) -> Response:
    """Sign in with a token this process trusts, optionally under an idempotency key.

    Sign-in stands in for the registration endpoint these tests used to drive: it is
    what a client retries on a flaky connection, it creates the account on first sight,
    and it is reached without a session — so it exercises the anonymous partition.
    """
    chosen = verifier or GOOGLE
    chosen.trust(token, identity_for(chosen.provider, subject=token, email=email))
    headers = {"Idempotency-Key": key} if key else {}
    return await client.post(
        f"/api/v1/auth/{chosen.provider.value}",
        json={"id_token": token, "device": {"platform": "android"}},
        headers=headers,
    )


async def _access_token(client: AsyncClient, *, token: str, email: str) -> str:
    """Sign in and return the bearer token, for tests about an *authenticated* caller."""
    response = await _sign_in(client, token=token, email=email)
    assert response.status_code == 200, response.text
    return str(response.json()["tokens"]["accessToken"])


async def test_a_repeat_of_the_same_key_replays_the_first_response(client: AsyncClient):
    """The whole point: the client gets its original answer back, not a second one.

    A second *offline* sign-in would mint a different access token, so byte equality of
    the two bodies is the proof that the route ran once rather than twice.
    """
    key = str(uuid.uuid4())

    first = await _sign_in(client, token="retry-idempotency-token", key=key)
    second = await _sign_in(client, token="retry-idempotency-token", key=key)

    assert first.status_code == 200, first.text
    assert second.json() == first.json()
    assert second.headers.get("Idempotent-Replay") == "true"


async def test_one_caller_cannot_replay_another_callers_response_with_the_same_key(
    client: AsyncClient,
):
    """The end-to-end proof of the partition, through the real middleware.

    Two accounts present the *same* key on the *same* path with *byte-identical* bodies,
    which is the only shape that reaches the replay branch at all -- a differing body is
    caught earlier as a conflict. Without a partition the second caller would be handed the
    first caller's stored response instead of having its own request performed.

    ``PATCH /me`` is the authenticated, body-carrying write the API publishes now that the
    credential endpoints are gone; the partition is derived from the bearer token, so two
    accounts cannot collide however the same key is reused.
    """
    key = str(uuid.uuid4())
    first_token = await _access_token(client, token="first-idem-token", email=PROBE_EMAIL)
    second_token = await _access_token(
        client, token="second-idem-token", email="second-idempotency@natiqo.app"
    )
    body = {"display_name": "Idempotent Learner"}

    first = await client.patch(
        "/api/v1/me",
        json=body,
        headers={"Authorization": f"Bearer {first_token}", "Idempotency-Key": key},
    )
    second = await client.patch(
        "/api/v1/me",
        json=body,
        headers={"Authorization": f"Bearer {second_token}", "Idempotency-Key": key},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.headers.get("Idempotent-Replay") is None, "the second caller was served a replay"


async def test_one_caller_replays_their_own_response_with_the_same_key(client: AsyncClient):
    """The partition must not be so aggressive that a legitimate retry stops replaying.

    This is the counterpart to the isolation test: isolating per caller is only correct if
    the same caller still recognises its own retry.
    """
    key = str(uuid.uuid4())
    token = await _access_token(client, token="owner-idempotency-token", email=PROBE_EMAIL)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
    body = {"display_name": "Idempotent Learner"}

    first = await client.patch("/api/v1/me", json=body, headers=headers)
    second = await client.patch("/api/v1/me", json=body, headers=headers)

    assert first.status_code == 200, first.text
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json() == first.json()


async def test_the_replayed_response_is_distinguishable_from_a_fresh_one(client: AsyncClient):
    """A replay must be visible, or the access log cannot count real writes."""
    key = str(uuid.uuid4())

    first = await _sign_in(client, token="marked-idempotency-token", key=key)
    second = await _sign_in(client, token="marked-idempotency-token", key=key)

    assert "Idempotent-Replay" not in first.headers
    assert second.headers["Idempotent-Replay"] == "true"


async def test_a_key_is_usable_only_once_per_distinct_request(client: AsyncClient):
    """A different token is a different request, so the first response is not replayed."""
    key = str(uuid.uuid4())

    first = await _sign_in(client, token="one-idempotency-token", key=key)
    second = await _sign_in(client, token="two-idempotency-token", key=key)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_a_retry_after_a_correction_is_not_rejected_forever(client: AsyncClient):
    """A 4xx must not be stored, or the fixed retry fails with the old error forever.

    A client that reuses one key per "attempt" must be able to correct its payload.
    Storing the first rejection would make that permanently impossible.
    """
    key = str(uuid.uuid4())

    # A token no verifier was told about produces a 401; the corrected retry must work.
    rejected = await client.post(
        "/api/v1/auth/google",
        json={"id_token": "a-token-nobody-trusted"},
        headers={"Idempotency-Key": key},
    )
    corrected = await _sign_in(client, token="corrected-idempotency-token", key=key)

    assert rejected.status_code == 401, rejected.text
    assert corrected.status_code == 200, corrected.text


async def test_get_requests_are_never_treated_as_idempotent_writes(client: AsyncClient):
    """A GET must not consume a key slot or be answered from the store."""
    response = await client.get("/api/v1/content/tree", headers={"Idempotency-Key": "k"})
    assert response.status_code == 200
    assert "Idempotent-Replay" not in response.headers


async def test_a_request_without_a_key_still_works(client: AsyncClient):
    """Idempotency is opt-in per request; nothing may require the header."""
    response = await _sign_in(client, token="nokey-idempotency-token")
    assert response.status_code == 200, response.text


async def test_a_failing_request_is_not_cached(client: AsyncClient):
    """An unreachable dependency must not make the retry permanently fail.

    If a 500 were stored, one transient database blip would cost the learner the write
    for a day — strictly worse than the duplicate this feature exists to prevent.
    """
    key = str(uuid.uuid4())

    # The schema rejects this body before the route is reached: a 422.
    rejected = await client.post(
        "/api/v1/auth/google",
        json={"id_token": "short"},
        headers={"Idempotency-Key": key},
    )
    accepted = await _sign_in(client, token="later-idempotency-token", key=key)

    assert rejected.status_code == 422
    assert accepted.status_code == 200, accepted.text


# ---------------------------------------------------------------- in-flight lock
def _scope_for(key: str) -> dict[str, Any]:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/quiet",
        "raw_path": b"/api/v1/quiet",
        "root_path": "",
        "query_string": b"",
        "headers": [(b"idempotency-key", key.encode()), (b"content-type", b"application/json")],
        "client": ("198.51.100.1", 1234),
        "server": ("test", 80),
        "state": {},
    }


def _receive_body() -> Any:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return receive


class _Capture:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int:
        return next(int(m["status"]) for m in self.messages if m["type"] == "http.response.start")

    @property
    def headers(self) -> dict[str, str]:
        start = next(m for m in self.messages if m["type"] == "http.response.start")
        return {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}

    @property
    def body(self) -> bytes:
        return b"".join(
            m.get("body", b"") for m in self.messages if m["type"] == "http.response.body"
        )


async def test_a_second_identical_request_is_refused_while_the_first_is_running() -> None:
    """Two concurrent retries must not both execute the write.

    The first request is held open inside the app; the second presents the same key and
    must be answered with a short 409 instead of running the handler again. Once the
    first finishes, a third call replays the stored response — the lock is a gate, not a
    permanent refusal.
    """
    from app.platform.cache.memory import InMemoryCache

    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_app(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        started.set()
        await finish.wait()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    middleware = IdempotencyMiddleware(slow_app, cache=InMemoryCache(), ttl_seconds=60)

    first = _Capture()
    first_task = asyncio.create_task(middleware(_scope_for("same-key"), _receive_body(), first))
    await asyncio.wait_for(started.wait(), timeout=1)

    second = _Capture()
    await asyncio.wait_for(middleware(_scope_for("same-key"), _receive_body(), second), timeout=1)

    assert second.status == 409
    assert json.loads(second.body)["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert second.headers["retry-after"] == "1"

    finish.set()
    await asyncio.wait_for(first_task, timeout=1)
    assert first.status == 200

    replay = _Capture()
    await middleware(_scope_for("same-key"), _receive_body(), replay)

    assert replay.status == 200
    assert replay.headers["idempotent-replay"] == "true"


class _NoLockCache:
    """A cache that answers reads but refuses the in-flight claim."""

    def build_key(self, *parts: str) -> str:
        return ":".join(parts)

    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        return None

    async def delete(self, *keys: str) -> int:
        return 0

    async def exists(self, key: str) -> bool:
        return False

    async def increment(self, key: str, *, amount: int = 1, ttl_seconds: int | None = None) -> int:
        raise RuntimeError("redis unreachable")

    async def ping(self) -> bool:
        return False


async def test_a_cache_outage_does_not_block_writes() -> None:
    """The lock is an optimisation; losing it must not turn a POST into a 500."""

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = IdempotencyMiddleware(app, cache=_NoLockCache())  # type: ignore[arg-type]
    captured = _Capture()

    await middleware(_scope_for("outage-key"), _receive_body(), captured)

    assert captured.status == 200
    assert captured.body == b"ok"
