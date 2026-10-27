"""The daily-challenge ledger against a real schema.

Two properties cannot be shown through the HTTP surface: that a retried sitting is a
no-op rather than a second lesson, and that a day's totals are a *sum* over that day's
sittings instead of a stored number somebody has to keep in step. Both live in the
repository, so they are tested here.

The rule these tests defend: a day is decided by the sitting that produced it, and the
identity of that sitting is the only thing that can stop it being counted twice.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.registry import import_all_models
from app.core.types import UserId
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.users.infrastructure.models import (
    UserDailyChallengeModel,
    UserModel,
)

pytestmark = pytest.mark.integration

_DAY = "2026-10-27"
_NEXT_DAY = "2026-10-28"


@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest_asyncio.fixture
async def session(uow: SqlAlchemyUnitOfWork) -> AsyncSession:
    return uow.session


async def _learner(session: AsyncSession) -> UserId:
    row = UserModel(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", status="active")
    session.add(row)
    await session.commit()
    return UserId(row.id)


@pytest.mark.asyncio
async def test_a_sitting_recorded_twice_is_counted_once(
    session: AsyncSession, uow: SqlAlchemyUnitOfWork
) -> None:
    """The second report is the *same* sitting, so there is nothing new to count."""
    user_id = await _learner(session)
    sitting = uuid.uuid4()

    first = await uow.users.record_daily_challenge_contribution(
        user_id,
        session_id=sitting,
        challenge_date=_DAY,
        lessons_completed=1,
        words_learned=4,
    )
    again = await uow.users.record_daily_challenge_contribution(
        user_id,
        session_id=sitting,
        challenge_date=_DAY,
        lessons_completed=1,
        words_learned=4,
    )

    assert first is True
    assert again is False
    day = await uow.users.get_daily_challenge(user_id, challenge_date=_DAY)
    assert day.lessons_completed_today == 1
    assert day.words_learned_today == 4


@pytest.mark.asyncio
async def test_each_day_sums_only_its_own_sittings(
    session: AsyncSession, uow: SqlAlchemyUnitOfWork
) -> None:
    user_id = await _learner(session)

    await uow.users.record_daily_challenge_contribution(
        user_id,
        session_id=uuid.uuid4(),
        challenge_date=_DAY,
        lessons_completed=1,
        words_learned=2,
    )
    await uow.users.record_daily_challenge_contribution(
        user_id,
        session_id=uuid.uuid4(),
        challenge_date=_NEXT_DAY,
        lessons_completed=1,
        words_learned=3,
    )

    today = await uow.users.get_daily_challenge(user_id, challenge_date=_DAY)
    tomorrow = await uow.users.get_daily_challenge(user_id, challenge_date=_NEXT_DAY)
    assert (today.lessons_completed_today, today.words_learned_today) == (1, 2)
    assert (tomorrow.lessons_completed_today, tomorrow.words_learned_today) == (1, 3)


@pytest.mark.asyncio
async def test_reading_a_day_creates_no_day_row(
    session: AsyncSession, uow: SqlAlchemyUnitOfWork
) -> None:
    """A learner who opens the app and does nothing leaves no trace to reconcile."""
    user_id = await _learner(session)

    day = await uow.users.get_daily_challenge(user_id, challenge_date=_DAY)

    assert day.lessons_completed_today == 0
    assert day.words_learned_today == 0
    assert day.claimed is False
    rows = (
        (
            await session.execute(
                select(UserDailyChallengeModel).where(UserDailyChallengeModel.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_one_learners_sittings_are_invisible_to_another(
    session: AsyncSession, uow: SqlAlchemyUnitOfWork
) -> None:
    mine = await _learner(session)
    theirs = await _learner(session)
    await uow.users.record_daily_challenge_contribution(
        mine,
        session_id=uuid.uuid4(),
        challenge_date=_DAY,
        lessons_completed=2,
        words_learned=7,
    )

    day = await uow.users.get_daily_challenge(theirs, challenge_date=_DAY)

    assert (day.lessons_completed_today, day.words_learned_today) == (0, 0)


@pytest.mark.asyncio
async def test_claiming_creates_the_day_row_once_without_touching_progress(
    session: AsyncSession, uow: SqlAlchemyUnitOfWork
) -> None:
    """The claim is the only state a day row holds, and it never rewrites the totals."""
    user_id = await _learner(session)
    await uow.users.record_daily_challenge_contribution(
        user_id,
        session_id=uuid.uuid4(),
        challenge_date=_DAY,
        lessons_completed=1,
        words_learned=5,
    )

    first = await uow.users.claim_daily_challenge(user_id, challenge_date=_DAY)
    again = await uow.users.claim_daily_challenge(user_id, challenge_date=_DAY)

    assert first is True
    assert again is False
    day = await uow.users.get_daily_challenge(user_id, challenge_date=_DAY)
    assert day.claimed is True
    assert (day.lessons_completed_today, day.words_learned_today) == (1, 5)
