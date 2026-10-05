"""Push notification port (Firebase Cloud Messaging in production)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.core.types import UserId


class PushType(StrEnum):
    """Notification taxonomy. Copy itself lives in the content system, not here."""

    DAILY_REMINDER = "daily_reminder"
    STREAK_WARNING = "streak_warning"
    STREAK_MAINTAINED = "streak_maintained"
    ACHIEVEMENT = "achievement"
    NEW_CONTENT = "new_content"
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True, slots=True)
class PushMessage:
    title: str
    body: str
    data: dict[str, str]
    type: PushType
    #: Deep-link target the Flutter app should open.
    target: str | None = None


@dataclass(frozen=True, slots=True)
class PushResult:
    success_count: int
    failure_count: int
    invalid_tokens: tuple[str, ...] = ()


@runtime_checkable
class PushPort(Protocol):
    async def send_to_user(
        self, user_id: UserId, tokens: Sequence[str], message: PushMessage
    ) -> PushResult: ...

    async def send_to_users(
        self, recipients: dict[UserId, Sequence[str]], message: PushMessage
    ) -> PushResult: ...

    async def validate_token(self, token: str) -> bool: ...

    async def ping(self) -> bool: ...
