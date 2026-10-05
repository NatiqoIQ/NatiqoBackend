"""ASGI middleware stack.

Installed in this order (outermost first):

1. :class:`SecurityHeadersMiddleware` — headers that must be on *every* response,
   including errors produced further in.
2. :class:`RequestContextMiddleware` — request/correlation id, access log, duration.
3. :class:`BodySizeLimitMiddleware` — reject oversized bodies before parsing.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.core.http_cache import etag_for, matches_etag, not_modified, policy_for
from app.core.logging import get_logger, request_id_var, user_id_var
from app.core.responses import ErrorBody, ErrorResponse

logger = get_logger("http")

REQUEST_ID_HEADER = "X-Request-ID"

#: Endpoints excluded from access logging noise (health probes).
_QUIET_PATHS = frozenset({"/health", "/ready", "/live", "/metrics"})

#: Policy for JSON API responses. ``default-src 'none'`` is the right default here
#: because nothing in the API is ever loaded as a document.
_API_CSP = "default-src 'none'; frame-ancestors 'none'"

#: Policy for the interactive docs page, which is the one response that *is* a
#: document. FastAPI renders it as HTML that pulls Swagger UI from jsDelivr and boots
#: through an inline <script>, so the API-wide ``default-src 'none'`` blocks the
#: stylesheet, the bundle, the favicon and the bootstrap script, and the page renders
#: blank. The API policy is unchanged; this one applies only to the docs routes, which
#: are not mounted in production at all.
#:
#: ``script-src`` keeps ``'unsafe-inline'`` because FastAPI inlines the SwaggerUIBundle
#: configuration, and there is no stable hash to pin: the template changes with the
#: FastAPI version. It is confined to a public, unauthenticated page that renders only
#: the public OpenAPI schema and holds no user data.
_DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src 'self' data: https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)

#: Paths that serve the interactive docs document.
_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect"})


def _csp_for(path: str) -> str:
    """Pick the policy for one path.

    The docs allowance is keyed on the path only, not on a setting: if the routes are
    not mounted, the path 404s and never reaches a response header, so there is nothing
    to leak by leaving the branch unconditional.
    """
    return _DOCS_CSP if path in _DOCS_PATHS else _API_CSP


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers.

    Applied to every response, including errors produced further in. The CSP is the
    strict JSON policy everywhere except the docs document, which needs its own — see
    :data:`_DOCS_CSP` for why a single policy cannot serve both.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        # setdefault: an explicit header set further in (e.g. the body-limit rejection)
        # always wins, so a rejection cannot have its security headers stripped.
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-site")
        headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(self), camera=()")
        headers.setdefault("Content-Security-Policy", _csp_for(request.url.path))
        # ``no-store`` is the correct default for almost everything here — an auth response
        # or a profile must not sit in a phone's cache — so a response has to *opt out* of
        # it. A route declares that with ``@cacheable(...)``; see ``app/core/http_cache.py``
        # for why the marker lives on the handler instead of in a path allowlist that could
        # silently start covering authenticated routes.
        #
        # Opting out is only allowed on success. On this catalogue a 404 is not an
        # assertion that the id is invalid, it is the same answer for "exists but is not
        # published yet", so a cacheable 404 would let an editor publish a lesson and have
        # learners keep the miss for the whole ``stale-while-revalidate`` window.
        cache_policy = policy_for(request)
        if cache_policy is None or response.status_code != 200:
            headers.setdefault("Cache-Control", "no-store")
        else:
            headers.setdefault("Cache-Control", cache_policy.cache_control)
        if get_settings().is_production:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds it to the log context and emits an access log."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = _sanitise_request_id(incoming) or uuid.uuid4().hex
        request.state.request_id = request_id

        token_request = request_id_var.set(request_id)
        token_user = user_id_var.set(None)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "http.request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise
        finally:
            request_id_var.reset(token_request)
            user_id_var.reset(token_user)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id

        path = request.url.path
        if path not in _QUIET_PATHS:
            log = logger.info if response.status_code < 500 else logger.warning
            log(
                "http.request",
                method=request.method,
                path=path,
                status_code=response.status_code,
                duration_ms=duration_ms,
                client_ip=_client_ip(request),
                user_agent=request.headers.get("user-agent", "")[:120],
            )
        return response


class BodySizeLimitMiddleware:
    """Reject bodies larger than the configured limit.

    Implemented as raw ASGI (not ``BaseHTTPMiddleware``) so it can inspect
    ``Content-Length`` and stream-count the body *before* anything is buffered or
    parsed — that is what actually protects against memory exhaustion.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        declared = headers.get("content-length")
        if declared is not None:
            try:
                declared_length = int(declared)
            except ValueError:
                declared_length = 0
            if declared_length > self.max_bytes:
                await self._reject(scope, send, declared_length=declared_length)
                return

        received = 0
        state = {"started": False}

        async def guarded_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, counting_receive, guarded_send)
        except _BodyTooLarge:
            if not state["started"]:
                await self._reject(scope, send, declared_length=received)

    async def _reject(self, scope: Scope, send: Send, *, declared_length: int) -> None:
        # Sanitised: this value is echoed into a response header, so an unsanitised
        # client-supplied id would be a response-splitting vector.
        request_id = (
            _sanitise_request_id(_header_value(scope, REQUEST_ID_HEADER)) or uuid.uuid4().hex
        )
        body = ErrorResponse(
            error=ErrorBody(
                code=str(ErrorCode.PAYLOAD_TOO_LARGE),
                message="The request body exceeds the maximum allowed size.",
                details={"max_bytes": self.max_bytes, "received_bytes": declared_length},
                request_id=request_id,
            )
        )
        raw = json.dumps(body.model_dump(exclude_none=True)).encode("utf-8")
        logger.warning("http.body_too_large", max_bytes=self.max_bytes, received=declared_length)
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(raw)).encode()),
                    (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": raw})


class _BodyTooLarge(Exception):
    """Internal signal raised from the receive wrapper when the cap is passed."""


def _header_value(scope: Scope, name: str) -> str | None:
    target = name.lower().encode()
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode("latin-1")
    return None


def _sanitise_request_id(value: str | None) -> str | None:
    """Never trust a client-supplied correlation id in logs or headers."""
    if not value:
        return None
    cleaned = "".join(ch for ch in value if ch.isalnum() or ch in "-_")[:64]
    return cleaned or None


def _client_ip(request: Request) -> str | None:
    """Log-safe client identity. ``X-Forwarded-For`` is only trusted behind our proxy."""
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


class ResponseCacheMiddleware(BaseHTTPMiddleware):
    """Adds ``ETag``/``304`` revalidation to routes marked ``@cacheable``.

    A separate middleware from the security headers, and the reason is the body: computing
    a validator means reading every byte of the response. Doing that here confines the cost
    — and the risk of mishandling a streamed body — to the handful of routes that opted in.
    Everything else is passed through untouched.

    Registered as the **outermost** layer. That placement is load-bearing: the ETag has to
    describe the bytes the client actually receives, and by the time a response arrives here
    GZip has already run. Hashing the uncompressed body and labelling it with a strong
    validator would violate RFC 9110 §8.8.3, where the validator must match the selected
    representation — the client holding that mismatched tag could never revalidate to a
    304, which is the entire point of sending one. The gzip and identity representations
    therefore get distinct tags, and GZip's ``Vary: Accept-Encoding`` keeps caches from
    mixing them up.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        # HEAD is deliberately excluded: its body is empty, so hashing it would mint a
        # validator that does not match the GET a client would then revalidate against.
        policy = policy_for(request)
        if request.method != "GET" or policy is None:
            return response
        # Only a success carries a cacheable body. A 404 or a 422 gets no validator, so a
        # client cannot pin an error page by accident while it waits for content to appear.
        if response.status_code != 200:
            return response
        # ``BaseHTTPMiddleware`` hands back an internal streaming response, but its declared
        # type is the plain ``Response``, which has no ``body_iterator``. Asked for by name
        # rather than asserted, so a route that ever returns a fully buffered response is
        # simply passed through instead of failing at runtime.
        iterator = getattr(response, "body_iterator", None)
        if iterator is None:
            return response

        body = b"".join([chunk async for chunk in iterator])
        etag = etag_for(body)
        if matches_etag(request.headers.get("if-none-match"), etag):
            return not_modified(etag, policy)

        headers = MutableHeaders(raw=list(response.raw_headers))
        headers["ETag"] = etag
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            background=response.background,
        )


def register_middleware(app: ASGIApp) -> None:
    """Install the middleware stack on a FastAPI/Starlette application."""
    from fastapi import FastAPI

    settings = get_settings()
    if not isinstance(app, FastAPI):  # pragma: no cover - defensive
        raise TypeError("register_middleware expects a FastAPI application")
    # Starlette executes the most recently added middleware first, so registration order is
    # reversed to read outer-to-inner below. Two placements are deliberate: GZip runs inside
    # ResponseCacheMiddleware (see that class for why the ETag must cover compressed bytes),
    # and RequestContextMiddleware sits inside SecurityHeadersMiddleware so the CSP branch
    # can tell the docs page from the API without re-deriving the path.
    app.add_middleware(ResponseCacheMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
