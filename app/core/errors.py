"""Stable, machine-readable error codes and the framework-free exception hierarchy.

This module is imported by domain code, so it must not depend on FastAPI,
SQLAlchemy, Redis or any other delivery/infrastructure concern.

The API contract guarantees that clients can branch on ``code`` and never need to
parse human-readable ``message`` text.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Canonical error codes. Values are part of the public API contract."""

    # ---------------------------------------------------------------- generic
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"

    # ------------------------------------------------------------------ auth
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"  # noqa: S105 - error code, not a secret
    AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"  # noqa: S105 - error code, not a secret
    AUTH_TOKEN_REVOKED = "AUTH_TOKEN_REVOKED"  # noqa: S105 - error code, not a secret
    AUTH_ACCOUNT_LOCKED = "AUTH_ACCOUNT_LOCKED"
    AUTH_ACCOUNT_DISABLED = "AUTH_ACCOUNT_DISABLED"
    AUTH_EMAIL_NOT_VERIFIED = "AUTH_EMAIL_NOT_VERIFIED"
    AUTH_ACCOUNT_EXISTS = "AUTH_ACCOUNT_EXISTS"
    AUTH_WEAK_PASSWORD = "AUTH_WEAK_PASSWORD"  # noqa: S105 - error code, not a secret
    AUTH_REFRESH_INVALID = "AUTH_REFRESH_INVALID"
    AUTH_REFRESH_REUSED = "AUTH_REFRESH_REUSED"
    AUTH_FORBIDDEN = "AUTH_FORBIDDEN"
    AUTH_INSUFFICIENT_ROLE = "AUTH_INSUFFICIENT_ROLE"
    AUTH_LOGIN_ATTEMPTS_EXCEEDED = "AUTH_LOGIN_ATTEMPTS_EXCEEDED"
    #: The Google/Apple ID token failed signature, issuer, audience or expiry checks.
    AUTH_IDENTITY_INVALID = "AUTH_IDENTITY_INVALID"
    #: The provider shared no usable email, so a new account cannot be keyed.
    AUTH_IDENTITY_INCOMPLETE = "AUTH_IDENTITY_INCOMPLETE"
    #: The address is already registered and the provider did not vouch for it.
    AUTH_IDENTITY_CONFLICT = "AUTH_IDENTITY_CONFLICT"
    #: Sign-in was requested from a provider this deployment has not enabled.
    AUTH_PROVIDER_UNAVAILABLE = "AUTH_PROVIDER_UNAVAILABLE"

    # ------------------------------------------------------------------ user
    USER_NOT_FOUND = "USER_NOT_FOUND"
    USER_DELETED = "USER_DELETED"

    # --------------------------------------------------------------- content
    LEVEL_NOT_FOUND = "LEVEL_NOT_FOUND"
    UNIT_NOT_FOUND = "UNIT_NOT_FOUND"
    LESSON_NOT_FOUND = "LESSON_NOT_FOUND"
    LESSON_NOT_AVAILABLE = "LESSON_NOT_AVAILABLE"
    LESSON_LOCKED = "LESSON_LOCKED"
    LESSON_ALREADY_COMPLETED = "LESSON_ALREADY_COMPLETED"
    CONTENT_NOT_PUBLISHED = "CONTENT_NOT_PUBLISHED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"

    # ------------------------------------------------------ vocab / grammar
    WORD_NOT_FOUND = "WORD_NOT_FOUND"
    GRAMMAR_TOPIC_NOT_FOUND = "GRAMMAR_TOPIC_NOT_FOUND"

    # -------------------------------------------------------------- question
    QUESTION_NOT_FOUND = "QUESTION_NOT_FOUND"
    QUESTION_TYPE_UNSUPPORTED = "QUESTION_TYPE_UNSUPPORTED"
    QUESTION_ALREADY_ANSWERED = "QUESTION_ALREADY_ANSWERED"
    QUESTION_NOT_IN_SESSION = "QUESTION_NOT_IN_SESSION"

    # ------------------------------------------------------------------ quiz
    QUIZ_SESSION_NOT_FOUND = "QUIZ_SESSION_NOT_FOUND"
    QUIZ_SESSION_EXPIRED = "QUIZ_SESSION_EXPIRED"
    QUIZ_ALREADY_COMPLETED = "QUIZ_ALREADY_COMPLETED"
    QUIZ_NOT_COMPLETED = "QUIZ_NOT_COMPLETED"

    # -------------------------------------------------------------- progress
    PROGRESS_NOT_FOUND = "PROGRESS_NOT_FOUND"

    # ---------------------------------------------------------------- review
    REVIEW_NOTHING_DUE = "REVIEW_NOTHING_DUE"

    # ---------------------------------------------------------- gamification
    INSUFFICIENT_HEARTS = "INSUFFICIENT_HEARTS"
    PREMIUM_REQUIRED = "PREMIUM_REQUIRED"
    ACHIEVEMENT_NOT_FOUND = "ACHIEVEMENT_NOT_FOUND"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    NO_FREEZES_AVAILABLE = "NO_FREEZES_AVAILABLE"

    # --------------------------------------------------------------- economy
    SUBSCRIPTION_NOT_FOUND = "SUBSCRIPTION_NOT_FOUND"
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    PAYMENT_INVALID_SIGNATURE = "PAYMENT_INVALID_SIGNATURE"
    PAYMENT_EVENT_ALREADY_PROCESSED = "PAYMENT_EVENT_ALREADY_PROCESSED"

    # --------------------------------------------------------- notifications
    NOTIFICATION_DISABLED = "NOTIFICATION_DISABLED"
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    DEVICE_ALREADY_REGISTERED = "DEVICE_ALREADY_REGISTERED"

    # ----------------------------------------------------------------- media
    MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"
    MEDIA_TYPE_NOT_ALLOWED = "MEDIA_TYPE_NOT_ALLOWED"
    MEDIA_UPLOAD_FAILED = "MEDIA_UPLOAD_FAILED"

    # -------------------------------------------------------------------- ai
    AI_DISABLED = "AI_DISABLED"
    AI_QUOTA_EXCEEDED = "AI_QUOTA_EXCEEDED"
    AI_PROVIDER_ERROR = "AI_PROVIDER_ERROR"
    AI_TIMEOUT = "AI_TIMEOUT"
    AI_UNSAFE_INPUT = "AI_UNSAFE_INPUT"
    #: A tutor thread id that does not exist, or does not belong to the caller.
    CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"

    # ------------------------------------------------------------------ jobs
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_FAILED = "JOB_FAILED"


