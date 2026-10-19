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
class SeedThread:
    """One tutor conversation, as the learner would have held it.

    Only the learner's own turns are declared. The replies are not written here: they are
    produced by the tutor the app ships with (``app.platform.ai.service``, whose
    deterministic fallback is what a learner sees whenever a model is unavailable or
    switched off), so the thread reads like the product rather than like a fixture. See
    :mod:`scripts.seed_activity` for why that matters.
    """

    #: The learner's turns, in order. One entry opens the thread; later ones continue it.
    learner_messages: tuple[str, ...]
    days_ago: int = 1
    #: Position of the seeded lesson the thread was opened from, or ``None`` for a thread
    #: that is not anchored to one. Out of range is treated as ``None``.
    lesson_index: int | None = None


@dataclass(frozen=True, slots=True)
class SeedActivity:
    """A persona's learning history, declared as a *shape* rather than as rows.

    The catalogue it is written against is not declared here, because it must not be:
    which lesson is the fourth one is a property of the curriculum, and a copy of it in
    this file would rot the first time a lesson was inserted. The plan says "fourteen
    lessons, spread over nine days, at these scores" and :mod:`scripts.seed_activity`
    resolves that against whatever is actually published.

    Everything here is a *choice about the demo*, never a rule about the product: the
    points, the XP, the review schedule and the achievements are all produced by the same
    code the app runs. A persona that declares no activity gets exactly the account it
    did before this existed.
    """

    #: How many lessons, in curriculum order, the learner has finished. Capped at the
    #: size of the published catalogue.
    lessons_completed: int = 0
    #: Score, as a percentage, cycled over those completions. The spread is the point: a
    #: screen that only ever shows 100% hides every "needs work" state in the app.
    score_pattern: tuple[int, ...] = (100, 90, 100, 75, 100, 60, 85)
    #: How many days the completions are spread over, ending today. The streak is derived
    #: from these dates, so this is also what decides the streak the account shows.
    study_span_days: int = 7
    #: Local hour the first sitting of a day starts at. Later sittings on the same day
    #: follow it 25 minutes apart.
    first_hour: int = 17
    #: Leave the next lesson half answered, the way closing the app mid-lesson does.
    lesson_in_progress: bool = False
    #: Sessions started and never finished, in lessons after the one above.
    abandoned_sessions: int = 0
    #: How many review cards carry a real SM-2 schedule (i.e. were reviewed). Cards are
    #: drawn from the earliest completions, because a card cannot be reviewed before the
    #: question that created it was answered.
    reviews_reviewed: int = 0
    #: How long before today each review of a card happened, oldest first. The tuple's
    #: length is therefore also how many times each reviewed card was reviewed.
    review_days_ago: tuple[int, ...] = (3, 1)
    #: Whether review *k* was passed, cycled over every review in order. A failed review is
    #: a real lapse: the card goes back to relearning and its interval resets.
    review_pass_pattern: tuple[bool, ...] = (True, True, True, False)
    tutor_threads: tuple[SeedThread, ...] = ()
    #: How many unlocked achievements the learner has already claimed, oldest first.
    #: Claiming pays the reward, so this also decides the gem balance.
    claimed_achievements: int = 0


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
    #: The learning history to write for this account, or ``None`` for an account that
    #: should hold nothing but a profile. See :class:`SeedActivity`.
    activity: SeedActivity | None = None


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
        roles=(UserRole.ADMIN,),
        current_level=CEFRLevel.B2,
        # The admin is the account a reviewer signs in with, so it is the one that has to
        # have something on every screen. Staff also work the course from the beginning
        # when they check content in order, which is why the history starts at A1 even
        # though the profile sits at B2.
        activity=SeedActivity(
            lessons_completed=14,
            score_pattern=(100, 90, 100, 75, 100, 60, 85),
            study_span_days=9,
            lesson_in_progress=True,
            abandoned_sessions=1,
            reviews_reviewed=8,
            review_days_ago=(4, 1),
            # Three passes and a lapse: a retention figure of 75%, which is what an
            # honest review log looks like. A log where nothing is ever forgotten would
            # hide the relearning state the schedule exists to produce.
            review_pass_pattern=(True, True, True, False),
            tutor_threads=(
                SeedThread(
                    learner_messages=("What is the difference between 'make' and 'do'?",),
                    days_ago=3,
                    lesson_index=0,
                ),
                SeedThread(
                    learner_messages=(
                        "Can you explain when to use the past simple?",
                        "And how do I make a negative sentence?",
                    ),
                    days_ago=1,
                    lesson_index=4,
                ),
            ),
            claimed_achievements=2,
        ),
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
            # Appended, not inserted: an entry's id is derived from its position in this
            # tuple, so a call placed in the middle would be re-keyed as a new row and
            # leave the old one behind. Today's call is what ``GET /ai/quota`` reads as
            # spend — the budget is a calendar day, so a persona whose calls are all
            # older than midnight answers that screen with a zero it cannot explain.
            _usage(
                "explain_answer",
                "mini",
                input_tokens=260,
                output_tokens=380,
                latency_ms=1_100,
                hours_ago=0,
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
    "SeedActivity",
    "SeedAiCall",
    "SeedDevice",
    "SeedPersona",
    "SeedThread",
]
