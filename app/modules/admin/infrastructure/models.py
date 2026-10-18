"""Admin module — SQLAlchemy models.

One table today: ``audit_logs``. It is the durable half of the admin surface's double
protection (the role check is the other), so it carries no ``updated_at`` and no soft
delete — an audit row is written once and never edited, and a model without the columns
to change it is a model nothing can accidentally rewrite.

``actor_id`` is nullable and its foreign key is ``ON DELETE SET NULL``: an audit row must
outlive the account it names, because "this admin changed that lesson" is exactly the fact
worth keeping after the admin themselves is gone.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base, UUIDPrimaryKeyMixin


class AuditLogModel(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "audit_logs"

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    #: Namespaced action, e.g. ``content.lesson.publish``. See ``domain/audit.py``.
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    #: Small JSON snapshot of what the write carried. Never a full diff.
    changes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # "What happened to this lesson?" — the entity lookup an incident review runs.
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        # "What did this admin do, most recently?" — the actor lookup.
        Index("ix_audit_logs_actor", "actor_id", "created_at"),
        # The audit screen's default view is "newest first, unfiltered", which neither
        # composite index leads with: both start with a filter column.
        Index("ix_audit_logs_created_at", "created_at"),
    )
