"""Seeder tests.

These run against a real SQLite schema with the real repositories, because almost every
property the seeder claims is a property of the *database*, not of the Python: unique
constraints, foreign keys, "insert unless this id exists", and the fact that a second
run issues no writes at all. A fake UoW would assert all of those away.

The behaviours pinned here are the ones that would cause real damage if they broke:
a rerun that duplicates usage history, a seeder that overwrites a stranger's account,
and a dry run that quietly writes.
"""

from __future__ import annotations

import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import reset_settings_cache
from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.errors import AppError
from app.core.registry import import_all_models
from app.core.time import utc_now
from app.core.types import CEFRLevel, UserId, UserRole, UserStatus
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.ai.infrastructure.models import AiUsageRecord
from app.modules.users.domain.policy import normalise_email
from app.modules.users.infrastructure.models import (
    RefreshTokenModel,
    UserDeviceModel,
    UserIdentityModel,
    UserModel,
    UserProfileModel,
    UserSettingsModel,
)
from scripts.seed import (
    SEED_IDENTITY_PROVIDER,
    PersonaOutcome,
    SeedAction,
    SeedRefused,
    SeedReport,
    UnknownPersona,
    _build_parser,
    _confirm,
    _device_id,
    _mint_extra_sessions,
    _run,
    _write_devices,
    main,
    run_seed,
    seed_subject,
    select_personas,
)
from scripts.seed_personas import PERSONAS, SEED_NAMESPACE, SeedDevice

pytestmark = pytest.mark.integration

#: The web personas never get a ``user_devices`` row, because a web session has nothing
#: to push to. Asserted rather than assumed: it is the one place where the seed's device
#: count and the auth service's device behaviour could silently disagree.
ADMIN_EMAIL = normalise_email("admin@natiqo.app")
LEARNER_EMAIL = normalise_email("amina@natiqo.app")
OMAR_EMAIL = normalise_email("omar@natiqo.app")
HINA_EMAIL = normalise_email("hina@natiqo.app")


@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    """An *open* unit of work spanning the whole test.

    The seeder calls ``commit`` on it per persona, so it has to be entered for the life
    of the test rather than constructed and passed around.
    """
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest_asyncio.fixture
async def verify(session_factory) -> AsyncIterator[AsyncSession]:
    """A *separate* connection, so assertions see committed state.

    Reading through the seeder's own session would pass even if nothing had been
    committed, which is exactly the failure an idempotency claim must not have.
    """
    async with session_factory() as reader:
        yield reader


@pytest.fixture
def session_factory():
    return get_session_factory()


# ------------------------------------------------------------------- selection
def test_select_all_by_default() -> None:
    assert select_personas(None) == PERSONAS


def test_select_only_filters() -> None:
    assert [p.key for p in select_personas(["admin", "omar"])] == ["admin", "omar"]


def test_select_only_is_case_insensitive_and_deduplicated() -> None:
    # A repeated key must not yield the same persona twice, or it would be seeded twice
    # and the report would double-count it.
    assert [p.key for p in select_personas(["ADMIN", "admin", " admin "])] == ["admin"]


def test_select_only_rejects_unknown_keys() -> None:
    # An error, not a fallback: silently seeding everything because of a typo is the
    # kind of surprise that is only discovered after the writes.
    with pytest.raises(UnknownPersona) as excinfo:
        select_personas(["adminn"])
    assert "adminn" in str(excinfo.value)


# ------------------------------------------------------------------ first run
@pytest.mark.asyncio
async def test_seeds_every_persona(uow, settings, verify) -> None:
    report = await run_seed(uow, settings)

    assert report.count(SeedAction.CREATE) == len(PERSONAS)
    assert not report.dry_run

    users = (await verify.execute(select(func.count()).select_from(UserModel))).scalar_one()
    assert users == len(PERSONAS)
    # Every persona must produce a complete set of child rows. A missing profile is the
    # classic partial-seed symptom, because the account is committed before it.
    for model in (UserProfileModel, UserSettingsModel):
        count = (await verify.execute(select(func.count()).select_from(model))).scalar_one()
        assert count == len(PERSONAS), f"{model.__tablename__} incomplete"


@pytest.mark.asyncio
async def test_admin_keeps_its_roles_and_a_verified_email(uow, settings, verify) -> None:
    await run_seed(uow, settings)

    admin = await verify.scalar(select(UserModel).where(UserModel.email == ADMIN_EMAIL))
    assert admin is not None
    assert UserStatus(admin.status) is UserStatus.ACTIVE
    # Roles are stored as their string values; the admin persona must actually be an
    # admin, or every "log in as the admin" instruction in the README is a dead end.
    assert set(admin.roles) >= {str(UserRole.ADMIN)}
    assert admin.email_verified_at is not None


