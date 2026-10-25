"""Load demo accounts into a Natiqo environment.

    uv run python -m scripts.seed                 # seed every persona
    uv run python -m scripts.seed --dry-run       # show the plan, write nothing
    uv run python -m scripts.seed --only admin    # one persona
    uv run python -m scripts.seed --yes           # no interactive confirmation

Rerunnable. Every id the seeder mints is derived from a fixed namespace and the
persona's key, so a second run recognises its own rows instead of duplicating them and
neither device rows nor usage history grow each time the script is invoked.

Three properties matter more than convenience here, and each is enforced in code rather
than left to the operator:

**A rerun is a no-op for data it did not create.** See :func:`_seed_one` for the
ownership check. A seeded account carries a deterministic provider identity
(:func:`seed_subject`), and that is what marks it as the seeder's; an account holding a
demo address without that identity is skipped without a single write.

That last case has one deliberate exit: ``--adopt``. It takes over an account that holds
a persona's address but has **no provider identity at all** — the shape an older run of
this seeder left behind — and it refuses the same account the moment any provider has
been used to sign in to it. Adoption writes one identity row and then treats the account
exactly like one this seeder had created, so its history is filled in around whatever is
already there and nothing is deleted.

**A production environment is refused by default.** Seeding a live database would
overwrite real profile and settings rows with fictional data, so the guard is on unless
the operator passes ``--allow-production``. ``--dry-run`` is exempt: it only reads.

**An interruption leaves the database consistent.** Personas are committed one at a
time, so a crash halfway leaves whole accounts seeded and whole accounts untouched —
never half a persona. The next run converges from wherever it stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum

from sqlalchemy.exc import DBAPIError

from app.core.config import Settings, get_settings
from app.core.database import dispose_engine, make_uow
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.time import utc_now
from app.core.types import CEFRLevel, UserId, UserStatus, new_id
from app.core.uow import UnitOfWorkProtocol
from app.modules.ai.domain.usage import AiUsageEntry
from app.modules.users.application.auth_service import AuthService
from app.modules.users.application.dto import DeviceContext, SocialLoginCommand
from app.modules.users.domain.entities import (
    ExternalIdentity,
    UserDevice,
    UserProfile,
    UserSettings,
)
from app.modules.users.domain.identity import IdentityProvider, VerifiedIdentity
from app.modules.users.domain.policy import normalise_email
from app.platform.ai.providers import estimate_cost
from scripts.seed_activity import ActivityOutcome, seed_activity
from scripts.seed_personas import PERSONAS, SEED_NAMESPACE, SeedDevice, SeedPersona

logger = get_logger("scripts.seed")

#: The level a brand-new account starts at. Anyone the persona places above it got
#: there by taking the placement test, so their profile records that they finished one.
PLACEMENT_FLOOR = CEFRLevel.A1

#: The provider a seeded identity is recorded under. Paired with :func:`seed_subject`,
#: it is what marks an account as this seeder's.
SEED_IDENTITY_PROVIDER = IdentityProvider.GOOGLE


def seed_subject(persona_key: str) -> str:
    """The deterministic provider subject that identifies a seeded account.

    Frozen rather than generated: a subject that changed between runs would stop the
    seeder recognising its own rows, and every invocation would create a second account
    for the same persona.
    """
    return f"seed:{persona_key}"


class SeedIdentityVerifier:
    """A verifier that vouches for exactly one persona.

    The seeder signs in through the real use case instead of writing rows by hand, so
    account creation, the identity row and the first refresh chain all travel the same
    code path the app uses. This class is what makes that possible with no network. It
    is a fixture and lives in a script; the API builds its verifier map from
    configuration, so no request can ever reach it.
    """

    provider = SEED_IDENTITY_PROVIDER

    def __init__(self, identity: VerifiedIdentity) -> None:
        self._identity = identity

    async def verify(self, id_token: str) -> VerifiedIdentity:
        return self._identity


class SeedRefused(RuntimeError):
    """The seed declined to run.

    Always raised before any write, and never swallowed: a script that quietly skips
    what it cannot do is worse than one that stops and says why.
    """


class UnknownPersona(ValueError):
    """``--only`` named a persona that does not exist."""


class SeedAction(StrEnum):
    """What the seeder decided to do with one persona.

    Imperative rather than past tense so one value reads correctly in both modes: the
    report renders "would create" for :attr:`CREATE` when ``dry_run`` is set and
    "created" when it is not, instead of claiming something happened that did not.
    """

    CREATE = "create"
    ENRICH = "enrich"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class PersonaOutcome:
    """The result for one persona. The counts are what *changed*, not what exists.

    On a rerun every count is therefore zero, which is the point: a second invocation
    reporting "6 accounts, 0 changes" has told the operator the database is already in
    the state they asked for.
    """

    key: str
    email: str
    action: SeedAction
    user_id: UserId | None = None
    reason: str | None = None
    devices_added: int = 0
    sessions_issued: int = 0
    ai_usage_added: int = 0
    #: The learning history this persona's plan produced. Empty for a persona that
    #: declares none, which is every account but the admin today.
    activity: ActivityOutcome = field(default_factory=ActivityOutcome)

    @property
    def changed(self) -> bool:
        return bool(
            self.devices_added
            or self.sessions_issued
            or self.ai_usage_added
            or self.activity.changed
        )


@dataclass(frozen=True, slots=True)
class SeedReport:
    dry_run: bool
    outcomes: tuple[PersonaOutcome, ...] = ()

    def count(self, action: SeedAction) -> int:
        return sum(1 for outcome in self.outcomes if outcome.action is action)

    @property
    def devices_added(self) -> int:
        return sum(outcome.devices_added for outcome in self.outcomes)

    @property
    def sessions_issued(self) -> int:
        return sum(outcome.sessions_issued for outcome in self.outcomes)

    @property
    def ai_usage_added(self) -> int:
        return sum(outcome.ai_usage_added for outcome in self.outcomes)

    @property
    def activity_rows(self) -> int:
        return sum(outcome.activity.rows for outcome in self.outcomes)

    def format(self) -> str:
        """Render the report for a human reading a terminal."""
        if not self.outcomes:
            return "No personas selected."

        verb = "would " if self.dry_run else ""
        width = max(len(outcome.key) for outcome in self.outcomes)
        lines = [
            f"{'persona'.ljust(width)}  {'action':<10}  devices  sessions  ai-usage  activity",
        ]
        for outcome in self.outcomes:
            lines.append(
                f"{outcome.key.ljust(width)}  "
                f"{(verb + outcome.action.value):<10}  "
                f"{outcome.devices_added:>7}  {outcome.sessions_issued:>9}  "
                f"{outcome.ai_usage_added:>9}  {outcome.activity.rows:>8}"
            )

        lines.append("")
        lines.append(
            f"{self.count(SeedAction.CREATE)} {verb}create, "
            f"{self.count(SeedAction.ENRICH)} {verb}enrich, "
            f"{self.count(SeedAction.SKIP)} skipped  "
            f"({verb}add {self.devices_added} devices, "
            f"{self.sessions_issued} sessions, {self.ai_usage_added} AI usage rows, "
            f"{self.activity_rows} activity rows)"
        )
        for outcome in self.outcomes:
            if outcome.reason:
                lines.append(f"  {outcome.key}: {outcome.reason}")
            if outcome.activity.changed:
                # "activity 96" says how much was written; the breakdown says what, which
                # is the part an operator actually reads before pointing a client at it.
                lines.append(f"  {outcome.key}: {outcome.activity.format()}")

        if not self.dry_run and not any(outcome.changed for outcome in self.outcomes):
            lines.append("  Nothing changed: this environment was already seeded.")
        return "\n".join(lines)


# --------------------------------------------------------------------- selection
def select_personas(only: Sequence[str] | None) -> tuple[SeedPersona, ...]:
    """Resolve ``--only`` to personas, or return all of them.

    An unknown key is an error rather than a warning. A typo'd ``--only admin`` that
    quietly fell back to seeding every account would be a surprising amount of writing
    to undo.
    """
    if not only:
        return PERSONAS

    wanted = {key.strip().lower() for key in only if key.strip()}
    known = {persona.key for persona in PERSONAS}
    unknown = sorted(wanted - known)
    if unknown:
        raise UnknownPersona(
            f"Unknown persona(s): {', '.join(unknown)}. Available: {', '.join(sorted(known))}"
        )
    # Filtered from PERSONAS rather than looked up by key, so a repeated or differently
    # cased key cannot yield the same persona twice.
    return tuple(persona for persona in PERSONAS if persona.key in wanted)


def _device_context(device: SeedDevice) -> DeviceContext:
    return DeviceContext(
        platform=device.platform,
        push_token=device.push_token,
        app_version=device.app_version,
        user_agent=device.user_agent,
        ip_address=device.ip_address,
    )


def _assert_writable(settings: Settings, *, allow_production: bool, dry_run: bool) -> None:
    """Refuse a production write unless the operator asked for it.

    A dry run is exempt because it only reads, and refusing to *describe* what would
    happen in production would make the flag impossible to use safely: an operator
    would have to guess whether the report was accurate.
    """
    if settings.is_production and not allow_production and not dry_run:
        raise SeedRefused(
            "Refusing to seed a production environment. Re-run with --allow-production "
            "if that is really what you want."
        )


# -------------------------------------------------------------------------- seed
async def run_seed(
    uow: UnitOfWorkProtocol,
    settings: Settings,
    *,
    allow_production: bool = False,
    only: Sequence[str] | None = None,
    dry_run: bool = False,
    adopt: bool = False,
) -> SeedReport:
    """Seed ``uow`` and return what changed.

    The unit of work is passed in already open, because the auth service commits on it
    and the caller owns its lifetime. ``adopt`` opts in to taking over an account that
    holds a persona's address and has never been signed in with a provider; see
    :func:`_adopt_or_skip`.
    """
    personas = select_personas(only)
    _assert_writable(settings, allow_production=allow_production, dry_run=dry_run)

    outcomes: list[PersonaOutcome] = []
    for persona in personas:
        outcomes.append(await _seed_one(uow, settings, persona, dry_run=dry_run, adopt=adopt))
        if not dry_run:
            # One commit per persona. A failure at persona four leaves three complete
            # accounts and one untouched, and the next run resumes from there.
            await uow.commit()

    report = SeedReport(dry_run=dry_run, outcomes=tuple(outcomes))
    logger.info(
        "scripts.seed_completed",
        dry_run=dry_run,
        personas=len(outcomes),
        create=report.count(SeedAction.CREATE),
        enrich=report.count(SeedAction.ENRICH),
        skip=report.count(SeedAction.SKIP),
        ai_usage_added=report.ai_usage_added,
        activity_rows=report.activity_rows,
        adopt=adopt,
    )
    return report


async def _seed_one(
    uow: UnitOfWorkProtocol,
    settings: Settings,
    persona: SeedPersona,
    *,
    dry_run: bool,
    adopt: bool = False,
) -> PersonaOutcome:
    """Decide what to do with one persona, and do it unless this is a dry run.

    Ownership is carried by the identity row, not by a shared credential: an account
    this seeder created is the one whose ``(provider, subject)`` pair is
    :func:`seed_subject`. An account that merely holds a demo address is somebody
    else's and is left entirely alone — unless the operator asks for it explicitly and
    nothing about the account says a person has used it; see :func:`_adopt_or_skip`.
    """
    email = normalise_email(persona.email)
    identity = await uow.identities.find(str(SEED_IDENTITY_PROVIDER), seed_subject(persona.key))
    existing = await uow.users.get(identity.user_id) if identity is not None else None

    if existing is None:
        # No seeded identity behind this persona. If the address is taken anyway, it
        # belongs to a real person, another environment, or an older run of this seeder
        # — and only the operator can tell which, so ``--adopt`` is what decides.
        collision = await uow.users.get_by_email(email)
        if collision is not None:
            return await _adopt_or_skip(
                uow,
                settings,
                persona,
                email,
                collision.id,
                adopt=adopt,
                dry_run=dry_run,
            )

    if dry_run:
        if existing is None:
            return PersonaOutcome(
                key=persona.key,
                email=email,
                action=SeedAction.CREATE,
                devices_added=sum(1 for d in persona.devices if d.creates_device_row),
                sessions_issued=len(persona.devices),
                ai_usage_added=len(persona.ai_calls),
                # Nothing can exist for an account that does not exist yet, so this is the
                # plan's full size — and the run it previews writes exactly that.
                activity=await seed_activity(uow, settings, persona, None, dry_run=True),
            )
        return await _predict_enrichment(uow, settings, persona, existing.id)

    if existing is None:
        return await _create(uow, settings, persona, email)
    return await _enrich(uow, settings, persona, existing.id)


async def _adopt_or_skip(
    uow: UnitOfWorkProtocol,
    settings: Settings,
    persona: SeedPersona,
    email: str,
    user_id: UserId,
    *,
    adopt: bool,
    dry_run: bool,
) -> PersonaOutcome:
    """Take over an existing account, or leave it alone and say why.

    The gate is the identity table. Every account this application creates is created by
    a provider sign-in, so an account with *no* identity row has never been signed in to
    by anybody — it is the shape an older, password-based run of this seeder left behind.
    That is the account ``--adopt`` is for, and the moment a Google or Apple identity
    exists the account is refused however the flag was set: a real learner's history must
    not be handed to whoever reads the demo instructions next.

    Adoption is one write — the seeder's own ``seed:<persona>`` identity — after which the
    account is indistinguishable from one this seeder created, so the rest of the run
    takes the enrichment path and fills in around whatever is already stored.
    """
    held = await uow.identities.list_for_user(user_id)
    if held:
        return PersonaOutcome(
            key=persona.key,
            email=email,
            action=SeedAction.SKIP,
            user_id=user_id,
            reason=(
                "address belongs to an account that has signed in with "
                f"{', '.join(sorted({identity.provider for identity in held}))}; "
                "left untouched even with --adopt"
            ),
        )
    if not adopt:
        return PersonaOutcome(
            key=persona.key,
            email=email,
            action=SeedAction.SKIP,
            user_id=user_id,
            reason=(
                "address already belongs to an unseeded account; left untouched. "
                "Re-run with --adopt if it is a demo account this seeder should take over."
            ),
        )
    if dry_run:
        predicted = await _predict_enrichment(uow, settings, persona, user_id)
        return replace(predicted, reason="would adopt this account and take it over")

    await uow.identities.add(
        ExternalIdentity(
            id=new_id(),
            user_id=user_id,
            provider=str(SEED_IDENTITY_PROVIDER),
            subject=seed_subject(persona.key),
            email=email,
            email_verified=persona.email_verified,
            display_name=persona.display_name,
        )
    )
    # Deliberately enrichment, not creation: the account already exists, so there is
    # nothing to register and no reason to mint a session the operator never asked for.
    return await _enrich(uow, settings, persona, user_id)


async def _predict_enrichment(
    uow: UnitOfWorkProtocol, settings: Settings, persona: SeedPersona, user_id: UserId
) -> PersonaOutcome:
    """Report what :func:`_enrich` *would* change, without changing it.

    An enrichment run is not defined by the persona, it is defined by the difference
    between the persona and the rows already present. Predicting from the persona's own
    size — the obvious shortcut — makes a dry run after a completed seed claim it would
    add eleven devices and eleven usage rows, and send an operator looking for writes
    that will never happen. So the existing rows are read, and the answer is the diff.
    """
    existing_devices = await uow.user_devices.list_active(user_id)
    existing_ids = {device.id for device in existing_devices}
    existing_tokens = {device.push_token for device in existing_devices if device.push_token}

    # Mirrors _write_devices: a device already reachable by its push token needs no new
    # row, and a token-less one needs no new row if its deterministic id is present.
    added_device_ids = {
        _device_id(persona, index)
        for index, device in enumerate(persona.devices)
        if device.creates_device_row
        and device.push_token not in existing_tokens
        and _device_id(persona, index) not in existing_ids
    }
    added_usage = 0
    for index in range(len(persona.ai_calls)):
        if await uow.ai_usage.get(_usage_id(persona, index)) is None:
            added_usage += 1
    return PersonaOutcome(
        key=persona.key,
        email=normalise_email(persona.email),
        action=SeedAction.ENRICH,
        user_id=user_id,
        devices_added=len(added_device_ids - existing_ids),
        # Deliberately omitted: enrichment never mints a session, so the honest number
        # is zero and the default already is.
        ai_usage_added=added_usage,
        activity=await seed_activity(uow, settings, persona, user_id, dry_run=True),
    )


def _build_auth(
    uow: UnitOfWorkProtocol, settings: Settings, persona: SeedPersona, email: str
) -> AuthService:
    """An ``AuthService`` whose only verifier trusts this persona.

    Passing the credential-free command through the real service is what keeps the
    seeder from drifting away from the app: if sign-in ever stops creating a profile,
    or starts creating two identity rows, the seed breaks at the same moment the app
    does — which is the point.
    """
    identity = VerifiedIdentity(
        provider=SEED_IDENTITY_PROVIDER,
        subject=seed_subject(persona.key),
        email=email,
        email_verified=persona.email_verified,
        display_name=persona.display_name,
    )
    return AuthService(
        uow,
        settings,
        verifiers={SEED_IDENTITY_PROVIDER: SeedIdentityVerifier(identity)},
    )


async def _create(
    uow: UnitOfWorkProtocol,
    settings: Settings,
    persona: SeedPersona,
    email: str,
) -> PersonaOutcome:
    """Sign the account in for the first time, then bring its rows up to the persona.

    ``login_with_identity`` both creates the account and mints the first refresh chain,
    and it commits internally, so it cannot share a transaction with what follows. That
    is acceptable here and only here: a crash between the two leaves a real, usable
    account missing some demo extras, which the next run enriches. A use case whose
    halves must land together is a different problem, and the fix for it belongs in the
    service rather than in a script that works around it.

    Device rows are deliberately *not* written here. The sign-in creates the first and
    each :func:`_mint_extra_sessions` sign-in creates the rest, through the same
    ``upsert_for_token`` path the app uses — so the seeder would be doing the same work
    twice, and doing it in the one order that breaks: with ``autoflush`` off, a row this
    function had pending would be invisible to the SELECT inside the next upsert, and
    the duplicate would only surface as a unique-constraint violation at flush.
    """
    primary = persona.devices[0] if persona.devices else SeedDevice()
    auth = _build_auth(uow, settings, persona, email)
    result = await auth.login_with_identity(
        SocialLoginCommand(
            provider=SEED_IDENTITY_PROVIDER,
            id_token=seed_subject(persona.key),
            display_name=persona.display_name,
            native_language=persona.native_language,
            english_variant=persona.english_variant,
            timezone=persona.timezone,
            locale=persona.locale,
            device=_device_context(primary),
        )
    )
    user_id = result.user_id
    await _write_account(uow, settings, persona, user_id)
    sessions = await _mint_extra_sessions(uow, settings, persona, email)
    activity = await seed_activity(uow, settings, persona, user_id, dry_run=False)
    return PersonaOutcome(
        key=persona.key,
        email=email,
        action=SeedAction.CREATE,
        user_id=user_id,
        # The account did not exist a moment ago, so every device row it now holds is
        # one this run added. Counted from the definition rather than measured, because
        # there is nothing to measure against — and it is the same number the dry run
        # predicts, which is what makes the two reports comparable.
        devices_added=sum(1 for d in persona.devices if d.creates_device_row),
        sessions_issued=1 + sessions,
        ai_usage_added=await _write_usage(uow, persona, user_id),
        activity=activity,
    )


async def _enrich(
    uow: UnitOfWorkProtocol,
    settings: Settings,
    persona: SeedPersona,
    user_id: UserId,
) -> PersonaOutcome:
    """Bring an existing, password-verified account up to the persona.

    Deliberately mints no sessions. Registration is the only moment a demo account
    should gain a refresh chain; a rerun that issued one per run would leave a growing
    pile of live tokens for an account whose password is published in the README.
    """
    before = await _device_ids(uow, user_id)
    await _write_account(uow, settings, persona, user_id)
    await _write_devices(uow, persona, user_id)
    after = await _device_ids(uow, user_id)
    activity = await seed_activity(uow, settings, persona, user_id, dry_run=False)
    return PersonaOutcome(
        key=persona.key,
        email=normalise_email(persona.email),
        action=SeedAction.ENRICH,
        user_id=user_id,
        # Measured rather than declared: this account may already hold a subset of its
        # devices, and reporting the persona's full device count every run would claim
        # progress that did not happen.
        devices_added=len(after - before),
        ai_usage_added=await _write_usage(uow, persona, user_id),
        activity=activity,
    )


async def _mint_extra_sessions(
    uow: UnitOfWorkProtocol, settings: Settings, persona: SeedPersona, email: str
) -> int:
    """Sign in once per extra device, so a two-device persona has two refresh chains.

    Skipped for accounts that cannot authenticate. A suspended demo account keeps the
    session minted at first sign-in — which is what a real suspension looks like — but
    it cannot sign in again to mint more.
    """
    if len(persona.devices) < 2:
        return 0
    if persona.status not in (UserStatus.ACTIVE, UserStatus.PENDING_VERIFICATION):
        return 0
    auth = _build_auth(uow, settings, persona, email)
    for device in persona.devices[1:]:
        await auth.login_with_identity(
            SocialLoginCommand(
                provider=SEED_IDENTITY_PROVIDER,
                id_token=seed_subject(persona.key),
                device=_device_context(device),
            )
        )
    return len(persona.devices) - 1


async def _device_ids(uow: UnitOfWorkProtocol, user_id: UserId) -> frozenset[uuid.UUID]:
    return frozenset(device.id for device in await uow.user_devices.list_active(user_id))


async def _write_account(
    uow: UnitOfWorkProtocol,
    settings: Settings,
    persona: SeedPersona,
    user_id: UserId,
) -> None:
    """Write the account, profile and settings rows for one persona."""
    user = await uow.users.get(user_id)
    if user is None:  # pragma: no cover - a missing row here means the UoW lied
        raise SeedRefused(f"{persona.key}: account {user_id} disappeared mid-seed")

    user.status = persona.status
    user.is_admin = persona.is_admin
    if persona.email_verified and user.email_verified_at is None:
        user.verify_email()
    await uow.users.update(user)

    profile = await uow.users.get_profile(user_id)
    if profile is None:
        profile = UserProfile(user_id=user_id, display_name=persona.display_name)
    profile.display_name = persona.display_name
    profile.avatar_key = persona.avatar_key
    profile.native_language = persona.native_language
    profile.english_variant = persona.english_variant
    profile.timezone = persona.timezone
    profile.current_level = persona.current_level
    if persona.current_level is not PLACEMENT_FLOOR and profile.placement_test_completed_at is None:
        # A B1 learner with no placement test on record looks like someone who skipped
        # onboarding, which is a bug report waiting to happen.
        profile.placement_test_completed_at = utc_now()
    await uow.users.save_profile(profile)

    row = await uow.users.get_settings(user_id)
    if row is None:
        row = UserSettings(user_id=user_id, daily_goal_minutes=settings.daily_goal_default)
    row.locale = persona.locale
    row.explanation_language = persona.explanation_language
    row.notification_preferences = persona.notification_preferences
    row.daily_goal_minutes = persona.daily_goal_minutes
    row.daily_goal_new_words = persona.daily_goal_new_words
    await uow.users.save_settings(row)


async def _write_devices(uow: UnitOfWorkProtocol, persona: SeedPersona, user_id: UserId) -> None:
    """Upsert every device row the persona declares.

    Only reached on the enrichment path: the create path gets its rows from
    ``register`` and ``login`` instead. Keyed on the push token, so a rerun reattaches
    to the row it wrote last time rather than adding a second one for the same install.

    Each device is upserted with a *distinct* deterministic id. The id is only ever used
    when the token matches nothing, but it has to be unique per row: sharing one id
    across two devices would turn a genuinely new second device into a primary-key
    collision.
    """
    for index, device in enumerate(persona.devices):
        if not device.creates_device_row:
            # No push token and no app version means a web session: a refresh chain
            # with nothing to push to, and therefore no device row, exactly as
            # AuthService._issue_session decides it.
            continue
        device_id = _device_id(persona, index)
        if not device.push_token and await uow.user_devices.get(device_id) is not None:
            # A device with a push token is matched by that token, which is the correct
            # key: it survives a reinstall that hands the install a new row id. A device
            # *without* a token cannot be matched that way, so upsert_for_token falls
            # through to a plain insert under the deterministic id. Re-submitting it on
            # a later run would collide on the primary key, so the id is checked first.
            continue
        await uow.user_devices.upsert_for_token(
            UserDevice(
                id=device_id,
                user_id=user_id,
                platform=device.platform,
                push_token=device.push_token,
                app_version=device.app_version,
                last_seen_at=utc_now(),
            )
        )


def _device_id(persona: SeedPersona, index: int) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, f"device:{persona.key}:{index}")


def _usage_id(persona: SeedPersona, index: int) -> uuid.UUID:
    return uuid.uuid5(SEED_NAMESPACE, f"usage:{persona.key}:{index}")


async def _write_usage(uow: UnitOfWorkProtocol, persona: SeedPersona, user_id: UserId) -> int:
    """Insert the persona's historical AI calls. Returns how many rows were written."""
    written = 0
    for index, call in enumerate(persona.ai_calls):
        entry = AiUsageEntry(
            id=_usage_id(persona, index),
            user_id=user_id,
            feature=call.feature,
            provider=call.provider,
            model=call.model,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            latency_ms=call.latency_ms,
            # Derived from the same pricing table production bills against, rather than
            # hard-coded. A literal dollar figure here would drift the first time a
            # price changed, and demo spend that disagrees with the estimator is worse
            # than no demo spend at all.
            cost_usd=estimate_cost(
                call.provider, call.model, call.input_tokens, call.output_tokens
            ),
            status=call.status,
            error=call.error,
            created_at=utc_now() - timedelta(days=call.days_ago, hours=call.hours_ago),
        )
        if await uow.ai_usage.add_if_absent(entry):
            written += 1
    return written


