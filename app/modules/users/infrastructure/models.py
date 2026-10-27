"""Users module — SQLAlchemy models.

Eight tables, split along the axes that change for different reasons:

- ``users`` — account lifecycle. Changes rarely; the most sensitive.
- ``user_identities`` — the Google/Apple accounts linked to a learner. Append-mostly.
- ``user_profiles`` — learning identity and calendar preferences. Changes often.
- ``user_settings`` — behaviour switches. Changes often, and is per-device-ish later.
- ``refresh_tokens`` — appended on every rotation. Never updated except to revoke.
- ``user_devices`` — one row per install, used for push and per-device revocation.
- ``user_daily_challenges`` — one row per learner per day, holding only the claim.
- ``user_daily_challenge_contributions`` — append-only ledger of finished sittings.

Splitting them means a profile edit never contends with a sign-in, and a token table
that grows without bound never bloats a row the app reads on every request.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
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
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=UserStatus.ACTIVE.value)
    #: The single privilege switch. There is no role vocabulary: an account either runs the
    #: admin surface or it does not, and every gated route reads this column.
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: When a soft delete happened. Null means the account was never closed, which is the
    #: only distinction that matters -- ``status`` already says whether it is closed *now*.
    #: Kept as a separate column because "when did this account end" is a question the
    #: export and retention jobs need answered, and overwriting ``updated_at`` on every
    #: subsequent write would destroy the answer.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    profile: Mapped[UserProfileModel | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False, lazy="joined"
    )
    settings: Mapped[UserSettingsModel | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False, lazy="joined"
    )

    __table_args__ = (
        # Sign-up growth is charted by day, and the overview counts new accounts in a
        # window; both are a range scan on this column. ``deleted_at`` is indexed for the
        # same reason and for the retention job, but it cannot answer "how many signed up
        # last week".
        Index("ix_users_created_at", "created_at"),
        CheckConstraint(
            "status IN ('active', 'pending_verification', 'suspended', 'deleted')",
            name="valid_status",
        ),
    )


class UserIdentityModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One sign-in account at a provider, linked to a learner.

    A learner may hold more than one — a Google identity and an Apple identity for the
    same person — so the ``(provider, subject)`` pair is the unique key rather than a
    single row per account. ``subject`` is the provider's stable, opaque user id
    (``sub``); an email is recorded for support but is deliberately not a key, because
    it can change and it is not unique across providers.
    """

    __tablename__ = "user_identities"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254))
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    picture_url: Mapped[str | None] = mapped_column(String(512))
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Ordering metadata only — see the note on :attr:`RefreshTokenModel.user`.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_identity_provider_subject"),
        Index("ix_identity_user", "user_id"),
        # "Active in the last N days" is counted from the newest sign-in per identity, so
        # the window predicate reads this column on every overview request.
        Index("ix_identity_last_login", "last_login_at"),
        CheckConstraint("provider IN ('google', 'apple')", name="valid_provider"),
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


class UserDailyChallengeModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One learner's daily challenge state for one day.

    The row keeps only the fact a completion cannot imply: whether the day's reward was
    taken. Progress is derived from :class:`UserDailyChallengeContributionModel`, so
    there is no counter here to fall out of step with the sittings it is supposed to
    summarise — and a retried write cannot inflate a total that is never stored.
    """

    __tablename__ = "user_daily_challenges"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Date of the challenge in the learner's local timezone (YYYY-MM-DD).
    challenge_date: Mapped[str] = mapped_column(String(10), nullable=False)
    #: Whether the daily challenge has been claimed today.
    claimed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: Ordering metadata only — see the module docstring. Never navigated.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        # A learner can have at most one row per day.
        UniqueConstraint("user_id", "challenge_date", name="uq_user_daily_challenge_day"),
        # Enable efficient "who claimed today" queries.
        Index("ix_user_daily_challenge_date", "challenge_date"),
    )


class UserDailyChallengeContributionModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One finished sitting's contribution to the day it finished on.

    The row *is* the idempotency record: ``(user_id, session_id)`` is unique, so a
    completion that is retried, delivered twice from two devices or replayed from an
    offline queue cannot be counted twice. ``challenge_date`` is decided once, from the
    sitting's own ``completed_at`` in the learner's timezone, and never recomputed from
    the clock at write time — a retried completion keeps the day it happened on, even
    when the retry itself arrives after midnight.

    ``session_id`` is deliberately not a foreign key to ``quiz_sessions``: the users
    module does not depend on the quiz module's schema, and a day's totals are an account
    fact that should outlive the sitting row it was earned in.
    """

    __tablename__ = "user_daily_challenge_contributions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: The completed sitting this contribution came from — the idempotency key.
    session_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    #: The day the sitting finished in the learner's local timezone (YYYY-MM-DD).
    challenge_date: Mapped[str] = mapped_column(String(10), nullable=False)
    lessons_completed: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )
    words_learned: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default=text("0")
    )

    #: Ordering metadata only — see the module docstring. Never navigated.
    user: Mapped[UserModel] = relationship(lazy="raise_on_sql")

    __table_args__ = (
        UniqueConstraint("user_id", "session_id", name="uq_daily_challenge_contribution_session"),
        # Every read is "sum this learner's contributions for this day".
        Index("ix_daily_challenge_contribution_day", "user_id", "challenge_date"),
    )