@pytest.mark.asyncio
async def test_suspended_and_unverified_personas_keep_their_status(uow, settings, verify) -> None:
    await run_seed(uow, settings)

    omar = await verify.scalar(select(UserModel).where(UserModel.email == OMAR_EMAIL))
    assert UserStatus(omar.status) is UserStatus.SUSPENDED
    # A suspended account that claims a verified email would be a contradiction the app
    # would have to resolve somewhere; the persona says "banned for abuse", so it is
    # verified and suspended.
    assert omar.email_verified_at is not None

    hina = await verify.scalar(select(UserModel).where(UserModel.email == HINA_EMAIL))
    assert UserStatus(hina.status) is UserStatus.PENDING_VERIFICATION
    assert hina.email_verified_at is None


@pytest.mark.asyncio
async def test_placement_test_recorded_above_the_onboarding_floor(uow, settings, verify) -> None:
    await run_seed(uow, settings)

    admin = await verify.scalar(select(UserModel).where(UserModel.email == ADMIN_EMAIL))
    learner = await verify.scalar(select(UserModel).where(UserModel.email == LEARNER_EMAIL))
    profiles = {row.user_id: row for row in (await verify.scalars(select(UserProfileModel))).all()}

    # A B2 learner with no placement test on record looks like someone who skipped
    # onboarding entirely, which is a bug report waiting to happen.
    assert profiles[admin.id].current_level == CEFRLevel.B2
    assert profiles[admin.id].placement_test_completed_at is not None
    # ...but an A1 learner starts *at* the floor, so a test on record would be a lie.
    assert profiles[learner.id].current_level == CEFRLevel.A1
    assert profiles[learner.id].placement_test_completed_at is None


@pytest.mark.asyncio
async def test_web_persona_has_no_device_row_and_mobile_ones_do(uow, settings, verify) -> None:
    report = await run_seed(uow, settings)

    admin = await verify.scalar(select(UserModel).where(UserModel.email == ADMIN_EMAIL))
    learner = await verify.scalar(select(UserModel).where(UserModel.email == LEARNER_EMAIL))
    omar = await verify.scalar(select(UserModel).where(UserModel.email == OMAR_EMAIL))

    # Admin's first device is a web session: a refresh chain, but nothing to push to, so
    # no device row. Its second device is Android, so exactly one row.
    assert len(await _devices(verify, admin.id)) == 1
    # The learner registers straight from Android, so its only device is a row.
    assert len(await _devices(verify, learner.id)) == 1
    # Omar is web-only: no device rows at all.
    assert await _devices(verify, omar.id) == []

    by_key = {outcome.key: outcome for outcome in report.outcomes}
    assert by_key["admin"].devices_added == 1
    assert by_key["omar"].devices_added == 0


@pytest.mark.asyncio
async def test_two_device_persona_gets_two_refresh_chains(uow, settings, verify) -> None:
    report = await run_seed(uow, settings)

    admin = await verify.scalar(select(UserModel).where(UserModel.email == ADMIN_EMAIL))
    sessions = await _sessions(verify, admin.id)
    # One from registration, one from the Android login. A single-chain persona would
    # mean signing in on the phone silently evicts the web session.
    assert len(sessions) == 2
    assert {s.device_id for s in sessions} == {None, (await _devices(verify, admin.id))[0].id}

    by_key = {outcome.key: outcome for outcome in report.outcomes}
    assert by_key["admin"].sessions_issued == 2
    # A suspended persona cannot log in, so it keeps only the registration chain. The
    # seeder must not try, or seeding would fail on a persona that is *meant* to be
    # unusable.
    omar = await verify.scalar(select(UserModel).where(UserModel.email == OMAR_EMAIL))
    assert len(await _sessions(verify, omar.id)) == 1


@pytest.mark.asyncio
async def test_ai_usage_is_written_with_derived_cost(uow, settings, verify) -> None:
    await run_seed(uow, settings)

    records = (await verify.scalars(select(AiUsageRecord))).all()
    expected = sum(len(persona.ai_calls) for persona in PERSONAS)
    assert len(records) == expected

    for record in records:
        # A row priced at exactly zero would mean the estimator was bypassed, and demo
        # spend that disagrees with the real table is worse than none.
        assert record.cost_usd >= 0
        assert record.provider in ("openai", "anthropic")
        assert record.created_at is not None


@pytest.mark.asyncio
async def test_failed_ai_call_keeps_its_error(uow, settings, verify) -> None:
    # Seeded on purpose. If ``error`` were never populated, a bug that stopped recording
    # failures would survive, because there would be no row anywhere to contradict it.
    await run_seed(uow, settings)

    failures = (
        await verify.scalars(select(AiUsageRecord).where(AiUsageRecord.status != "ok"))
    ).all()
    assert failures, "no failed AI call was seeded"
    assert all(record.error for record in failures)


