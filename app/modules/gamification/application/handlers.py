"""Wiring that turns domain events into gamification projections.

The bus is built per request and bound to the request's unit of work, so every subscriber
writes through the *same* transaction that emits the event. That is what makes a lesson
completion and the stats it earned atomic: the handler adds rows to the open session and the
completing service commits them together.

The ``LessonCompleted`` subscription is registered ``critical``: a broken projection must
roll the completion back rather than commit a lesson whose XP and achievements were only
half recorded. That is the opposite of the bus's default, which shields the emitter from a
failing side-effect — appropriate for analytics, wrong for a durable product projection.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.events import InProcessEventBus, LessonCompleted
from app.core.uow import UnitOfWorkProtocol
from app.modules.gamification.application.service import GamificationService

__all__ = ["build_gamification_bus", "register_gamification_handlers"]


def register_gamification_handlers(
    bus: InProcessEventBus, uow: UnitOfWorkProtocol, settings: Settings
) -> None:
    """Subscribe the gamification projection to the events it maintains state from."""
    service = GamificationService(uow, settings)

    async def _on_lesson_completed(event: LessonCompleted) -> None:
        await service.apply_lesson_completed(
            event.user_id, xp_awarded=event.xp_awarded, on=event.occurred_at
        )

    bus.subscribe(LessonCompleted, _on_lesson_completed, critical=True)


def build_gamification_bus(uow: UnitOfWorkProtocol, settings: Settings) -> InProcessEventBus:
    """A fresh bus, bound to ``uow``, with the gamification subscribers attached."""
    bus = InProcessEventBus()
    register_gamification_handlers(bus, uow, settings)
    return bus
