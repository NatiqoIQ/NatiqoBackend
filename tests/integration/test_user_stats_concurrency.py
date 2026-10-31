"""The stats row's insert and lock, against a real schema.

The lock itself only means something on PostgreSQL, but the insert contract and the
row's uniqueness are schema facts: a second ``add`` must report that it lost instead of
raising, so the completion path can add its delta to the row that already exists.
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
from app.modules.gamification.domain.entities import UserStats
from app.modules.gamification.infrastructure.models import UserStatsModel
from app.modules.users.infrastructure.models import UserModel

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest.fixture
def session(uow: SqlAlchemyUnitOfWork) -> AsyncSession:
    return uow.session


async def _user(session: AsyncSession) -> UserId:
    row = UserModel(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", status="active")
    session.add(row)
    await session.flush()
    return UserId(row.id)


@pytest.mark.asyncio
async def test_a_second_insert_reports_the_loss_instead_of_raising(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    user_id = await _user(uow.session)

    assert await uow.user_stats.add(UserStats(user_id=user_id, xp_total=10)) is True
    assert await uow.user_stats.add(UserStats(user_id=user_id, xp_total=999)) is False

    await uow.commit()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as fresh:
        stored = await fresh.session.scalar(
            select(UserStatsModel).where(UserStatsModel.user_id == user_id)
        )
        assert stored is not None
        assert stored.xp_total == 10, "the first writer's row is untouched"
        locked = await fresh.user_stats.get_for_update(user_id)
        assert locked is not None
        assert locked.xp_total == 10
