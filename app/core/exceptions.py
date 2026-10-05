"""Global exception handling.

Anything that escapes a handler is converted into the stable error envelope. Two
hard rules:
- Production stack traces are never returned to the client (only a request id).
- Every response carries the correlation id, so a user-reported failure can be traced
  without exposing internals.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorCode, RateLimitedError
from app.core.logging import get_logger, request_id_var
from app.core.responses import ErrorBody, ErrorResponse

logger = get_logger(__name__)

#: OpenAPI error responses reused by routers via ``responses={...}``.
#:
#: The keys are **strings, not ints, and that is load-bearing.** ``fastapi.openapi.utils``
#: decides whether to inject its own ``422`` with a ``HTTPValidationError`` body by testing
#: ``if status not in operation["responses"] for status in ["422", "4XX", "default"]`` — it
#: looks up the *string*. An ``int`` key leaves the lookup unsatisfied, FastAPI injects the
#: stock shape, and the published schema then promises the client an error envelope we never
#: send. Publishing a lie in the schema is worse than publishing nothing: a generated client
#: deserialises ``detail``/``loc`` and finds neither. See ``docs/client-contract.md``.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "400": {"model": ErrorResponse, "description": "Malformed request"},
    "401": {"model": ErrorResponse, "description": "Missing or invalid credentials"},
    "403": {"model": ErrorResponse, "description": "Authenticated but not permitted"},
    "404": {"model": ErrorResponse, "description": "Resource not found"},
    "409": {"model": ErrorResponse, "description": "Conflicting state"},
    "422": {"model": ErrorResponse, "description": "Validation failed"},
    "429": {"model": ErrorResponse, "description": "Rate limited"},
    "500": {"model": ErrorResponse, "description": "Internal error"},
}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None) or request_id_var.get()


def _json_error(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or None,
            request_id=_request_id(request),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(exclude_none=True),
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler to the application. Called once during startup."""

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        headers: dict[str, str] | None = None
        if isinstance(exc, RateLimitedError):
            headers = {"Retry-After": str(exc.retry_after)}
        if exc.http_status >= 500:
            logger.error("http.app_error", code=str(exc.code), detail=str(exc))
        else:
            logger.info("http.app_error", code=str(exc.code), detail=str(exc))
        return _json_error(
            request,
            status_code=exc.http_status,
            code=str(exc.code),
            message=exc.message,
            details=exc.details or None,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ()) if part != "body"),
                "issue": err.get("msg", "invalid value"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        logger.info("http.validation_error", fields=fields)
        return _json_error(
            request,
            # The `_CONTENT` spelling, not the deprecated `..._ENTITY` alias. The old
            # name still resolves on Starlette 1.7 but warns on access, and a warning
            # raised inside an exception handler is a 500 where a 422 belongs.
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=str(ErrorCode.VALIDATION_ERROR),
            message="The request payload failed validation.",
            details={"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: ErrorCode.AUTH_TOKEN_INVALID,
            403: ErrorCode.AUTH_FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            405: ErrorCode.NOT_FOUND,
            413: ErrorCode.PAYLOAD_TOO_LARGE,
            429: ErrorCode.RATE_LIMITED,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        headers = dict(getattr(exc, "headers", None) or {})
        return _json_error(
            request,
            status_code=exc.status_code,
            code=str(code),
            message=str(exc.detail) if exc.status_code < 500 else "Internal server error.",
            headers=headers or None,
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        # The driver message can name columns and values, so it is logged but never
        # returned to the client.
        logger.warning("http.integrity_error", detail=str(exc.orig))
        return _json_error(
            request,
            status_code=status.HTTP_409_CONFLICT,
            code=str(ErrorCode.CONFLICT),
            message="The request conflicts with existing data.",
        )

    @app.exception_handler(SQLAlchemyError)
    async def _sqlalchemy_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.error("http.database_error", detail=str(exc))
        return _json_error(
            request,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=str(ErrorCode.INTERNAL_ERROR),
            message="A database error occurred. Please retry.",
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("http.unhandled_error", error_type=type(exc).__name__)
        return _json_error(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=str(ErrorCode.INTERNAL_ERROR),
            message="An unexpected error occurred.",
            details={"request_id": _request_id(request)},
        )
