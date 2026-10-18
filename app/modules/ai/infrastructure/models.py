"""AI module infrastructure models."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.modules.ai.domain.conversation import MessageRole

#: Written into the ``CHECK`` on ``role``. A module constant so the model, the mapper and
#: the migration cannot drift by being edited separately.
MESSAGE_ROLES: tuple[str, ...] = tuple(role.value for role in MessageRole)
MESSAGE_ROLES_SQL: str = ", ".join(f"'{role}'" for role in MESSAGE_ROLES)


class AiUsageRecord(Base):
    """One AI call. Append-only; never updated.

    Kept as its own table (rather than a column on ``users``) so cost can be
    aggregated per day, per feature and per provider without a schema change each
    time a new AI feature ships.
    """

    __tablename__ = "ai_usage_records"

    # ``Uuid`` maps to PostgreSQL's native UUID and to CHAR(32) elsewhere, so the same
    # model runs on Postgres in production and SQLite in the test suite.
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    feature: Mapped[str] = mapped_column(String(40), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)

    input_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ok")
    error: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Budget checks hit (user_id, created_at); cost reports hit (created_at, feature).
        Index("ix_ai_usage_user_created", "user_id", "created_at"),
        Index("ix_ai_usage_feature_created", "feature", "created_at"),
        # A deployment-wide daily cost series is not filtered by user or feature, so it
        # cannot use either composite above — they both lead with a filter column.
        Index("ix_ai_usage_created_at", "created_at"),
    )


class AiConversationModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One learner's tutor thread.

    ``lesson_id`` is a bare id with **no** foreign key. A conversation is the learner's own
    record of what they asked and when; it must survive an editor retiring or revising the
    lesson it was anchored to, so a cascade from ``content`` is exactly the behaviour we do
    not want. Only ``user_id`` cascades: deleting an account takes the learner's threads.
    """

    __tablename__ = "ai_conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Optional anchor to the lesson that was open. No FK — see the class docstring.
    lesson_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    __table_args__ = (
        # The conversation list reads (user_id, updated_at) descending, so the index is
        # ordered the way the query is.
        Index("ix_ai_conversations_user_updated", "user_id", "updated_at"),
    )


class AiMessageModel(Base, UUIDPrimaryKeyMixin):
    """One turn in a conversation. Append-only; never updated.

    ``role`` is constrained to the two values :class:`MessageRole` defines, so the
    transcript cannot sprout an informal speaker that no client knows how to render. Only
    ``created_at`` is stored — a turn is written once, so an ``updated_at`` would only ever
    be a lie.
    """

    __tablename__ = "ai_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(f"role IN ({MESSAGE_ROLES_SQL})", name="valid_role"),
        # Turns are read in order for one conversation, so the index leads with the
        # conversation and then the ordering column.
        Index("ix_ai_messages_conversation", "conversation_id", "created_at"),
    )
