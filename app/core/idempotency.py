"""Idempotency for unsafe requests.

A phone on a bad connection retries. Without a guard, ``POST /quiz/sessions`` twice
creates two sessions, ``POST /subscription/checkout`` twice creates two charges, and
``POST /quiz/sessions/{id}/complete`` twice awards the XP twice. The user is billed for
the network, which is the one thing they did not choose.

The client sends ``Idempotency-Key: <client-generated uuid>``. This middleware stores the
*response* of the first successful attempt and replays it byte-for-byte on a repeat, so
the client sees the same ``sessionId`` it saw before.

Design decisions worth stating, because each one has a cheaper-looking alternative that
is wrong:

**The key is scoped to method + path + authenticated user.** Without the user component,
user A can replay user B's stored response by guessing a key, and without the path
component a key reused across endpoints returns the wrong body.

**Only 2xx responses are stored.** Storing a 4xx would make a *corrected* retry
permanently fail: the client fixes its payload, resends with the same key, and gets the
cached rejection forever. Storing a 5xx would make a transient database blip permanent.
Both are worse than the duplicate this feature exists to prevent.

**A replayed response is marked.** ``Idempotent-Replay: true`` lets the client and the
logs tell a replay from a fresh execution. It matters when debugging "why did my XP not
go up twice" and, more importantly, when reading the access log to count real writes.

**The store is the same cache the rest of the app uses.** That is a deliberate
limitation, not laziness: an idempotency record lost to an eviction means a duplicate is
possible, which is the pre-existing behaviour and not a new failure mode. Swapping in a
durable store is a matter of implementing one port.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger, request_id_var
from app.platform.cache.port import CachePort

logger = get_logger(__name__)

#: Methods that can change state. GET/HEAD are excluded: they are already idempotent and
#: caching their responses here would only consume the store.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: How long a key is honoured. Long enough to cover a client retrying after a night of
#: bad signal, short enough that the store does not accumulate every key ever sent.
DEFAULT_TTL_SECONDS = 86_400

#: Returned when a key is reused with a *different* body. Replaying the old response
#: would be worse than failing: the client would believe its new payload took effect.
KEY_CONFLICT_STATUS = 409

#: Headers that describe *this emission's* framing rather than its content. They are
#: recomputed on replay, because the stored body is the same bytes but the surrounding
#: transport is not. ``content-encoding`` is deliberately absent: the stored bytes are
#: whatever came off the wire, compressed or not, so the header has to travel with them.
_FRAMING_HEADERS = frozenset({"content-length", "transfer-encoding", "connection"})

KEY_CONFLICT_BODY: dict[str, Any] = {
    "error": {
        "code": "IDEMPOTENCY_KEY_REUSED",
        "message": "This Idempotency-Key was already used with a different request body.",
        "details": None,
        "requestId": None,
    }
}


def fingerprint(payload: bytes) -> str:
    """Stable hash of a request body, used to detect key reuse with different content."""
    if not payload:
        return "empty"
    return hashlib.sha256(payload).hexdigest()[:32]


#: Prefix of the ``Authorization`` header, lower-cased: ASGI header names arrive as
#: lower-case bytes.
_AUTHORIZATION = b"authorization"


def build_key(*, method: str, path: str, user_id: str | None, client_key: str) -> str:
    """Namespace a client key so it cannot collide across users, paths or methods."""
    scope = f"{method.upper()} {path} {user_id or 'anonymous'}"
    digest = hashlib.sha256(scope.encode()).hexdigest()[:16]
    return f"idem:{method.lower()}:{digest}:{client_key}"


def _scope_user_id(scope: Scope) -> str | None:
    """Partition requests per caller, without verifying anything.

    The goal here is narrow and worth stating precisely: this must not be *forgeable*, and
    it must not need to be an identity claim. It only has to partition the keyspace so a
    client-supplied key cannot reach another caller's stored response.

    Two properties follow from that:

    * The JWT is **not** verified here. Verification belongs to the route, which does it
      anyway; a second verification in middleware would be a cost with no new guarantee,
      since this value is never used to make an authorisation decision.
    * The partition is the *digest of the presented bearer token*, not its contents. The
      token cannot be forged without possessing it, so neither can the partition, and the
      digest is one-way so nothing about the caller is recoverable from a cache key.

    A previous middleware may still publish a real ``user_id`` on ``scope["state"]``, which
    is preferred when present: it keeps the partition stable across token rotation.

    Without this, every authenticated caller shares the ``anonymous`` partition, and a
    client who guessed another caller's key together with their method and path would be
    served that caller's stored response.
    """
    state = scope.get("state")
    if isinstance(state, dict):
        published = state.get("user_id")
        if published:
            return str(published)

    for name, value in scope.get("headers", ()):
        if name.lower() != _AUTHORIZATION:
            continue
        presented = value.strip()
        if not presented:
            return None
        return hashlib.sha256(presented).hexdigest()[:16]
    return None


async def _drain_body(receive: Receive) -> bytes:
    """Read the whole request body, leaving the app free to hand it on.

    The message is consumed rather than peeked, so the buffered bytes are replayed to
    the downstream app through :func:`_replay_body`. Getting this wrong is not a subtle
    corruption: the route simply never sees a body and blocks on the next receive.
    """
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay_body(body: bytes, fallback: Receive) -> Receive:
    """A ``receive`` that serves ``body`` once, then defers to the real one.

    The delegation matters for disconnects: a client that walks away mid-request must
    still be able to tell the app, or a streaming route would wait forever.
    """
    served = False

    async def receive() -> Message:
        nonlocal served
        if not served:
            served = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await fallback()

    return receive


class IdempotencyMiddleware:
    """Replay the stored response for a repeated ``Idempotency-Key``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        cache: CachePort,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.app = app
        self.cache = cache
        self.ttl_seconds = ttl_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Built without ``receive`` on purpose: reading the body here would consume the
        # stream the route is about to read, and the route would then block forever
        # waiting for a body that was already delivered.
        probe = Request(scope)
        client_key = probe.headers.get("Idempotency-Key")
        if probe.method not in UNSAFE_METHODS or not client_key:
            await self.app(scope, receive, send)
            return

        key = build_key(
            method=probe.method,
            path=probe.url.path,
            user_id=_scope_user_id(scope),
            client_key=client_key,
        )

        body = await _drain_body(receive)

        record = await self._load(key)
        if record is not None:
            incoming = fingerprint(body)
            if record["fingerprint"] not in ("empty", incoming):
                await self._send_conflict(send, scope)
                return
            await self._send_replay(send, scope, record)
            return

        captured = _ResponseCapture()
        await self.app(scope, _replay_body(body, receive), captured.wrap(send))
        if captured.status is not None and 200 <= captured.status < 300:
            await self._store(key, fingerprint(body), captured)

    async def _load(self, key: str) -> dict[str, Any] | None:
        raw = await self.cache.get(key)
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return None
        return record if isinstance(record, dict) else None

    async def _store(self, key: str, body_fingerprint: str, captured: _ResponseCapture) -> None:
        record = {
            "fingerprint": body_fingerprint,
            "status": captured.status,
            "headers": captured.headers,
            # base64 because the captured bytes are the *wire* form: this middleware sits
            # outside GZip, so what it stores may be compressed. Decoding as utf-8 here
            # would corrupt it irrecoverably, and a JSON string cannot hold arbitrary
            # bytes anyway.
            "body": base64.b64encode(captured.body).decode("ascii"),
        }
        try:
            await self.cache.set(key, json.dumps(record), ttl_seconds=self.ttl_seconds)
        except Exception:  # noqa: BLE001 - an evicted key must not fail the request
            logger.warning("idempotency.store_failed", key=key[:40])

    async def _send_replay(self, send: Send, scope: Scope, record: dict[str, Any]) -> None:
        raw: list[tuple[bytes, bytes]] = [
            (b"idempotent-replay", b"true"),
        ]
        # ``content-encoding`` is deliberately *kept*. The stored bytes are compressed, so
        # dropping the header would hand the client gzip it is not told about; keeping it
        # while re-encoding the body would corrupt it. Only the framing headers, which
        # describe this particular emission rather than the content, are rebuilt.
        for name, value in record.get("headers", {}).items():
            if name.lower() not in _FRAMING_HEADERS:
                raw.append((name.encode("latin-1"), value.encode("latin-1")))
        try:
            body = base64.b64decode(record.get("body", ""))
        except (ValueError, TypeError):  # pragma: no cover - defensive
            body = b""
        raw.append((b"content-length", str(len(body)).encode()))
        await self._emit(send, scope, record["status"], raw, body)

    async def _send_conflict(self, send: Send, scope: Scope) -> None:
        payload = KEY_CONFLICT_BODY["error"] | {"requestId": request_id_var.get()}
        body = json.dumps({"error": payload}).encode()
        logger.info("idempotency.key_reused_with_different_body")
        await self._emit(
            send,
            scope,
            KEY_CONFLICT_STATUS,
            [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            body,
        )

    async def _emit(
        self,
        send: Send,
        scope: Scope,
        status: int,
        raw_headers: list[tuple[bytes, bytes]],
        body: bytes,
    ) -> None:
        start: Message = {
            "type": "http.response.start",
            "status": status,
            "headers": raw_headers,
        }
        await send(start)
        await send({"type": "http.response.body", "body": body})


class _ResponseCapture:
    """Buffer an outgoing response so it can be persisted and replayed verbatim."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = b""

    def wrap(self, send: Send) -> Callable[[Message], Awaitable[None]]:
        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
                self.headers = {
                    key.decode("latin-1"): value.decode("latin-1")
                    for key, value in message.get("headers", [])
                }
            elif message["type"] == "http.response.body":
                self.body += message.get("body", b"")
            await send(message)

        return _send


def new_idempotency_key() -> str:  # pragma: no cover - convenience for scripts
    return str(uuid.uuid4())


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "KEY_CONFLICT_BODY",
    "UNSAFE_METHODS",
    "IdempotencyMiddleware",
    "build_key",
    "fingerprint",
    "new_idempotency_key",
]
