"""Media module — SQLAlchemy model.

One table, ``media_assets``. It is metadata only: the bytes live in the object store and
are addressed by ``storage_key``. Three constraints carry real weight rather than
decoration:

* ``storage_key`` is ``UNIQUE``, which is what makes confirmation idempotent *and* makes
  it impossible for two rows to describe the same object.
* ``kind`` is guarded by a ``CHECK`` against the code-defined :class:`MediaKind` values, so
  the column cannot sprout an informal value no query filters on.
* ``size_bytes`` is constrained non-negative, matching the checksum's implicit claim that
  this row describes bytes that actually exist.

``owner_id`` is nullable and ``ON DELETE SET NULL``: an upload outlives its uploader, so
deleting an account must not cascade a shared asset out from under the lessons using it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.modules.media.domain.policy import MEDIA_KINDS
from app.modules.users.infrastructure.models import UserModel

#: Written into the ``CHECK`` on ``kind``. A module constant so the model and the
#: migration cannot drift by being edited separately.
MEDIA_KINDS_SQL: str = ", ".join(f"'{kind}'" for kind in MEDIA_KINDS)


class MediaAssetModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "media_assets"

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    #: The *sniffed* type, not the declared one. See ``domain/policy.py``.
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Ordering metadata only — never navigated. Present so the unit of work orders the
    #: insert after ``users`` when a test seeds an owner and an asset in one flush.
    user: Mapped[UserModel | None] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        CheckConstraint(f"kind IN ({MEDIA_KINDS_SQL})", name="valid_kind"),
        CheckConstraint("size_bytes >= 0", name="non_negative_size"),
        # "What has this account uploaded?" — the lookup a quota or an account purge runs.
        Index("ix_media_assets_owner", "owner_id"),
        # The library screen lists newest first across all owners.
        Index("ix_media_assets_created_at", "created_at"),
    )
