"""Declarative base, naming conventions and shared column mixins.

Every table in the system derives from :class:`Base`, which guarantees:
- deterministic constraint names (Alembic can autogenerate real migrations),
- UTC-aware ``created_at`` / ``updated_at`` on every row,
- UUID primary keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import uuid7

# Explicit names keep Alembic autogenerate output stable and make it possible to
# reference a constraint by name in tests and in `DROP CONSTRAINT` migrations.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Common declarative base for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012
        # `Uuid` renders as PostgreSQL's native UUID and as CHAR(32) elsewhere, so the
        # same models run on Postgres in production and SQLite in the test suite.
        uuid.UUID: Uuid(as_uuid=True),
        datetime: DateTime(timezone=True),
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


class UUIDPrimaryKeyMixin:
    """UUIDv7 primary key: time-ordered, non-enumerable, no sequence to coordinate.

    v7 keeps writes at the right edge of the primary-key index, which is what makes
    append-heavy tables (answers, logs, usage records) cheap to insert at scale.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid7, nullable=False
    )


class TimestampMixin:
    """UTC ``created_at`` / ``updated_at``.

    ``server_default``/``server_onupdate`` keep the database authoritative even if a
    write bypasses the ORM.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
