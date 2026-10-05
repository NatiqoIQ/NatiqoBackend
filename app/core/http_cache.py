"""Opt-in HTTP caching for responses that are safe to share.

Every response this API emits is ``no-store`` by default, and for the great majority of
them that is correct: an auth response, a profile, or an ai-usage record must never sit in
a shared cache or on a phone that someone else can pick up.

The public catalogue is the exception. It is unauthenticated, contains no user data, and
changes only when an editor publishes something — which is precisely the access pattern
HTTP caching was built for. Leaving it ``no-store`` means a mobile client re-downloads the
entire curriculum on every cold start, over metered data, to learn nothing that changed.

So caching is a property a route *declares*, not something inferred from a path prefix:

    @router.get("/levels")
    @cacheable(max_age=60, stale_while_revalidate=600)
    async def list_levels(...): ...

The marker is an attribute on the handler, which the middleware reads off
``request.scope["endpoint"]``. A route that forgets the decorator stays ``no-store``, which
is the safe direction to fail in: the cost of forgetting is a slower app, not a cached
password reset link.

**Why not a CDN or a reverse proxy doing this?** Both can, but neither knows which of this
API's responses are public. Encoding the decision in the route keeps it next to the code
that would have to justify it, and keeps it reviewable.

**Why an ETag as well as ``max-age``?** The two answer different questions. ``max-age``
covers the common case — a recently used level list, served from the phone with no network
at all. The ETag covers the rest: a client that already holds a stale copy revalidates and
receives ``304`` with no body, instead of the full JSON. Together they mean an app that
launches daily costs one round trip per changed endpoint and nothing per unchanged one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, TypeVar

from fastapi import Request
from starlette.responses import Response

#: A route handler of any signature. The decorator returns it unchanged, so this is only
#: ever a constraint on what may be marked, not on what it becomes.
F = TypeVar("F", bound=Callable[..., object])

#: Attribute the decorator leaves on the handler. Read by the middleware through the ASGI
#: scope, so it is part of this module's contract with :mod:`app.core.middleware`.
CACHE_POLICY_ATTR: Final = "__cache_policy__"

#: Second granularity is enough: revalidating more often than this costs a round trip for
#: a save the mobile connection cannot notice.
_DEFAULT_MAX_AGE: Final = 60


@dataclass(frozen=True)
class CachePolicy:
    """How long one route's response may be reused.

    ``stale_while_revalidate`` is the mobile-specific part: it lets a client keep showing
    the copy it already has — a lesson list is far better shown slightly out of date than
    shown as a spinner — while it refreshes in the background.
    """

    max_age: int = _DEFAULT_MAX_AGE
    stale_while_revalidate: int | None = None
    #: Only GET and HEAD are cacheable by definition. A POST that happens to return a
    #: public body is still a side effect the client must not assume it can replay.
    public: bool = True

    @property
    def cache_control(self) -> str:
        value = f"public, max-age={self.max_age}"
        if self.stale_while_revalidate is not None:
            value += f", stale-while-revalidate={self.stale_while_revalidate}"
        return value


def cacheable(
    *,
    max_age: int = _DEFAULT_MAX_AGE,
    stale_while_revalidate: int | None = None,
) -> Callable[[F], F]:
    """Mark a route's successful GET responses as publicly cacheable.

    The decorator returns the handler completely unchanged — same signature, same return
    type — so it costs nothing at runtime, confuses neither FastAPI's dependency analysis
    nor a type checker, and does not interfere with the route's own decorators. Keeping it
    directly above ``@router.get`` puts the marker where a reviewer of a caching decision
    will look.

    ``F`` rather than a concrete callable type on purpose: a FastAPI handler returns a
    Pydantic model, not a ``Response``, and typing this as ``Callable[..., Response]`` would
    reject every route it is meant to annotate.
    """

    def decorate(handler: F) -> F:
        setattr(
            handler,
            CACHE_POLICY_ATTR,
            CachePolicy(max_age=max_age, stale_while_revalidate=stale_while_revalidate),
        )
        return handler

    return decorate


def policy_for(request: Request) -> CachePolicy | None:
    """The cache policy the matched route declared, or ``None`` if it declared none.

    Returning ``None`` for an unmatched path, a non-idempotent method, or a handler with no
    marker is what makes ``no-store`` the default rather than an omission: an unmatched path
    has no endpoint to ask, and asking anyway would guess.
    """
    if request.method not in ("GET", "HEAD"):
        return None
    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return None
    policy = getattr(endpoint, CACHE_POLICY_ATTR, None)
    return policy if isinstance(policy, CachePolicy) else None


def etag_for(body: bytes) -> str:
    """A strong validator over the exact bytes served.

    Strong rather than weak on purpose: the JSON is produced once per response and hashed
    as it will be sent, so there is no risk of a semantically-equal-but-different body and
    no reason to pay for the extra precision a weak validator would need.

    A truncated digest is enough. An ETag only has to distinguish responses within one
    cache's lifetime, and 16 bytes makes a collision here a non-event while keeping the
    header short enough to be worth setting.
    """
    return f'"{hashlib.sha256(body).hexdigest()[:32]}"'


def matches_etag(header_value: str | None, etag: str) -> bool:
    """Whether a client's ``If-None-Match`` already holds this exact representation.

    Handles the two shapes a client may send: a single tag, or a list including ``*``.
    The ``W/`` prefix is stripped because a client may have stored a weak validator from an
    intermediary and still be asking about the same body.
    """
    if not header_value:
        return False
    candidates = {part.strip() for part in header_value.split(",")}
    if "*" in candidates:
        return True
    return etag in candidates or f"W/{etag}" in candidates


def not_modified(etag: str, policy: CachePolicy) -> Response:
    """A ``304`` carrying the validators and nothing else.

    The caching directives are repeated from the policy on purpose. A 304 is how the client
    learns that its stored copy is *still* fresh, so the response that says so has to carry
    the same ``max-age`` the 200 did — repeating ``max-age=0, must-revalidate`` instead
    would demote a response the client had just proven was current, and every launch would
    pay a round trip again.
    """
    return Response(
        status_code=304,
        headers={"ETag": etag, "Cache-Control": policy.cache_control},
    )