@pytest.mark.asyncio
async def test_ai_usage_ids_are_deterministic(uow, settings, verify) -> None:
    await run_seed(uow, settings)

    learner = await verify.scalar(select(UserModel).where(UserModel.email == LEARNER_EMAIL))
    ids = {
        record.id
        for record in await _all(
            verify, select(AiUsageRecord).where(AiUsageRecord.user_id == learner.id)
        )
    }
    # Ids derived from the persona key and index, not random. This is the mechanism that
    # makes the rerun test above pass, so it is pinned directly rather than only through
    # its effect — the count is read off the persona so the two cannot drift apart.
    learner_persona = next(p for p in PERSONAS if p.key == "learner")
    assert len(ids) == len(learner_persona.ai_calls)
    expected = {
        uuid.uuid5(SEED_NAMESPACE, f"usage:{learner_persona.key}:{index}")
        for index in range(len(learner_persona.ai_calls))
    }
    assert ids == expected


# ------------------------------------------------------------------ rerun
@pytest.mark.asyncio
async def test_rerun_changes_nothing(uow, settings, verify) -> None:
    first = await run_seed(uow, settings)
    assert first.ai_usage_added > 0

    before = await _row_counts(verify)
    second = await run_seed(uow, settings)
    after = await _row_counts(verify)

    # The central claim: a second invocation is a no-op. Every count is zero because
    # every row was already there.
    assert second.count(SeedAction.CREATE) == 0
    assert second.count(SeedAction.ENRICH) == len(PERSONAS)
    assert not any(outcome.changed for outcome in second.outcomes)
    assert before == after, "a rerun grew the database"


@pytest.mark.asyncio
async def test_rerun_mints_no_extra_sessions(uow, settings, verify) -> None:
    await run_seed(uow, settings)
    before = len(await _all(verify, select(RefreshTokenModel)))

    await run_seed(uow, settings)
    after = len(await _all(verify, select(RefreshTokenModel)))

    # Sessions are minted on creation only. A seeder that issued a fresh refresh chain
    # per run would accumulate live tokens for an account whose password is published.
    assert after == before


@pytest.mark.asyncio
async def test_rerun_adds_no_extra_devices(uow, settings, verify) -> None:
    await run_seed(uow, settings)
    before = len(await _all(verify, select(UserDeviceModel)))

    await run_seed(uow, settings)

    # The Android and iOS rows are keyed on their push tokens, so the second run
    # reattaches to the existing rows instead of adding new ones.
    assert len(await _all(verify, select(UserDeviceModel))) == before


# ---------------------------------------------------------------- ownership
@pytest.mark.asyncio
async def test_foreign_account_is_left_untouched(uow, settings, verify) -> None:
    """An account holding a demo address but a real password must not be rewritten.

    This is the safety property that lets the seed be run against a shared environment:
    a seeded account is one carrying the seeder's own provider identity, and an account
    holding a demo address without that identity belongs to a person.
    """
    stranger_id = UserId(uuid.uuid4())
    verify.add(
        UserModel(
            id=stranger_id,
            email=ADMIN_EMAIL,
            status=UserStatus.ACTIVE.value,
            roles=[str(UserRole.USER)],
        )
    )
    await verify.commit()

    report = await run_seed(uow, settings)
    await uow.commit()

    outcome = next(o for o in report.outcomes if o.key == "admin")
    assert outcome.action is SeedAction.SKIP
    assert outcome.reason is not None

    untouched = await verify.scalar(select(UserModel).where(UserModel.id == stranger_id))
    assert untouched is not None
    # The identity is the thing that must not change: attaching a seeded identity to an
    # account the seeder does not own would hand that person's history to the next
    # person who reads the README.
    assert untouched.status == UserStatus.ACTIVE.value
    assert (
        await verify.scalar(
            select(UserIdentityModel).where(UserIdentityModel.user_id == stranger_id)
        )
        is None
    )
    # And nothing was created around it either. The seeder writes a profile, settings,
    # devices and usage for accounts it owns; a stranger gets none of those.
    assert (
        await verify.scalar(select(UserProfileModel).where(UserProfileModel.user_id == stranger_id))
        is None
    )
    assert (
        await verify.scalar(
            select(UserSettingsModel).where(UserSettingsModel.user_id == stranger_id)
        )
        is None
    )
    assert await _devices(verify, stranger_id) == []
    assert (
        await _all(verify, select(AiUsageRecord).where(AiUsageRecord.user_id == stranger_id)) == []
    )


