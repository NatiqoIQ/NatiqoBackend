"""SQLAlchemy implementation of :class:`ConversationRepositoryPort`.

``get_for_user`` carries its ownership predicate in the query, so a thread id belonging to
another account reads as absent — the same rule the review and quiz repositories keep.

Neither method commits. The unit of work owns the transaction, so a learner's turn and the
tutor's reply land together or not at all; a stored question with no answer would be a
transcript that lies about what was said.

Mappers are explicit functions rather than declarative configuration, mirroring the usage
repository: a conversation has no relationships and no lazy loads, so the round trip is a
row in and a frozen dataclass out.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.types import UserId
from app.modules.ai.domain.conversation import Conversation, Message
from app.modules.ai.infrastructure.models import AiConversationModel, AiMessageModel


def _to_conversation(model: AiConversationModel) -> Conversation:
    return Conversation(
        id=model.id,
        user_id=UserId(model.user_id),
        lesson_id=model.lesson_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _to_conversation_model(conversation: Conversation) -> AiConversationModel:
    return AiConversationModel(
        id=conversation.id,
        user_id=conversation.user_id,
        lesson_id=conversation.lesson_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _to_message(model: AiMessageModel) -> Message:
    return Message(
        id=model.id,
        conversation_id=model.conversation_id,
        role=model.role,
        content=model.content,
        created_at=model.created_at,
    )


def _to_message_model(message: Message) -> AiMessageModel:
    model = AiMessageModel(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=message.content,
    )
    # Assigned rather than passed to the constructor: the column is NOT NULL with a server
    # default, so an explicit ``None`` would insert NULL and fail instead of falling back
    # to the database clock.
    model.created_at = message.created_at
    return model


class SqlAlchemyConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, conversation: Conversation) -> None:
        self._session.add(_to_conversation_model(conversation))

    async def get_for_user(
        self, conversation_id: uuid.UUID, user_id: UserId
    ) -> Conversation | None:
        stmt = select(AiConversationModel).where(
            AiConversationModel.id == conversation_id,
            AiConversationModel.user_id == user_id,
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_conversation(model) if model is not None else None

    async def touch(self, conversation_id: uuid.UUID, *, at: datetime) -> None:
        # A targeted UPDATE rather than loading, mutating and flushing: the only column
        # that changes is ``updated_at``, and reading the row back to change one value
        # would be a round trip for nothing.
        stmt = (
            update(AiConversationModel)
            .where(AiConversationModel.id == conversation_id)
            .values(updated_at=at)
        )
        await self._session.execute(stmt)

    async def add_message(self, message: Message) -> None:
        self._session.add(_to_message_model(message))

    async def list_messages(
        self, conversation_id: uuid.UUID, *, limit: int | None = None
    ) -> list[Message]:
        stmt = select(AiMessageModel).where(AiMessageModel.conversation_id == conversation_id)
        if limit is None:
            stmt = stmt.order_by(AiMessageModel.created_at, AiMessageModel.id)
            models = (await self._session.execute(stmt)).scalars().all()
            return [_to_message(model) for model in models]

        # A window wants the *latest* turns, so the query orders newest-first and the
        # result is reversed to restore chronological order for the reader.
        stmt = stmt.order_by(AiMessageModel.created_at.desc(), AiMessageModel.id.desc()).limit(
            limit
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [_to_message(model) for model in reversed(models)]


__all__ = ["SqlAlchemyConversationRepository"]
