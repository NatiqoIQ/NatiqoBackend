"""SQLAlchemy repositories for the users module.

Every mapper function is explicit. An ORM default or a ``session.add`` on a detached
object would make it possible to persist a half-built aggregate, so nothing is
immutable-by-accident here: each write constructs the row from the domain object.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import ensure_aware, utc_now
from app.core.types import UserId
from app.modules.users.domain.entities import (
    NotificationPreferences,
    RefreshSession,
    User,
    UserDevice,
    UserProfile,
    UserSettings,
)
from app.modules.users.domain.policy import normalise_email
from app.modules.users.domain.ports import PasswordResetRecord
from app.modules.users.infrastructure.models import (
    EmailVerificationTokenModel,
    PasswordResetTokenModel,
    RefreshTokenModel,
    UserDeviceModel,
    UserModel,
    UserProfileModel,
    UserSettingsModel,
)


def _rows_affected(result: Result[Any]) -> int:
    """Row count of a DML statement.

    ``AsyncSession.execute`` is typed as returning ``Result``, which has no
    ``rowcount``; only the ``CursorResult`` a DML statement actually produces does.
    The cast documents that expectation instead of hiding it behind ``getattr``.
    """
    cursor = cast("CursorResult[Any]", result)
    return int(cursor.rowcount or 0)


def _to_user(model: UserModel) -> User:
    from app.core.types import UserRole, UserStatus

    return User(
        id=UserId(model.id),
        email=model.email,
        password_hash=model.password_hash,
        status=UserStatus(model.status),
        roles=tuple(UserRole(role) for role in (model.roles or ["user"])),
        email_verified_at=ensure_aware(model.email_verified_at)
        if model.email_verified_at
        else None,
        password_changed_at=ensure_aware(model.password_changed_at)
        if model.password_changed_at
        else None,
        deleted_at=ensure_aware(model.deleted_at) if model.deleted_at else None,
        failed_login_attempts=model.failed_login_attempts,
        locked_until=ensure_aware(model.locked_until) if model.locked_until else None,
        created_at=ensure_aware(model.created_at),
        updated_at=ensure_aware(model.updated_at),
    )


def _to_profile(model: UserProfileModel) -> UserProfile:
    from app.core.types import CEFRLevel, ContentLanguage, EnglishVariant

    return UserProfile(
        user_id=UserId(model.user_id),
        display_name=model.display_name,
        avatar_key=model.avatar_key,
        native_language=ContentLanguage(model.native_language),
        english_variant=EnglishVariant(model.english_variant),
        timezone=model.timezone,
        current_level=CEFRLevel.parse(model.current_level),
        placement_test_completed_at=ensure_aware(model.placement_test_completed_at)
        if model.placement_test_completed_at
        else None,
    )


def _to_settings(model: UserSettingsModel) -> UserSettings:
    from app.core.types import ContentLanguage, Locale

    return UserSettings(
        user_id=UserId(model.user_id),
        locale=Locale(model.locale),
        explanation_language=ContentLanguage(model.explanation_language),
        notification_preferences=NotificationPreferences.from_dict(model.notification_preferences),
        daily_goal_minutes=model.daily_goal_minutes,
        daily_goal_new_words=model.daily_goal_new_words,
    )


def _to_session(model: RefreshTokenModel) -> RefreshSession:
    return RefreshSession(
        id=model.id,
        user_id=UserId(model.user_id),
        jti_hash=model.jti_hash,
        family_id=model.family_id,
        device_id=model.device_id,
        expires_at=ensure_aware(model.expires_at),
        created_at=ensure_aware(model.created_at),
        revoked_at=ensure_aware(model.revoked_at) if model.revoked_at else None,
        replaced_by_hash=model.replaced_by_hash,
        user_agent=model.user_agent,
        ip_address=model.ip_address,
    )


def _to_device(model: UserDeviceModel) -> UserDevice:
    return UserDevice(
        id=model.id,
        user_id=UserId(model.user_id),
        platform=model.platform,
        push_token=model.push_token,
        app_version=model.app_version,
        created_at=ensure_aware(model.created_at),
        last_seen_at=ensure_aware(model.last_seen_at) if model.last_seen_at else None,
        revoked_at=ensure_aware(model.revoked_at) if model.revoked_at else None,
    )


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ----------------------------------------------------------------- users
    async def get(self, user_id: UserId) -> User | None:
        model = await self._session.get(UserModel, user_id)
        return _to_user(model) if model else None

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(UserModel).where(UserModel.email == normalise_email(email)).limit(1)
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_user(model) if model else None

    async def email_exists(self, email: str) -> bool:
        stmt = (
            select(func.count())
            .select_from(UserModel)
            .where(UserModel.email == normalise_email(email))
        )
        return bool(await self._session.scalar(stmt))

    async def add(self, user: User) -> None:
        self._session.add(
            UserModel(
                id=user.id,
                email=user.email,
                password_hash=user.password_hash,
                status=user.status.value,
                roles=[role.value for role in user.roles],
                email_verified_at=user.email_verified_at,
                password_changed_at=user.password_changed_at,
                failed_login_attempts=user.failed_login_attempts,
                locked_until=user.locked_until,
                created_at=user.created_at,
                updated_at=user.updated_at,
            )
        )

    async def update(self, user: User) -> None:
        """Write the mutable columns of an existing account.

        The WHERE clause pins ``email`` to the value being replaced, so a concurrent
        email change cannot make this statement overwrite the newer row.
        """
        await self._session.execute(
            update(UserModel)
            .where(UserModel.id == user.id, UserModel.email == user.email)
            .values(
                password_hash=user.password_hash,
                status=user.status.value,
                roles=[role.value for role in user.roles],
                email_verified_at=user.email_verified_at,
                password_changed_at=user.password_changed_at,
                # Listed explicitly because this method enumerates every mutable column: a
                # new entity field that is not added here is silently dropped on write, and
                # the closure timestamp would never reach the row.
                deleted_at=user.deleted_at,
                failed_login_attempts=user.failed_login_attempts,
                locked_until=user.locked_until,
                updated_at=utc_now(),
            )
        )

    # -------------------------------------------------------------- profile
    async def get_profile(self, user_id: UserId) -> UserProfile | None:
        stmt = select(UserProfileModel).where(UserProfileModel.user_id == user_id)
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_profile(model) if model else None

    async def save_profile(self, profile: UserProfile) -> None:
        stmt = select(UserProfileModel).where(UserProfileModel.user_id == profile.user_id)
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        if model is None:
            self._session.add(
                UserProfileModel(
                    user_id=profile.user_id,
                    display_name=profile.display_name,
                    avatar_key=profile.avatar_key,
                    native_language=profile.native_language.value,
                    english_variant=profile.english_variant.value,
                    timezone=profile.timezone,
                    current_level=profile.current_level.value,
                    placement_test_completed_at=profile.placement_test_completed_at,
                )
            )
            return
        model.display_name = profile.display_name
        model.avatar_key = profile.avatar_key
        model.native_language = profile.native_language.value
        model.english_variant = profile.english_variant.value
        model.timezone = profile.timezone
        model.current_level = profile.current_level.value
        model.placement_test_completed_at = profile.placement_test_completed_at

    # ------------------------------------------------------------- settings
    async def get_settings(self, user_id: UserId) -> UserSettings | None:
        stmt = select(UserSettingsModel).where(UserSettingsModel.user_id == user_id)
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_settings(model) if model else None

    async def save_settings(self, settings: UserSettings) -> None:
        stmt = select(UserSettingsModel).where(UserSettingsModel.user_id == settings.user_id)
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        payload = settings.notification_preferences.to_dict()
        if model is None:
            self._session.add(
                UserSettingsModel(
                    user_id=settings.user_id,
                    locale=settings.locale.value,
                    explanation_language=settings.explanation_language.value,
                    notification_preferences=payload,
                    daily_goal_minutes=settings.daily_goal_minutes,
                    daily_goal_new_words=settings.daily_goal_new_words,
                )
            )
            return
        model.locale = settings.locale.value
        model.explanation_language = settings.explanation_language.value
        model.notification_preferences = payload
        model.daily_goal_minutes = settings.daily_goal_minutes
        model.daily_goal_new_words = settings.daily_goal_new_words


class SqlAlchemyRefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, refresh_session: RefreshSession) -> None:
        self._session.add(
            RefreshTokenModel(
                id=refresh_session.id,
                user_id=refresh_session.user_id,
                jti_hash=refresh_session.jti_hash,
                family_id=refresh_session.family_id,
                device_id=refresh_session.device_id,
                expires_at=refresh_session.expires_at,
                revoked_at=refresh_session.revoked_at,
                replaced_by_hash=refresh_session.replaced_by_hash,
                user_agent=refresh_session.user_agent,
                ip_address=refresh_session.ip_address,
                created_at=refresh_session.created_at,
            )
        )

    async def get_by_jti_hash(self, jti_hash: str) -> RefreshSession | None:
        stmt = select(RefreshTokenModel).where(RefreshTokenModel.jti_hash == jti_hash)
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_session(model) if model else None

    async def revoke(self, refresh_session: RefreshSession, *, at: datetime | None = None) -> None:
        moment = at or utc_now()
        await self._session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.id == refresh_session.id)
            .values(revoked_at=moment)
        )

    async def mark_replaced(
        self, refresh_session: RefreshSession, *, replaced_by_hash: str, at: datetime | None = None
    ) -> None:
        await self._session.execute(
            update(RefreshTokenModel)
            .where(RefreshTokenModel.id == refresh_session.id)
            .values(revoked_at=at or utc_now(), replaced_by_hash=replaced_by_hash)
        )

    async def revoke_family(self, family_id: str, *, at: datetime | None = None) -> int:
        result = await self._session.execute(
            update(RefreshTokenModel)
            .where(
                RefreshTokenModel.family_id == family_id,
                RefreshTokenModel.revoked_at.is_(None),
            )
            .values(revoked_at=at or utc_now())
        )
        return _rows_affected(result)

    async def revoke_all_for_user(self, user_id: UserId, *, at: datetime | None = None) -> int:
        result = await self._session.execute(
            update(RefreshTokenModel)
            .where(
                RefreshTokenModel.user_id == user_id,
                RefreshTokenModel.revoked_at.is_(None),
            )
            .values(revoked_at=at or utc_now())
        )
        return _rows_affected(result)

    async def revoke_for_device(
        self, user_id: UserId, device_id: uuid.UUID, *, at: datetime | None = None
    ) -> int:
        """End every session opened from one device.

        Scoped by ``user_id`` as well as ``device_id`` so a malformed link between a
        device and another account's sessions cannot be used to revoke them.
        """
        result = await self._session.execute(
            update(RefreshTokenModel)
            .where(
                RefreshTokenModel.user_id == user_id,
                RefreshTokenModel.device_id == device_id,
                RefreshTokenModel.revoked_at.is_(None),
            )
            .values(revoked_at=at or utc_now())
        )
        return _rows_affected(result)


class SqlAlchemyUserDeviceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, device_id: uuid.UUID) -> UserDevice | None:
        model = await self._session.get(UserDeviceModel, device_id)
        return _to_device(model) if model else None

    async def upsert_for_token(self, device: UserDevice) -> UserDevice:
        """Re-attach an existing install to its owner, or create it.

        Matching on ``push_token`` rather than a client-supplied device id is what
        stops a reinstall from accumulating rows that all receive the same push.

        The match is scoped to the same ``user_id``. A push token is a bearer
        credential for a device, so matching on it alone would let anyone who learned
        one — by reinstalling onto a shared handset, or by reading it off a backup —
        claim that device and start receiving another learner's push notifications.
        A token belonging to someone else is left alone and a fresh row is created.
        """
        if device.push_token:
            stmt = select(UserDeviceModel).where(
                UserDeviceModel.push_token == device.push_token,
                UserDeviceModel.user_id == device.user_id,
            )
            model = (await self._session.execute(stmt)).scalar_one_or_none()
            if model is not None:
                model.platform = device.platform
                model.app_version = device.app_version
                model.last_seen_at = utc_now()
                # A re-authenticated device is live again; revocation is a user
                # action, not a permanent property of the install.
                model.revoked_at = None
                return _to_device(model)
        self._session.add(
            UserDeviceModel(
                id=device.id,
                user_id=device.user_id,
                platform=device.platform,
                push_token=device.push_token,
                app_version=device.app_version,
                last_seen_at=device.last_seen_at or utc_now(),
            )
        )
        return device

    async def list_active(self, user_id: UserId) -> list[UserDevice]:
        stmt = select(UserDeviceModel).where(
            UserDeviceModel.user_id == user_id,
            UserDeviceModel.revoked_at.is_(None),
        )
        models = (await self._session.execute(stmt)).scalars().all()
        return [_to_device(model) for model in models]

    async def revoke(self, device_id: uuid.UUID, *, at: datetime | None = None) -> None:
        await self._session.execute(
            update(UserDeviceModel)
            .where(UserDeviceModel.id == device_id)
            .values(revoked_at=at or utc_now())
        )


class SqlAlchemyPasswordResetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        user_id: UserId,
        token_hash: str,
        expires_at: datetime,
        requested_ip: str | None = None,
    ) -> None:
        self._session.add(
            PasswordResetTokenModel(
                user_id=user_id,
                token_hash=token_hash,
                expires_at=expires_at,
                requested_ip=requested_ip,
            )
        )

    async def get(self, token_hash: str) -> PasswordResetRecord | None:
        stmt = select(PasswordResetTokenModel).where(
            PasswordResetTokenModel.token_hash == token_hash
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        if model is None:
            return None
        return PasswordResetRecord(
            user_id=UserId(model.user_id),
            token_hash=model.token_hash,
            expires_at=ensure_aware(model.expires_at),
            created_at=ensure_aware(model.created_at),
            used_at=ensure_aware(model.used_at) if model.used_at else None,
            requested_ip=model.requested_ip,
        )

    async def consume(self, token_hash: str, *, at: datetime | None = None) -> None:
        await self._session.execute(
            update(PasswordResetTokenModel)
            .where(PasswordResetTokenModel.token_hash == token_hash)
            .values(used_at=at or utc_now())
        )

    async def revoke_outstanding(self, user_id: UserId, *, at: datetime | None = None) -> int:
        """Invalidate every unused token for the account.

        A new request supersedes earlier ones: two live reset links means two parties
        can change the credential, and the older holder has no reason to still be
        trying.
        """
        result = await self._session.execute(
            update(PasswordResetTokenModel)
            .where(
                PasswordResetTokenModel.user_id == user_id,
                PasswordResetTokenModel.used_at.is_(None),
            )
            .values(used_at=at or utc_now())
        )
        return _rows_affected(result)


class SqlAlchemyEmailVerificationRepository:
    """Email-verification token storage.

    A near-copy of :class:`SqlAlchemyPasswordResetRepository` rather than a subclass or a
    shared generic. The duplication is the cheaper mistake: a shared abstraction would
    have to be parameterised by table, and the one thing that must never be shared here
    is the ability of one flow to touch the other's rows.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        *,
        user_id: UserId,
        token_hash: str,
        expires_at: datetime,
        requested_ip: str | None = None,
    ) -> None:
        self._session.add(
            EmailVerificationTokenModel(
                user_id=user_id,
                token_hash=token_hash,
                expires_at=expires_at,
                requested_ip=requested_ip,
            )
        )

    async def get(self, token_hash: str) -> PasswordResetRecord | None:
        stmt = select(EmailVerificationTokenModel).where(
            EmailVerificationTokenModel.token_hash == token_hash
        )
        model = (await self._session.execute(stmt)).scalar_one_or_none()
        if model is None:
            return None
        return PasswordResetRecord(
            user_id=UserId(model.user_id),
            token_hash=model.token_hash,
            expires_at=ensure_aware(model.expires_at),
            created_at=ensure_aware(model.created_at),
            used_at=ensure_aware(model.used_at) if model.used_at else None,
            requested_ip=model.requested_ip,
        )

    async def consume(self, token_hash: str, *, at: datetime | None = None) -> None:
        await self._session.execute(
            update(EmailVerificationTokenModel)
            .where(EmailVerificationTokenModel.token_hash == token_hash)
            .values(used_at=at or utc_now())
        )

    async def revoke_outstanding(self, user_id: UserId, *, at: datetime | None = None) -> int:
        """Invalidate every unused verification token for the account.

        Same rule as the reset flow: only the newest link can confirm, so the older
        ones are marked used rather than left live.
        """
        result = await self._session.execute(
            update(EmailVerificationTokenModel)
            .where(
                EmailVerificationTokenModel.user_id == user_id,
                EmailVerificationTokenModel.used_at.is_(None),
            )
            .values(used_at=at or utc_now())
        )
        return _rows_affected(result)

    async def has_usable(self, user_id: UserId, *, at: datetime | None = None) -> bool:
        """Whether a live, unexpired, unused verification link already exists."""
        moment = at or utc_now()
        stmt = (
            select(func.count())
            .select_from(EmailVerificationTokenModel)
            .where(
                EmailVerificationTokenModel.user_id == user_id,
                EmailVerificationTokenModel.used_at.is_(None),
                EmailVerificationTokenModel.expires_at > moment,
            )
        )
        return bool((await self._session.execute(stmt)).scalar_one())


__all__ = [
    "SqlAlchemyEmailVerificationRepository",
    "SqlAlchemyPasswordResetRepository",
    "SqlAlchemyRefreshTokenRepository",
    "SqlAlchemyUserDeviceRepository",
    "SqlAlchemyUserRepository",
]
