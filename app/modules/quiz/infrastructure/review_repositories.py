"""SQLAlchemy implementations of the spaced-repetition ports.

``get_for_user`` and ``get_for_question`` carry their ownership predicate in the query,
so a card id belonging to another account reads as absent. The unique constraint on
``(user_id, question_id)`` is the write half of the "one card per question" rule; the
repository read is the other, so a race between two correct answers hits the database
rather than double-scheduling.

None of these methods commit. The unit of work owns the transaction, so scheduling a card
and appending its log either land whole or not at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import UserId
from app.modules.quiz.domain.review import (
    PASSING_QUALITY,
    ReviewCard,
    ReviewLog,
    ReviewLogStats,
)
from app.modules.quiz.infrastructure.review_mappers import (
    apply_card,
    to_card,
    to_card_model,
    to_log_model,
)
from app.modules.quiz.infrastructure.review_models import (
    ReviewCardModel,
    ReviewLogModel,
)


class SqlAlchemyReviewCardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, card: ReviewCard) -> None:
        self._session.add(to_card_model(card))

    async def get_for_user(self, card_id: uuid.UUID, user_id: UserId) -> ReviewCard | None:
        stmt = select(ReviewCardModel).where(
            ReviewCardModel.id == card_id,
            ReviewCardModel.user_id == user_id,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_card(model) if model is not None else None

    async def get_for_question(self, user_id: UserId, question_id: uuid.UUID) -> ReviewCard | None:
        stmt = select(ReviewCardModel).where(
            ReviewCardModel.user_id == user_id,
            ReviewCardModel.question_id == question_id,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_card(model) if model is not None else None

    async def save(self, card: ReviewCard) -> None:
        model = await self._session.get(ReviewCardModel, card.id)
        if model is None:
            self._session.add(to_card_model(card))
            return
        apply_card(card, model)

    async def list_due(self, user_id: UserId, *, now: datetime, limit: int) -> list[ReviewCard]:
        stmt = (
            select(ReviewCardModel)
            .where(
                ReviewCardModel.user_id == user_id,
                ReviewCardModel.due_at <= now,
            )
            .order_by(ReviewCardModel.due_at, ReviewCardModel.id)
            .limit(limit)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_card(model) for model in models]

    async def count_due(self, user_id: UserId, *, now: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(ReviewCardModel)
            .where(
                ReviewCardModel.user_id == user_id,
                ReviewCardModel.due_at <= now,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_for_user(self, user_id: UserId) -> list[ReviewCard]:
        stmt = (
            select(ReviewCardModel)
            .where(ReviewCardModel.user_id == user_id)
            .order_by(ReviewCardModel.id)
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [to_card(model) for model in models]


class SqlAlchemyReviewLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, log: ReviewLog) -> None:
        self._session.add(to_log_model(log))

    async def stats_for_user(self, user_id: UserId) -> ReviewLogStats:
        stmt = (
            select(
                func.count(),
                func.coalesce(
                    func.sum(case((ReviewLogModel.quality >= PASSING_QUALITY, 1), else_=0)),
                    0,
                ),
            )
            .select_from(ReviewLogModel)
            .where(ReviewLogModel.user_id == user_id)
        )
        total, correct = (await self._session.execute(stmt)).one()
        return ReviewLogStats(total=int(total), correct=int(correct))


__all__ = [
    "SqlAlchemyReviewCardRepository",
    "SqlAlchemyReviewLogRepository",
]