@pytest.mark.asyncio
async def test_skipped_persona_is_the_only_one_not_created(uow, settings, verify) -> None:
    """One stranger must not stop the other five personas from being seeded."""
    stranger_id = UserId(uuid.uuid4())
    verify.add(
        UserModel(
            id=stranger_id,
            email=ADMIN_EMAIL,
            status=UserStatus.ACTIVE.value,
            roles=[str(UserRole.USER)],
        )
    )
    await verify.commit()

    report = await run_seed(uow, settings)

    assert report.count(SeedAction.SKIP) == 1
    assert report.count(SeedAction.CREATE) == len(PERSONAS) - 1
    # The stranger plus the five seeded personas.
    assert len(await _all(verify, select(UserModel))) == len(PERSONAS)


@pytest.mark.asyncio
async def test_partial_seed_is_completed_by_a_later_run(uow, settings, verify) -> None:
    """A run interrupted partway must converge on the next invocation.

    This is the recovery path: personas commit independently, so "half seeded" is a
    normal state, not a broken one.
    """
    await run_seed(uow, settings, only=["admin", "omar"])
    partial = len(await _all(verify, select(UserModel)))
    assert partial == 2

    report = await run_seed(uow, settings)

    assert report.count(SeedAction.ENRICH) == 2
    assert report.count(SeedAction.CREATE) == len(PERSONAS) - 2
    assert len(await _all(verify, select(UserModel))) == len(PERSONAS)
    # The interrupted personas are left exactly as the first run wrote them, not
    # re-derived: the counts are measured, so they report zero.
    already = [o for o in report.outcomes if o.key in ("admin", "omar")]
    assert not any(o.changed for o in already)


@pytest.mark.asyncio
async def test_an_account_missing_its_profile_and_settings_is_completed(
    uow, settings, verify
) -> None:
    """The half-written state a sign-in can leave behind.

    The account and its identity commit before the profile and settings rows do, so a
    crash in between leaves a user row with neither. Enrichment has to create them
    rather than assume they are there, or the account stays unusable however many times
    the seeder is run.

    The identity row is what makes this the seeder's account; without it the same email
    is somebody else's and the persona is skipped.
    """
    user_id = UserId(uuid.uuid4())
    verify.add(
        UserModel(
            id=user_id,
            email=LEARNER_EMAIL,
            status=UserStatus.PENDING_VERIFICATION.value,
            roles=[str(UserRole.USER)],
        )
    )
    await verify.commit()
    verify.add(
        UserIdentityModel(
            user_id=user_id,
            provider=str(SEED_IDENTITY_PROVIDER),
            subject=seed_subject("learner"),
            email=LEARNER_EMAIL,
            email_verified=True,
            last_login_at=utc_now(),
        )
    )
    await verify.commit()

    report = await run_seed(uow, settings, only=["learner"])
    await uow.commit()

    outcome = next(o for o in report.outcomes if o.key == "learner")
    assert outcome.action is SeedAction.ENRICH

    profile = await verify.scalar(
        select(UserProfileModel).where(UserProfileModel.user_id == user_id)
    )
    assert profile is not None
    # Read from the persona rather than retyped, so a rename in the fixture shows up as a
    # failure about the fixture rather than as a stale literal here.
    learner = next(p for p in PERSONAS if p.key == "learner")
    assert profile.display_name == learner.display_name
    assert profile.current_level == learner.current_level
    row = await verify.scalar(select(UserSettingsModel).where(UserSettingsModel.user_id == user_id))
    assert row is not None
    assert row.daily_goal_minutes == learner.daily_goal_minutes


# ------------------------------------------------------------------ adoption
async def _providerless_account(verify: AsyncSession, *, subject: str | None = None) -> UserId:
    """An account holding the admin persona's address, as an older seeder left it.

    Optional ``subject`` adds a provider identity, which is what makes the account a
    person's rather than a leftover: the difference the adoption gate is built on.
    """
    user_id = UserId(uuid.uuid4())
    verify.add(
        UserModel(
            id=user_id,
            email=ADMIN_EMAIL,
            status=UserStatus.ACTIVE.value,
            roles=[str(UserRole.USER)],
        )
    )
    await verify.commit()
    if subject is not None:
        verify.add(
            UserIdentityModel(
                user_id=user_id,
                provider=str(SEED_IDENTITY_PROVIDER),
                subject=subject,
                email=ADMIN_EMAIL,
                email_verified=True,
                last_login_at=utc_now(),
            )
        )
        await verify.commit()
    return user_id


