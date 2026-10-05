"""In-process domain event bus.

Plan §54 requires event-driven *internal* design without prematurely introducing a
distributed message bus. ``DomainEventBus`` dispatches synchronously inside the same
request/transaction so that side effects (XP, streak, achievements, analytics) either
happen with the aggregate that emitted them or not at all.

The interface is a port. Swapping in Redis Streams or a real broker later only
requires a new implementation of :class:`EventBus`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

from app.core.logging import get_logger
from app.core.time import utc_now
from app.core.types import UserId

logger = get_logger("events")

E = TypeVar("E", bound="DomainEvent")


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base class for every internal event.

    Events are immutable records of something that already happened. They carry no
    behaviour and never reach the client.
    """

    occurred_at: datetime = field(default_factory=utc_now, kw_only=True)
    event_id: uuid.UUID = field(default_factory=uuid.uuid4, kw_only=True)

    @property
    def name(self) -> str:
        return type(self).__name__


@runtime_checkable
class EventHandler(Protocol):
    def __call__(self, event: DomainEvent) -> None: ...


class EventBus(Protocol):
    """Port for event distribution."""

    def subscribe(self, event_type: type[DomainEvent], handler: Callable[[Any], None]) -> None: ...

    def publish(self, event: DomainEvent) -> None: ...


class InProcessEventBus:
    """Synchronous, in-transaction dispatcher.

    A failing handler is logged and re-raised only for handlers explicitly marked
    critical, so a broken analytics sink can never fail a lesson completion.
    """

    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], list[Callable[[Any], None]]] = {}
        self._critical: set[type[DomainEvent]] = set()

    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: Callable[[Any], None],
        *,
        critical: bool = False,
    ) -> None:
        self._handlers.setdefault(event_type, []).append(handler)
        if critical:
            self._critical.add(event_type)

    def publish(self, event: DomainEvent) -> None:
        for handler in self._handlers.get(type(event), []):
            try:
                handler(event)
            except Exception as exc:
                if type(event) in self._critical:
                    raise
                logger.warning(
                    "events.handler_failed",
                    event_name=event.name,
                    handler=getattr(handler, "__qualname__", repr(handler)),
                    error=str(exc),
                )

    def clear(self) -> None:
        self._handlers.clear()
        self._critical.clear()


# ------------------------------------------------------------- domain events
@dataclass(frozen=True, slots=True)
class UserRegistered(DomainEvent):
    user_id: UserId
    email: str


@dataclass(frozen=True, slots=True)
class LessonCompleted(DomainEvent):
    user_id: UserId
    lesson_id: uuid.UUID
    score_percent: int
    correct_count: int
    total_count: int
    duration_seconds: int
    is_retry: bool = False


@dataclass(frozen=True, slots=True)
class QuizCompleted(DomainEvent):
    user_id: UserId
    session_id: uuid.UUID
    lesson_id: uuid.UUID | None
    correct_count: int
    total_count: int
    score_percent: int


@dataclass(frozen=True, slots=True)
class QuestionAnswered(DomainEvent):
    user_id: UserId
    question_id: uuid.UUID
    skill: str
    is_correct: bool
    response_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ReviewSessionCompleted(DomainEvent):
    user_id: UserId
    reviewed_count: int
    correct_count: int


@dataclass(frozen=True, slots=True)
class DailyGoalCompleted(DomainEvent):
    user_id: UserId
    local_date: str
    xp_earned: int


@dataclass(frozen=True, slots=True)
class StreakUpdated(DomainEvent):
    user_id: UserId
    current_streak: int
    longest_streak: int
    local_date: str


@dataclass(frozen=True, slots=True)
class AchievementUnlocked(DomainEvent):
    user_id: UserId
    achievement_code: str
    xp_reward: int


@dataclass(frozen=True, slots=True)
class HeartsDepleted(DomainEvent):
    user_id: UserId
    hearts_remaining: int


@dataclass(frozen=True, slots=True)
class SubscriptionActivated(DomainEvent):
    user_id: UserId
    plan_code: str
    provider: str
    external_reference: str


@dataclass(frozen=True, slots=True)
class SubscriptionCancelled(DomainEvent):
    user_id: UserId
    plan_code: str


_event_bus: InProcessEventBus | None = None


def get_event_bus() -> InProcessEventBus:
    global _event_bus
    if _event_bus is None:
        _event_bus = InProcessEventBus()
    return _event_bus


def reset_event_bus() -> None:
    global _event_bus
    _event_bus = None
