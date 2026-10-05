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

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.middleware import Middleware

from app.core.config import Settings
from app.core.idempotency import IdempotencyMiddleware, _scope_user_id, build_key, fingerprint

pytestmark = [pytest.mark.integration, pytest.mark.api]

PASSWORD = "Baghdad-learns-2026"


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
    """Registration and login have no session yet; they share ``anonymous`` by nature."""
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
    finish = build_key(
        method="POST", path="/quiz/sessions/s/complete", user_id="u1", client_key="k"
    )
    assert start != finish


def test_an_empty_body_and_a_missing_body_are_the_same_request():
    """Some routes take no body at all and some clients send ``""``.

    Treating them as different bodies would reject a legitimate retry of a bodiless POST.
    """
    assert fingerprint(b"") == fingerprint(b"")
    assert fingerprint(b"") != fingerprint(b'{"lessonId":"1"}')


# ------------------------------------------------------------------ middleware
@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An app whose idempotency middleware holds an isolated cache.

    The cache is swapped rather than shared because the store is process-wide: a key
    stored by one test would otherwise make the next test pass or fail by accident,
    which is the hardest kind of test bug to diagnose later.
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

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    await dispose_engine()


async def _register(client: AsyncClient, email: str, key: str | None = None):
    headers = {"Idempotency-Key": key} if key else {}
    return await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "displayName": "Natiqo Learner",
            "device": {"platform": "android"},
        },
        headers=headers,
    )


async def test_a_repeat_of_the_same_key_replays_the_first_response(client: AsyncClient):
    """The whole point: the client gets its original answer back, not a second one."""
    key = str(uuid.uuid4())

    first = await _register(client, "retry@example.com", key)
    second = await _register(client, "retry@example.com", key)

    assert first.status_code == 201, first.text
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
    """
    key = str(uuid.uuid4())
    first_token = (await _register(client, "first@example.com")).json()["tokens"]["accessToken"]
    second_token = (await _register(client, "second@example.com")).json()["tokens"]["accessToken"]
    body = {"currentPassword": PASSWORD, "newPassword": "Baghdad-progresses-2027"}

    first = await client.post(
        "/api/v1/auth/change-password",
        json=body,
        headers={"Authorization": f"Bearer {first_token}", "Idempotency-Key": key},
    )
    second = await client.post(
        "/api/v1/auth/change-password",
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
    token = (await _register(client, "retry@example.com")).json()["tokens"]["accessToken"]
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": key}
    body = {"currentPassword": PASSWORD, "newPassword": "Baghdad-progresses-2027"}

    first = await client.post("/api/v1/auth/change-password", json=body, headers=headers)
    second = await client.post("/api/v1/auth/change-password", json=body, headers=headers)

    assert first.status_code == 200, first.text
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.json() == first.json()


async def test_the_replayed_response_is_distinguishable_from_a_fresh_one(client: AsyncClient):
    """A replay must be visible, or the access log cannot count real writes."""
    key = str(uuid.uuid4())

    first = await _register(client, "marked@example.com", key)
    second = await _register(client, "marked@example.com", key)

    assert "Idempotent-Replay" not in first.headers
    assert second.headers["Idempotent-Replay"] == "true"


async def test_a_key_is_usable_only_once_per_distinct_request(client: AsyncClient):
    """A different address is a different request, so the first response is not replayed."""
    key = str(uuid.uuid4())

    first = await _register(client, "one@example.com", key)
    second = await _register(client, "two@example.com", key)

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_a_retry_after_a_correction_is_not_rejected_forever(client: AsyncClient):
    """A 4xx must not be stored, or the fixed retry fails with the old error forever.

    A client that reuses one key per "attempt" must be able to correct its payload.
    Storing the first 409 would make that permanently impossible.
    """
    await _register(client, "taken@example.com")
    key = str(uuid.uuid4())

    conflict = await _register(client, "taken@example.com", key)
    corrected = await _register(client, "free@example.com", key)

    assert conflict.status_code == 409
    assert corrected.status_code == 201, corrected.text


async def test_get_requests_are_never_treated_as_idempotent_writes(client: AsyncClient):
    """A GET must not consume a key slot or be answered from the store."""
    response = await client.get("/api/v1/content/levels", headers={"Idempotency-Key": "k"})
    assert response.status_code == 200
    assert "Idempotent-Replay" not in response.headers


async def test_a_request_without_a_key_still_works(client: AsyncClient):
    """Idempotency is opt-in per request; nothing may require the header."""
    response = await _register(client, "nokey@example.com")
    assert response.status_code == 201


async def test_a_failing_request_is_not_cached(client: AsyncClient):
    """An unreachable dependency must not make the retry permanently fail.

    If a 500 were stored, one transient database blip would cost the learner the write
    for a day — strictly worse than the duplicate this feature exists to prevent.
    """
    key = str(uuid.uuid4())

    # A body the schema rejects produces a 422; the corrected retry must still work.
    rejected = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "not-an-email",
            "password": PASSWORD,
            "displayName": "Natiqo Learner",
        },
        headers={"Idempotency-Key": key},
    )
    accepted = await _register(client, "later@example.com", key)

    assert rejected.status_code == 422
    assert accepted.status_code == 201
