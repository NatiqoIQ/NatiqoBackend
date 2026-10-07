"""Demo personas for :mod:`scripts.seed`.

These are *data*, kept apart from the seeder's logic so the two can change
independently: a reviewer reads this file to answer "what will a fresh environment
contain", and reads the seeder to answer "how is it written".

Every persona exists to make one state visible that would otherwise be untestable by
hand: a suspended account, an unverified account, an account with a failed AI call, a
learner with two devices and therefore two refresh chains. A demo set where every
account is a healthy A1 learner hides every interesting branch in the app.

Two rules keep reruns safe:

* **A deterministic provider identity is the ownership marker.** The seeder recognises
  its own accounts by the identity it minted for them (``seed:<persona>``), so an
  account that happens to share a demo email but was created some other way is left
  completely alone, and a demo environment that has drifted into real usage cannot be
  overwritten by a rerun.
* **Nothing here is a secret.** These addresses are published in the README on purpose.
  They are demo data for a seeded local environment, and :mod:`scripts.seed` refuses to
  write them to a production database without an explicit override.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.core.types import (
    CEFRLevel,
    ContentLanguage,
    EnglishVariant,
    Locale,
    UserRole,
    UserStatus,
)
from app.modules.users.domain.entities import NotificationPreferences

#: Fixed namespace for every deterministic id the seeder mints. Chosen once and frozen
#: rather than generated at import: a UUID5 namespace that changed between runs would
#: make the seeder insert duplicate rows on every invocation, which is the exact
#: failure the deterministic ids exist to prevent.
SEED_NAMESPACE = uuid.UUID("6f0c1d2a-7b3e-4c58-9a41-0d5e8f2b6c39")


@dataclass(frozen=True, slots=True)
class SeedDevice:
    """One install, and optionally one refresh chain.

    A device with a ``push_token`` produces a row in ``user_devices``; a device
    without one produces only a refresh token. That mirrors the app: a web session has
    nothing to push to, so ``AuthService._issue_session`` records no device for it.

    ``devices[0]`` is the context handed to registration, which mints the first refresh
    chain. Any later device mints an additional one, but only on the run that creates
    the account — see :func:`scripts.seed.run_seed`.
    """

    platform: str = "web"
    push_token: str | None = None
    app_version: str | None = None
    user_agent: str | None = None
    ip_address: str | None = None

    @property
    def creates_device_row(self) -> bool:
        return bool(self.push_token or self.app_version)


@dataclass(frozen=True, slots=True)
class SeedAiCall:
    """One historical AI call.

    ``cost_usd`` is deliberately absent: it is derived from tokens at write time by
    :func:`app.platform.ai.providers.estimate_cost`, so demo spend stays consistent
    with the same pricing table production bills against. A hard-coded dollar figure
    here would drift the moment a price changed.
    """

    feature: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    status: str = "ok"
    error: str | None = None
    days_ago: int = 0
    hours_ago: int = 0


@dataclass(frozen=True, slots=True)
class SeedPersona:
    """A complete account, as the seeder should find it afterwards."""

    key: str
    email: str
    display_name: str
    status: UserStatus = UserStatus.ACTIVE
    roles: tuple[UserRole, ...] = (UserRole.USER,)
    email_verified: bool = True
    native_language: ContentLanguage = ContentLanguage.AR
    english_variant: EnglishVariant = EnglishVariant.BRITISH
    timezone: str = "Asia/Baghdad"
    current_level: CEFRLevel = CEFRLevel.A1
    avatar_key: str | None = None
    locale: Locale = Locale.EN_GB
    explanation_language: ContentLanguage = ContentLanguage.EN
    notification_preferences: NotificationPreferences = field(
        default_factory=NotificationPreferences
    )
    daily_goal_minutes: int = 30
    daily_goal_new_words: int = 10
    devices: tuple[SeedDevice, ...] = ()
    ai_calls: tuple[SeedAiCall, ...] = ()


_GPT_MINI = ("openai", "gpt-4o-mini")
_HAIKU = ("anthropic", "claude-3-5-haiku-latest")

# The demo only ever meters the two cheap models the app is configured with, so the
# provider/model pair comes from a short alias table rather than being repeated across
# every persona. An unknown alias is a bug in this file, not a runtime condition.
_MODELS: dict[str, tuple[str, str]] = {"mini": _GPT_MINI, "haiku": _HAIKU}


def _usage(
    feature: str,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    days_ago: int = 0,
    hours_ago: int = 0,
    status: str = "ok",
    error: str | None = None,
) -> SeedAiCall:
    provider, model_name = _MODELS[model]
    return SeedAiCall(
        feature=feature,
        provider=provider,
        model=model_name,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        status=status,
        error=error,
        days_ago=days_ago,
        hours_ago=hours_ago,
    )


PERSONAS: tuple[SeedPersona, ...] = (
    SeedPersona(
        key="admin",
        email="admin@natiqo.app",
        display_name="Ali Al-Hashemi",
        roles=(UserRole.ADMIN, UserRole.SUPPORT),
        current_level=CEFRLevel.B2,
        devices=(
            SeedDevice(platform="web", user_agent="Chrome 128 (macOS)", ip_address="127.0.0.1"),
            SeedDevice(
                platform="android",
                push_token="demo-push-token-admin-android",
                app_version="1.4.0",
                user_agent="Natiqo/1.4.0 (Android 15)",
            ),
        ),
        ai_calls=(
            _usage(
                "content_helper",
                "mini",
                input_tokens=820,
                output_tokens=1_450,
                latency_ms=2_100,
                days_ago=3,
            ),
            _usage(
                "content_helper",
                "mini",
                input_tokens=640,
                output_tokens=1_120,
                latency_ms=1_870,
                days_ago=2,
                hours_ago=4,
            ),
        ),
    ),
    SeedPersona(
        key="learner",
        email="amina@natiqo.app",
        display_name="Amina Karim",
        current_level=CEFRLevel.A1,
        devices=(
            SeedDevice(
                platform="android",
                push_token="demo-push-token-amina-android",
                app_version="1.4.0",
                user_agent="Natiqo/1.4.0 (Android 14)",
            ),
        ),
        notification_preferences=NotificationPreferences(
            push=True, daily_reminder=True, streak_warning=True, review_due=True
        ),
        daily_goal_minutes=20,
        daily_goal_new_words=8,
        ai_calls=(
            _usage(
                "explain_answer",
                "mini",
                input_tokens=310,
                output_tokens=420,
                latency_ms=1_240,
                days_ago=2,
            ),
            _usage(
                "tutor_chat",
                "haiku",
                input_tokens=1_150,
                output_tokens=980,
                latency_ms=3_400,
                days_ago=1,
                hours_ago=2,
            ),
            _usage(
                "explain_answer",
                "mini",
                input_tokens=280,
                output_tokens=390,
                latency_ms=1_090,
                hours_ago=5,
            ),
        ),
    ),
    SeedPersona(
        key="yusuf",
        email="yusuf@natiqo.app",
        display_name="Yusuf Mohammed",
        current_level=CEFRLevel.A2,
        devices=(
            SeedDevice(
                platform="android",
                push_token="demo-push-token-yusuf-android",
                app_version="1.3.2",
                user_agent="Natiqo/1.3.2 (Android 13)",
            ),
        ),
        notification_preferences=NotificationPreferences(
            streak_warning=True, quiet_hours_start=23, quiet_hours_end=7
        ),
        daily_goal_minutes=45,
        daily_goal_new_words=15,
        ai_calls=(
            _usage(
                "writing_correction",
                "mini",
                input_tokens=540,
                output_tokens=610,
                latency_ms=1_760,
                days_ago=5,
            ),
            _usage(
                "speaking_feedback",
                "haiku",
                input_tokens=220,
                output_tokens=480,
                latency_ms=2_950,
                days_ago=1,
            ),
        ),
    ),
    SeedPersona(
        key="layla",
        email="layla@natiqo.app",
        display_name="Layla Hassan",
        current_level=CEFRLevel.B1,
        devices=(
            SeedDevice(platform="web", user_agent="Safari 18 (iPadOS)", ip_address="127.0.0.1"),
            SeedDevice(
                platform="ios",
                push_token="demo-push-token-layla-ios",
                app_version="1.4.0",
                user_agent="Natiqo/1.4.0 (iOS 18.2)",
            ),
        ),
        notification_preferences=NotificationPreferences(weekly_report=True, daily_reminder=False),
        daily_goal_minutes=60,
        daily_goal_new_words=20,
        ai_calls=(
            _usage(
                "tutor_chat",
                "haiku",
                input_tokens=1_480,
                output_tokens=1_320,
                latency_ms=4_100,
                days_ago=4,
            ),
            _usage(
                "writing_correction",
                "haiku",
                input_tokens=1_020,
                output_tokens=890,
                latency_ms=3_200,
                days_ago=2,
                hours_ago=3,
            ),
        ),
    ),
    SeedPersona(
        key="omar",
        email="omar@natiqo.app",
        display_name="Omar Abdulrahman",
        status=UserStatus.SUSPENDED,
        current_level=CEFRLevel.A2,
        devices=(
            SeedDevice(platform="web", user_agent="Chrome 127 (Windows)", ip_address="127.0.0.1"),
        ),
        ai_calls=(
            _usage(
                "explain_answer",
                "mini",
                input_tokens=260,
                output_tokens=340,
                latency_ms=1_150,
                days_ago=9,
            ),
            # A failed call is seeded on purpose: an empty error column would let a bug
            # that stops recording `error` survive, because nothing would ever have a
            # non-null value to compare against.
            _usage(
                "speaking_feedback",
                "mini",
                input_tokens=310,
                output_tokens=0,
                latency_ms=30_000,
                days_ago=7,
                status="timeout",
                error="Provider did not respond within 30s",
            ),
        ),
    ),
    SeedPersona(
        key="hina",
        email="hina@natiqo.app",
        display_name="Hina Saeed",
        status=UserStatus.PENDING_VERIFICATION,
        email_verified=False,
        native_language=ContentLanguage.IQ,
        current_level=CEFRLevel.A1,
        devices=(
            SeedDevice(
                platform="android",
                push_token="demo-push-token-hina-android",
                app_version="1.4.0",
                user_agent="Natiqo/1.4.0 (Android 12)",
            ),
        ),
        notification_preferences=NotificationPreferences(
            push=False, email=True, daily_reminder=True
        ),
        daily_goal_minutes=15,
        daily_goal_new_words=5,
    ),
)


__all__ = [
    "PERSONAS",
    "SEED_NAMESPACE",
    "SeedAiCall",
    "SeedDevice",
    "SeedPersona",
]
