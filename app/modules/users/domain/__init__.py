"""Users and authentication — domain layer.

Framework-free by design: nothing in this package imports FastAPI or SQLAlchemy.
The aggregates here hold the rules that must hold no matter how the API, the database
or the token format changes.
"""

from app.modules.users.domain.entities import (
    ExternalIdentity,
    NotificationPreferences,
    RefreshSession,
    User,
    UserDevice,
    UserProfile,
    UserSettings,
)
from app.modules.users.domain.identity import (
    IdentityProvider,
    IdentityVerificationError,
    IdentityVerifierPort,
    VerifiedIdentity,
)
from app.modules.users.domain.policy import (
    DEFAULT_NATIVE_LANGUAGE,
    is_valid_email,
    is_valid_timezone_name,
    normalise_email,
)
from app.modules.users.domain.ports import (
    ExternalIdentityRepositoryPort,
    RefreshTokenRepositoryPort,
    UserDeviceRepositoryPort,
    UserRepositoryPort,
)

__all__ = [
    "DEFAULT_NATIVE_LANGUAGE",
    "ExternalIdentity",
    "ExternalIdentityRepositoryPort",
    "IdentityProvider",
    "IdentityVerificationError",
    "IdentityVerifierPort",
    "NotificationPreferences",
    "RefreshSession",
    "RefreshTokenRepositoryPort",
    "User",
    "UserDevice",
    "UserDeviceRepositoryPort",
    "UserProfile",
    "UserRepositoryPort",
    "UserSettings",
    "VerifiedIdentity",
    "is_valid_email",
    "is_valid_timezone_name",
    "normalise_email",
]