class AppError(Exception):
    """Base class for every expected, client-visible failure.

    Subclasses map to a stable :class:`ErrorCode`, an HTTP status and an optional
    ``details`` payload that is safe to expose to the client.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 500
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        details: dict[str, Any] | None = None,
        http_status: int | None = None,
    ) -> None:
        self.message = message or type(self).message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": str(self.code), "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


# --------------------------------------------------------------- 4xx families
class ValidationFailed(AppError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 422
    message = "The request payload failed validation."


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND
    http_status = 404
    message = "The requested resource was not found."


class ConflictError(AppError):
    code = ErrorCode.CONFLICT
    http_status = 409
    message = "The request conflicts with the current state of the resource."


class ForbiddenError(AppError):
    code = ErrorCode.AUTH_FORBIDDEN
    http_status = 403
    message = "You do not have permission to perform this action."


class UnauthorizedError(AppError):
    code = ErrorCode.AUTH_TOKEN_INVALID
    http_status = 401
    message = "Authentication is required."


class RateLimitedError(AppError):
    code = ErrorCode.RATE_LIMITED
    http_status = 429
    message = "Too many requests. Please slow down."

    def __init__(self, message: str | None = None, *, retry_after: int = 60, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after
        self.details.setdefault("retry_after_seconds", retry_after)


class PayloadTooLargeError(AppError):
    code = ErrorCode.PAYLOAD_TOO_LARGE
    http_status = 413
    message = "The request body is too large."


class PremiumRequiredError(AppError):
    code = ErrorCode.PREMIUM_REQUIRED
    http_status = 402
    message = "This feature requires an active subscription."


class InsufficientHeartsError(AppError):
    code = ErrorCode.INSUFFICIENT_HEARTS
    http_status = 403
    message = "You have no hearts left. Wait to regenerate or upgrade."

    def __init__(
        self, message: str | None = None, *, next_heart_in_seconds: int = 0, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.details.setdefault("next_heart_in_seconds", next_heart_in_seconds)


class ServiceUnavailableError(AppError):
    code = ErrorCode.INTERNAL_ERROR
    http_status = 503
    message = "The service is temporarily unavailable."
