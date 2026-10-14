"""Row → aggregate mappers for spaced repetition.

Hand-written like the rest of the project, so the SQL shape can change without the domain
noticing. The card is a mutable aggregate and so converts both ways; the log is written
once and only ever converts out of the domain.
"""

from __future__ import annotations

from app.core.time import ensure_aware
from app.core.types import UserId
from app.modules.quiz.domain.review import ReviewCard, ReviewLog, ReviewState
from app.modules.quiz.infrastructure.review_models import (
    ReviewCardModel,
    ReviewLogModel,
)


def to_card(model: ReviewCardModel) -> ReviewCard:
    # Re-attach UTC on the way out: SQLite drops tzinfo, and the domain compares this
    # against an aware ``utc_now()`` (see ``ReviewCard.schedule``).
    return ReviewCard(
        id=model.id,
        user_id=UserId(model.user_id),
        question_id=model.question_id,
        ease_factor=model.ease_factor,
        interval_days=model.interval_days,
        repetitions=model.repetitions,
        lapses=model.lapses,
        due_at=ensure_aware(model.due_at),
        last_reviewed_at=(ensure_aware(model.last_reviewed_at) if model.last_reviewed_at else None),
        state=ReviewState(model.state),
    )


def to_log(model: ReviewLogModel) -> ReviewLog:
    return ReviewLog(
        id=model.id,
        user_id=UserId(model.user_id),
        card_id=model.card_id,
        quality=model.quality,
        previous_interval_days=model.previous_interval_days,
        new_interval_days=model.new_interval_days,
        reviewed_at=ensure_aware(model.reviewed_at),
    )


def to_card_model(card: ReviewCard) -> ReviewCardModel:
    return ReviewCardModel(
        id=card.id,
        user_id=card.user_id,
        question_id=card.question_id,
        ease_factor=card.ease_factor,
        interval_days=card.interval_days,
        repetitions=card.repetitions,
        lapses=card.lapses,
        due_at=card.due_at,
        last_reviewed_at=card.last_reviewed_at,
        state=card.state.value,
    )


def apply_card(card: ReviewCard, model: ReviewCardModel) -> None:
    """Copy a scheduled card onto its row.

    Only the SM-2 fields are written; ``user_id`` and ``question_id`` are the card's
    identity and must never move, which is what keeps the unique constraint meaningful.
    """
    model.ease_factor = card.ease_factor
    model.interval_days = card.interval_days
    model.repetitions = card.repetitions
    model.lapses = card.lapses
    model.due_at = card.due_at
    model.last_reviewed_at = card.last_reviewed_at
    model.state = card.state.value


def to_log_model(log: ReviewLog) -> ReviewLogModel:
    return ReviewLogModel(
        id=log.id,
        user_id=log.user_id,
        card_id=log.card_id,
        quality=log.quality,
        previous_interval_days=log.previous_interval_days,
        new_interval_days=log.new_interval_days,
        reviewed_at=log.reviewed_at,
    )


__all__ = [
    "apply_card",
    "to_card",
    "to_card_model",
    "to_log",
    "to_log_model",
]
