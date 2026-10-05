"""Declarative base, naming conventions and shared column mixins.

Every table in the system derives from :class:`Base`, which guarantees:
- deterministic constraint names (Alembic can autogenerate real migrations),
- UTC-aware ``created_at`` / ``updated_at`` on every row,
- UUID primary keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.time import utc_now

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
    """UUID v4 primary key. Non-enumerable by design: sequential IDs leak volume."""

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4, nullable=False
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


class SoftDeleteMixin:
    """Tombstone columns. Rows are never physically deleted from learner history."""

    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def soft_delete(self, at: datetime | None = None) -> None:
        self.deleted_at = at or utc_now()

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class VersionMixin:
    """Optimistic concurrency token for aggregates edited concurrently."""

    version: Mapped[int] = mapped_column(default=1, nullable=False)


def column_kwargs(**extra: Any) -> dict[str, Any]:
    """Small helper so modules stay declarative without repeating ``nullable``."""
    return {"nullable": False, **extra}
