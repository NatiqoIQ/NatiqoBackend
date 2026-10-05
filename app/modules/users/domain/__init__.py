"""Users and authentication — domain layer.

Framework-free by design: nothing in this package imports FastAPI or SQLAlchemy.
The aggregates here hold the rules that must hold no matter how the API, the database
or the token format changes.
"""

from app.modules.users.domain.entities import (
    NotificationPreferences,
    RefreshSession,
    User,
    UserDevice,
    UserProfile,
    UserSettings,
)
from app.modules.users.domain.policy import (
    DEFAULT_NATIVE_LANGUAGE,
    PasswordProblem,
    check_password,
    is_valid_email,
    is_valid_timezone_name,
    normalise_email,
)
from app.modules.users.domain.ports import (
    PasswordResetDeliveryPort,
    PasswordResetRecord,
    PasswordResetRepositoryPort,
    RefreshTokenRepositoryPort,
    UserDeviceRepositoryPort,
    UserRepository,
    UserRepositoryPort,
)

__all__ = [
    "DEFAULT_NATIVE_LANGUAGE",
    "NotificationPreferences",
    "PasswordProblem",
    "PasswordResetDeliveryPort",
    "PasswordResetRecord",
    "PasswordResetRepositoryPort",
    "RefreshSession",
    "RefreshTokenRepositoryPort",
    "User",
    "UserDevice",
    "UserDeviceRepositoryPort",
    "UserProfile",
    "UserRepository",
    "UserRepositoryPort",
    "UserSettings",
    "check_password",
    "is_valid_email",
    "is_valid_timezone_name",
    "normalise_email",
]
