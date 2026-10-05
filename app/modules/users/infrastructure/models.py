"""Users module — SQLAlchemy models.

Five tables, split along the axes that change for different reasons:

- ``users`` — credentials and account lifecycle. Changes rarely; the most sensitive.
- ``user_profiles`` — learning identity and calendar preferences. Changes often.
- ``user_settings`` — behaviour switches. Changes often, and is per-device-ish later.
- ``refresh_tokens`` — appended on every rotation. Never updated except to revoke.
- ``user_devices`` — one row per install, used for push and per-device revocation.

Splitting them means a profile edit never contends with a login, and a token table
that grows without bound never bloats a row the app reads on every request.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.core.types import CEFRLevel, ContentLanguage, EnglishVariant, Locale, UserStatus


class UserModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"

    #: Stored already lowercased by ``normalise_email``; the unique index on the
    #: normalised value is what makes "duplicate email" impossible rather than
    #: merely unlikely.
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=UserStatus.ACTIVE.value)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=lambda: ["user"])

    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: When a soft delete happened. Null means the account was never closed, which is the
    #: only distinction that matters -- ``status`` already says whether it is closed *now*.
    #: Kept as a separate column because "when did this account end" is a question the
    #: export and retention jobs need answered, and overwriting ``updated_at`` on every
    #: subsequent write would destroy the answer.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    failed_login_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    profile: Mapped[UserProfileModel | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False, lazy="joined"
    )
    settings: Mapped[UserSettingsModel | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False, lazy="joined"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'pending_verification', 'suspended', 'deleted')",
            name="valid_status",
        ),
        CheckConstraint("failed_login_attempts >= 0", name="non_negative_attempts"),
    )


class UserProfileModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    avatar_key: Mapped[str | None] = mapped_column(String(255))

    native_language: Mapped[str] = mapped_column(
        String(5), nullable=False, default=ContentLanguage.AR.value
    )
    english_variant: Mapped[str] = mapped_column(
        String(10), nullable=False, default=EnglishVariant.BRITISH.value
    )
    #: IANA name. Validated in the domain, not by a database CHECK, because the set of
    #: zones changes with every tzdata release.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Baghdad")
    current_level: Mapped[str] = mapped_column(
        String(2), nullable=False, default=CEFRLevel.A1.value
    )
    placement_test_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[UserModel] = relationship(back_populates="profile")

    __table_args__ = (
        CheckConstraint("english_variant IN ('british', 'american', 'both')", name="valid_variant"),
        CheckConstraint(
            "current_level IN ('A1', 'A2', 'B1', 'B2', 'C1', 'C2')", name="valid_level"
        ),
    )


class UserSettingsModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    locale: Mapped[str] = mapped_column(String(10), nullable=False, default=Locale.EN_GB.value)
    explanation_language: Mapped[str] = mapped_column(
        String(5), nullable=False, default=ContentLanguage.EN.value
    )
    notification_preferences: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    daily_goal_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=30)
    daily_goal_new_words: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=10)

    user: Mapped[UserModel] = relationship(back_populates="settings")

    __table_args__ = (
        CheckConstraint("daily_goal_minutes > 0", name="positive_minutes"),
        CheckConstraint("daily_goal_new_words > 0", name="positive_words"),
    )


class RefreshTokenModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One row per issued refresh token. Append-heavy, read by ``jti_hash`` only."""

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: SHA-256 of the token's ``jti``. Never the token itself.
    jti_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    family_id: Mapped[str] = mapped_column(String(32), nullable=False)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_devices.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_hash: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    #: Declared only so the unit of work knows a user row must be inserted first. A
    #: bare ``ForeignKey`` is enforced by the *database*, and the UoW derives its insert
    #: ordering from relationship metadata alone — without this, a token row can be
    #: flushed ahead of its user and fail the foreign key.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")
    #: A token issued for a recorded install points at that install's row, so the
    #: device has to be ordered ahead of the token too. A string target is required
    #: because ``UserDeviceModel`` is defined further down this module.
    device: Mapped[UserDeviceModel | None] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        # Rotation walks a family forward, and reuse detection revokes a whole family.
        Index("ix_refresh_family", "family_id"),
        Index("ix_refresh_user_active", "user_id", "revoked_at"),
        # Periodic cleanup of expired rows reads this column and nothing else.
        Index("ix_refresh_expires", "expires_at"),
    )


class UserDeviceModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "user_devices"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    #: FCM registration token. Unique *per account* rather than globally: on a shared
    #: handset the install legitimately belongs to whichever learner signed in last,
    #: and a global unique would either block them or force a device to be stolen
    #: between accounts. NULL stays unique-per-row, so a device with no push
    #: registration needs no special casing.
    push_token: Mapped[str | None] = mapped_column(String(512))
    app_version: Mapped[str | None] = mapped_column(String(32))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: See the note on :attr:`RefreshTokenModel.user` — ordering metadata only.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        Index("ix_device_user_active", "user_id", "revoked_at"),
        UniqueConstraint("user_id", "push_token", name="uq_device_user_push_token"),
    )


class PasswordResetTokenModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Single-use password-reset tokens.

    A sixth table beyond the plan's five. Reset tokens deliberately do **not** share
    the ``refresh_tokens`` table: the two have opposite lifecycles (reset is
    single-use and short-lived, refresh is rotated and long-lived), and mixing them
    makes "revoke all sessions on password change" ambiguous at exactly the moment it
    matters most.

    Only the SHA-256 of the opaque token is stored, so a database leak yields no
    usable reset links.
    """

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Recorded so a support agent can tell whether a learner reported "the link did
    #: nothing" because it was already used or because it expired.
    requested_ip: Mapped[str | None] = mapped_column(String(64))

    #: See the note on :attr:`RefreshTokenModel.user` — ordering metadata only.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        Index("ix_password_reset_user", "user_id", "used_at"),
        Index("ix_password_reset_expires", "expires_at"),
    )


class EmailVerificationTokenModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Single-use email-verification tokens.

    A separate table from :class:`PasswordResetTokenModel` even though the two rows look
    alike, because they answer different questions and are consumed by different code
    paths. Sharing one table would mean a "revoke outstanding resets" call could silently
    invalidate a verification link (or the reverse), and the two have genuinely different
    lifetimes: a verification link is worth days because the learner may not open their
    email for a while, a reset link is worth an hour because the alternative is being
    locked out.

    Only the SHA-256 digest is stored, so a database leak yields no usable links.
    """

    __tablename__ = "email_verification_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(64))

    #: Ordering metadata only — see the note on :attr:`RefreshTokenModel.user`.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        Index("ix_email_verification_user", "user_id", "used_at"),
        Index("ix_email_verification_expires", "expires_at"),
    )