# --------------------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.seed",
        description="Load demo accounts into this environment.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation.")
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="Permit seeding a production environment. Off by default.",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="PERSONA",
        help="Seed only this persona. Repeatable. One of: "
        + ", ".join(persona.key for persona in PERSONAS),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    parser.add_argument(
        "--adopt",
        action="store_true",
        help=(
            "Take over an account that holds a persona's address but has no provider "
            "identity, instead of skipping it. An account that has ever been signed in "
            "with Google or Apple is still refused."
        ),
    )
    return parser


def _confirm(plan: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    # Refusing when there is no terminal is the safe reading: a seed that ran unattended
    # in CI or a cron job should be a deliberate --yes, not a surprise.
    if not sys.stdin.isatty():
        print("Refusing to write without confirmation. Re-run with --yes.", file=sys.stderr)
        return False
    print(plan)
    try:
        answer = input("Write these changes? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    # Checked before the prompt, not after: an operator who confirmed a plan and is
    # then refused has been asked to agree to something that will not happen.
    #
    # ``dry_run=args.dry_run`` asks about the *first* pass, which is what decides whether
    # anything is refused before the prompt. Passing False here instead would refuse a
    # production ``--dry-run`` — the one command whose whole purpose is to be safe — and
    # would leave the write pass unguarded for the ``--yes`` path that skips the prompt.
    _assert_writable(settings, allow_production=args.allow_production, dry_run=args.dry_run)

    uow = make_uow()
    try:
        if args.dry_run:
            async with uow:
                report = await run_seed(
                    uow, settings, only=args.only, dry_run=True, adopt=args.adopt
                )
            print(report.format())
            return 0

        # The confirmation needs a plan and a plan needs a dry run. Two passes over the
        # same code path is the price of not writing rows before the operator agrees —
        # and the same ``adopt`` on both, or the preview would describe another run.
        async with uow:
            preview = await run_seed(uow, settings, only=args.only, dry_run=True, adopt=args.adopt)
        if not _confirm(preview.format(), assume_yes=args.yes):
            print("Nothing written.", file=sys.stderr)
            return 1

        async with uow:
            report = await run_seed(
                uow,
                settings,
                only=args.only,
                allow_production=args.allow_production,
                adopt=args.adopt,
            )
        print(report.format())
        return 0
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except SeedRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 1
    except UnknownPersona as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except AppError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    except DBAPIError as exc:
        # ``DBAPIError`` rather than ``OperationalError``: that is the class SQLAlchemy
        # actually raises for an unreachable host or a rejected password, and it is the
        # *parent* of ``OperationalError``, so catching only the child let the two most
        # likely startup failures escape as a stack trace.
        #
        # The overwhelmingly common cause is a missing schema, and the fix is a command
        # the operator can copy instead of a stack trace.
        print(
            f"Database error: {exc}\n"
            "If the schema is missing, run `uv run alembic upgrade head` first.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