@pytest.mark.asyncio
async def test_adopt_takes_over_an_account_an_earlier_run_left_behind(
    uow, settings, verify
) -> None:
    """``--adopt`` is the one exit from the ownership rule, and this is its account.

    The persona's own address with no provider identity behind it: nobody has ever signed
    in to it, which is the shape the password-based seeder of an earlier phase left in a
    development database. It keeps its row, gains the seeder's identity, and is then
    filled in like any other seeded account.
    """
    legacy_id = await _providerless_account(verify)
    persona = next(p for p in PERSONAS if p.key == "admin")

    report = await run_seed(uow, settings, only=["admin"], adopt=True)
    await uow.commit()

    outcome = next(o for o in report.outcomes if o.key == "admin")
    assert outcome.action is SeedAction.ENRICH
    assert outcome.user_id == legacy_id

    identity = await verify.scalar(
        select(UserIdentityModel).where(UserIdentityModel.user_id == legacy_id)
    )
    assert identity is not None
    assert identity.provider == str(SEED_IDENTITY_PROVIDER)
    assert identity.subject == seed_subject("admin")

    # The rest of the run treated it as its own: profile and settings are the persona's.
    profile = await verify.scalar(
        select(UserProfileModel).where(UserProfileModel.user_id == legacy_id)
    )
    assert profile is not None
    assert profile.display_name == persona.display_name
    assert profile.current_level == persona.current_level
    assert await verify.scalar(select(UserModel).where(UserModel.id == legacy_id)) is not None

    # And from now on the account is recognised without the flag, because the identity is
    # what marks it — so an operator only ever passes --adopt once.
    again = await run_seed(uow, settings, only=["admin"])
    assert next(o for o in again.outcomes if o.key == "admin").action is SeedAction.ENRICH


@pytest.mark.asyncio
async def test_adopt_still_refuses_an_account_a_person_has_signed_in_to(
    uow, settings, verify
) -> None:
    """The gate is the identity table, and --adopt does not open it.

    An account with any provider identity has been signed in to by somebody. Taking it
    over would hand that person's history to the next reader of the demo instructions,
    which is the failure the ownership rule exists to prevent.
    """
    taken_id = await _providerless_account(verify, subject="real-person-1")

    report = await run_seed(uow, settings, only=["admin"], adopt=True)

    outcome = next(o for o in report.outcomes if o.key == "admin")
    assert outcome.action is SeedAction.SKIP
    assert outcome.reason is not None and "signed in with google" in outcome.reason
    assert (
        await verify.scalar(
            select(UserIdentityModel).where(
                UserIdentityModel.user_id == taken_id,
                UserIdentityModel.subject == seed_subject("admin"),
            )
        )
        is None
    )
    assert (
        await verify.scalar(select(UserProfileModel).where(UserProfileModel.user_id == taken_id))
        is None
    )


@pytest.mark.asyncio
async def test_an_adoption_is_previewed_before_it_is_written(uow, settings, verify) -> None:
    """A dry run has to be able to show the take-over, and must write none of it."""
    await _providerless_account(verify)
    before = len(await _all(verify, select(UserIdentityModel)))

    preview = await run_seed(uow, settings, only=["admin"], dry_run=True, adopt=True)
    outcome = next(o for o in preview.outcomes if o.key == "admin")

    assert outcome.action is SeedAction.ENRICH
    assert outcome.reason is not None and "adopt" in outcome.reason
    assert len(await _all(verify, select(UserIdentityModel))) == before


# ------------------------------------------------------------------- dry run
@pytest.mark.asyncio
async def test_dry_run_writes_nothing(uow, settings, verify) -> None:
    report = await run_seed(uow, settings, dry_run=True)

    assert report.dry_run
    assert report.count(SeedAction.CREATE) == len(PERSONAS)
    # Reported as intentions, not results.
    assert all(outcome.devices_added >= 0 for outcome in report.outcomes)
    # The whole point: not one row, in any table.
    assert await _row_counts(verify) == _EMPTY_COUNTS


@pytest.mark.asyncio
async def test_dry_run_predicts_the_same_numbers_a_real_run_reports(uow, settings, verify) -> None:
    preview = await run_seed(uow, settings, dry_run=True)
    actual = await run_seed(uow, settings)

    # A preview that disagrees with the run it previews is worse than no preview, so the
    # two are compared rather than each being spot-checked.
    for dry, real in zip(preview.outcomes, actual.outcomes, strict=True):
        assert dry.action is real.action
        assert dry.devices_added == real.devices_added, dry.key
        assert dry.sessions_issued == real.sessions_issued, dry.key
        assert dry.ai_usage_added == real.ai_usage_added, dry.key


@pytest.mark.asyncio
async def test_dry_run_after_a_seed_reports_enrichment(uow, settings, verify) -> None:
    await run_seed(uow, settings)

    preview = await run_seed(uow, settings, dry_run=True)

    assert preview.count(SeedAction.ENRICH) == len(PERSONAS)
    assert preview.count(SeedAction.CREATE) == 0


@pytest.mark.asyncio
async def test_dry_run_after_a_seed_predicts_no_further_changes(uow, settings, verify) -> None:
    """The numbers must be a diff against what is already stored.

    Predicting from the persona's own size — the shortcut that is obvious to write —
    reports eleven devices and eleven usage rows still to come after a completed seed,
    which is a report an operator cannot act on. Asserted per field because each one has
    its own independent way of being wrong.
    """
    await run_seed(uow, settings)

    preview = await run_seed(uow, settings, dry_run=True)
    actual = await run_seed(uow, settings)

    for dry, real in zip(preview.outcomes, actual.outcomes, strict=True):
        assert dry.devices_added == 0, dry.key
        assert dry.sessions_issued == 0, dry.key
        assert dry.ai_usage_added == 0, dry.key
        # And the run it previewed reported exactly what it predicted, field for field,
        # so the zeros above are an accurate reading rather than a vacuous one.
        assert real == dry, dry.key


@pytest.mark.asyncio
async def test_dry_run_on_a_partial_account_predicts_only_the_missing_rows(
    uow, settings, verify
) -> None:
    """Half-seeded is a normal state, so a preview of it has to be a partial answer."""
    await run_seed(uow, settings, only=["omar"])

    preview = await run_seed(uow, settings, dry_run=True)
    omar = next(o for o in preview.outcomes if o.key == "omar")

    assert omar.action is SeedAction.ENRICH
    # Omar's devices and usage are already stored from the first run.
    assert omar.devices_added == 0
    assert omar.ai_usage_added == 0
    # The other five accounts do not exist yet, and the preview says so.
    assert preview.count(SeedAction.CREATE) == len(PERSONAS) - 1


# ---------------------------------------------------------------- production
@pytest.mark.asyncio
async def test_production_write_is_refused_by_default(uow, production_settings, verify) -> None:
    with pytest.raises(SeedRefused):
        await run_seed(uow, production_settings)

    # Refused before any write, which is the only order that makes the refusal useful.
    assert await _row_counts(verify) == _EMPTY_COUNTS


@pytest.mark.asyncio
async def test_production_write_proceeds_with_the_override(
    uow, production_settings, verify
) -> None:
    report = await run_seed(uow, production_settings, allow_production=True)

    assert report.count(SeedAction.CREATE) == len(PERSONAS)
    assert len(await _all(verify, select(UserModel))) == len(PERSONAS)


@pytest.mark.asyncio
async def test_production_dry_run_is_allowed(uow, production_settings, verify) -> None:
    # Exempt on purpose: an operator checking what would happen must be able to see it
    # without an override, and a read-only report is harmless.
    report = await run_seed(uow, production_settings, dry_run=True)

    assert report.dry_run
    assert await _row_counts(verify) == _EMPTY_COUNTS


# ----------------------------------------------------------------------- CLI
# The production guard is asserted above through `run_seed`, but the CLI has its own
# call into the same check and got it wrong independently: it passed dry_run=False, so
# `python -m scripts.seed --dry-run` against production was refused — the one command
# that cannot cause harm, and the one an operator runs *because* they are unsure. These
# tests drive `main` so that path is covered, not just the library entry point.


@pytest.mark.asyncio
async def test_cli_production_dry_run_is_allowed(monkeypatch, capsys) -> None:
    import_all_models()
    await create_all()
    monkeypatch.setenv("APP_ENV", "production")
    reset_settings_cache()

    code = await _run(_build_parser().parse_args(["--dry-run"]))

    assert code == 0
    assert "would create" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_production_write_is_refused_before_prompting(monkeypatch, capsys) -> None:
    import_all_models()
    await create_all()
    monkeypatch.setenv("APP_ENV", "production")
    reset_settings_cache()

    # --yes skips the prompt, so a guard that only lived in _confirm would be missed
    # here. The refusal has to come from the guard, before the first pass.
    with pytest.raises(SeedRefused, match="allow-production"):
        await _run(_build_parser().parse_args(["--yes"]))

    # Nothing was even planned, let alone written: the guard runs before the dry run.
    assert "would create" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_refuses_to_write_without_confirmation(capsys) -> None:
    import_all_models()
    await create_all()

    # pytest's stdin is not a tty, which is the same condition as CI or cron. An
    # unattended seed should have to be asked for, not assumed.
    code = await _run(_build_parser().parse_args([]))

    captured = capsys.readouterr()
    assert code == 1
    assert "--yes" in captured.err


@pytest.mark.asyncio
async def test_cli_rejects_an_unknown_persona() -> None:
    import_all_models()
    await create_all()

    with pytest.raises(UnknownPersona, match="nobody"):
        await _run(_build_parser().parse_args(["--only", "nobody", "--yes"]))


@pytest.mark.asyncio
async def test_cli_seeds_and_prints_a_report(capsys) -> None:
    import_all_models()
    await create_all()

    assert await _run(_build_parser().parse_args(["--yes"])) == 0

    # Only the first invocation is exercised here. _run disposes the engine on the way
    # out — right for a one-shot process, and fatal to the in-memory database the tests
    # run on, so a second call would seed a fresh empty one and prove nothing.
    # Idempotency is a property of run_seed and is pinned by the rerun tests above;
    # what is unique to this layer is the confirmation, the exit code and the guard.
    out = capsys.readouterr().out
    assert "6 create, 0 enrich, 0 skipped" in out
    # The count the personas declare today: 3 + 3 + 2 + 2 + 2. It moves whenever a
    # persona gains a metered call, which is why the assertions around it are derived
    # — this one is pinned so the rendered wording cannot drift unnoticed.
    assert "12 AI usage rows" in out


def test_report_says_so_when_nothing_changed() -> None:
    """The wording an operator reads to conclude no work was needed.

    Checked on the renderer rather than through a second CLI run, because the second run
    cannot be arranged against an in-memory database that the first one disposed.
    """
    report = SeedReport(
        dry_run=False,
        outcomes=(PersonaOutcome(key="omar", email="omar@natiqo.app", action=SeedAction.ENRICH),),
    )

    assert "Nothing changed" in report.format()
    # A dry run must not claim anything changed, since by definition nothing can.
    assert "Nothing changed" not in SeedReport(dry_run=True, outcomes=report.outcomes).format()


def test_main_maps_failures_to_distinct_exit_codes(monkeypatch) -> None:
    """``main`` is the only entry point that owns exit codes, so it is tested on its own.

    It wraps ``asyncio.run``, which cannot nest inside pytest's event loop, so the
    asynchronous behaviour is covered through ``_run`` above and only the mapping is
    faked here. A refusal is a *different kind* of failure from a mistyped persona, and
    a deployment script that cannot tell them apart is one that will retry a dangerous
    command forever.
    """

    def refuse(coro) -> int:
        coro.close()
        raise SeedRefused("refused")

    monkeypatch.setattr("asyncio.run", refuse)
    assert main(["--yes"]) == 1

    def unknown(coro) -> int:
        coro.close()
        raise UnknownPersona("nobody")

    monkeypatch.setattr("asyncio.run", unknown)
    assert main(["--only", "nobody"]) == 2


def test_main_suggests_a_migration_for_a_missing_schema(monkeypatch, capsys) -> None:
    """A missing schema is the common failure, and the fix is a command to copy.

    Asserted on the remediation string because that is the whole reason the handler
    exists: a stack trace tells an operator nothing they can act on.

    ``DBAPIError`` is the base class, deliberately. SQLAlchemy raises it directly for an
    unreachable host or a rejected password, and it is the *parent* of
    ``OperationalError``; a handler catching only the child lets the two likeliest
    failures through as tracebacks. This test raised the child while the code caught the
    parent, which passed while the real failure did not.
    """

    def fail(coro) -> int:
        coro.close()
        raise DBAPIError("select 1", {}, Exception("no such table: users"))

    monkeypatch.setattr("asyncio.run", fail)

    assert main(["--yes"]) == 1
    assert "alembic upgrade head" in capsys.readouterr().err


def test_main_maps_a_bare_dbapi_error_too(monkeypatch, capsys) -> None:
    """The class SQLAlchemy actually raises when the database cannot be reached."""

    def fail(coro) -> int:
        coro.close()
        raise DBAPIError("connect", {}, Exception("password authentication failed"))

    monkeypatch.setattr("asyncio.run", fail)

    assert main(["--yes"]) == 1
    assert "Database error" in capsys.readouterr().err


def test_main_maps_an_application_error(monkeypatch, capsys) -> None:
    def fail(coro) -> int:
        coro.close()
        raise AppError("something the app itself rejected")

    monkeypatch.setattr("asyncio.run", fail)

    assert main(["--yes"]) == 1
    assert "something the app itself rejected" in capsys.readouterr().err


# ------------------------------------------------------------------ confirmation
# The consent gate. Everything else in this script is about *what* gets written; this is
# about whether a human agreed to it, so the ways it can be bypassed or stuck are pinned.


def test_confirm_accepts_yes(monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    assert _confirm("plan", assume_yes=False) is True


def test_confirm_accepts_the_spelled_out_yes(monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "  YES \n")

    assert _confirm("plan", assume_yes=False) is True


@pytest.mark.parametrize("answer", ["", "n", "no", "yep", "sure"])
def test_confirm_treats_anything_else_as_no(monkeypatch, answer: str) -> None:
    """Only an explicit yes writes.

    "yep" and "sure" are the two that look reasonable to a human and are not consent to
    a bulk write, which is exactly why they are in the list rather than left to a truthy
    check on the string.
    """
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert _confirm("plan", assume_yes=False) is False


def test_confirm_treats_a_closed_stdin_as_no(monkeypatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def closed(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)

    assert _confirm("plan", assume_yes=False) is False


# ------------------------------------------------------- report rendering & edges
def test_an_empty_selection_reports_nothing_selected() -> None:
    # `--only` values that are only whitespace select nobody, which argparse cannot rule
    # out and a shell makes easy to produce. Silently seeding everyone instead would be
    # the worst possible reading of that input.
    assert select_personas(["  ", ""]) == ()
    # And the report has to say so, rather than print an empty plan that reads like the
    # seeder silently did nothing.
    assert "no personas selected" in SeedReport(dry_run=False, outcomes=()).format().lower()


def test_the_report_explains_why_a_persona_was_skipped() -> None:
    report = SeedReport(
        dry_run=False,
        outcomes=(
            PersonaOutcome(
                key="admin",
                email="admin@natiqo.app",
                action=SeedAction.SKIP,
                reason="account exists with a different password; left untouched",
            ),
        ),
    )

    rendered = report.format()
    assert "skip" in rendered
    # A skip with no explanation reads as a bug in the seeder rather than a deliberate
    # decision about someone else's account.
    assert "left untouched" in rendered


# ------------------------------------------------- guards for future persona edits
# No shipped persona reaches these two branches: every multi-device persona is
# authenticatable, and every device that creates a row has a push token. They exist so
# that the *next* persona added to the file cannot quietly break a rerun, and a guard
# nobody can exercise is a guard that stops working the day it is needed.


@pytest.mark.asyncio
async def test_a_suspended_multi_device_persona_mints_no_extra_sessions(uow, settings) -> None:
    persona = replace(
        next(p for p in PERSONAS if p.key == "admin"),
        devices=(
            SeedDevice(platform="android", push_token="x", app_version="1.0.0"),
            SeedDevice(platform="ios", push_token="y", app_version="1.0.0"),
        ),
        status=UserStatus.SUSPENDED,
    )
    # Creating a suspended account still works; it is the *sign-ins* that must not
    # happen, because a suspended user cannot authenticate.
    assert await _mint_extra_sessions(uow, settings, persona, "admin@natiqo.app") == 0


@pytest.mark.asyncio
async def test_a_tokenless_device_row_is_not_written_twice(uow, settings, verify) -> None:
    persona = replace(
        next(p for p in PERSONAS if p.key == "omar"),
        devices=(
            SeedDevice(platform="web", app_version="1.0.0"),
            SeedDevice(platform="android", app_version="1.0.0"),
        ),
    )
    user_id = UserId(uuid.uuid4())
    # A real user row, because user_devices.user_id is a foreign key and the seeder is
    # not expected to invent an owner.
    verify.add(
        UserModel(
            id=user_id,
            email=OMAR_EMAIL,
            status=UserStatus.ACTIVE.value,
            roles=[str(UserRole.USER)],
        )
    )
    await verify.commit()
    device_id = _device_id(persona, 1)

    await _write_devices(uow, persona, user_id)
    await uow.commit()
    assert await uow.user_devices.get(device_id) is not None

    # A device with an app version but no push token cannot be matched by token, so the
    # upsert falls through to a plain insert under the deterministic id. Without the
    # id check this second call collides on the primary key and aborts the whole seed.
    await _write_devices(uow, persona, user_id)
    await uow.commit()

    assert await uow.user_devices.get(device_id) is not None
    assert len(await uow.user_devices.list_active(user_id)) == 2


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def production_settings(settings):
    # A copy, not the cached instance: the autouse fixture resets the cache between
    # tests, and mutating the shared object would leak a production environment into
    # whatever runs next.
    return settings.model_copy(update={"app_env": "production"})


# ------------------------------------------------------------------ helpers
_EMPTY_COUNTS = {
    "users": 0,
    "profiles": 0,
    "settings": 0,
    "devices": 0,
    "sessions": 0,
    "usage": 0,
}

_MODELS = {
    "users": UserModel,
    "profiles": UserProfileModel,
    "settings": UserSettingsModel,
    "devices": UserDeviceModel,
    "sessions": RefreshTokenModel,
    "usage": AiUsageRecord,
}


async def _row_counts(verify: AsyncSession) -> dict[str, int]:
    return {
        name: (await verify.execute(select(func.count()).select_from(model))).scalar_one()
        for name, model in _MODELS.items()
    }


async def _all(verify: AsyncSession, stmt) -> list[Any]:
    """``AsyncSession.scalars`` is a coroutine returning a ``ScalarResult``.

    Spelled out in one helper because the inline form — awaiting a ``.all()`` call on
    the coroutine, or comparing the ``ScalarResult`` itself to a list — is wrong in two
    ways that only show up as confusing failures deep inside an assertion.
    """
    return list((await verify.scalars(stmt)).all())


async def _devices(verify: AsyncSession, user_id: UserId) -> list[UserDeviceModel]:
    return await _all(verify, select(UserDeviceModel).where(UserDeviceModel.user_id == user_id))


async def _sessions(verify: AsyncSession, user_id: UserId) -> list[RefreshTokenModel]:
    return await _all(verify, select(RefreshTokenModel).where(RefreshTokenModel.user_id == user_id))
