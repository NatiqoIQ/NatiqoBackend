"""Feature-flag HTTP surface (F24).

The public read answers for the caller and hides internal flags; the admin write is the
only way a flag comes into existence, and it is audited. Both are exercised against the
real app rather than a service double, because the wiring (auth, cache invalidation,
camelCase contract) is where this feature can break.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.infrastructure.models import AuditLogModel
from tests.api.test_endpoints import (
    _grant_admin,
    auth_headers,
    client as _endpoint_client,  # noqa: F401 - a fixture
    content_db as _endpoint_db,  # noqa: F401 - a fixture
    sign_in,
)


@pytest_asyncio.fixture
async def client(_endpoint_client: AsyncClient) -> AsyncClient:  # noqa: F811
    """Re-export the endpoint suite's app client under this module's name."""
    return _endpoint_client


@pytest_asyncio.fixture
async def content_db(_endpoint_db: AsyncSession) -> AsyncSession:  # noqa: F811
    """Re-export the seeding session, so both modules share one engine and one schema."""
    return _endpoint_db


@pytest.mark.asyncio
async def test_a_missing_flag_is_absent_from_the_public_read(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    account = await sign_in(client, token="flags-learner-00001")
    response = await client.get(
        "/api/v1/flags", headers=auth_headers(account["tokens"]["accessToken"])
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"flags": {}}


@pytest.mark.asyncio
async def test_an_admin_publishes_a_flag_and_a_learner_sees_it(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    admin = await _grant_admin(client, content_db, token="flags-admin-000001")
    created = await client.put(
        "/api/v1/admin/flags/beta_ui",
        headers=admin,
        json={
            "enabled": True,
            "rollout_percentage": 100,
            "public": True,
            "description": "New lesson player",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["key"] == "beta_ui"
    assert body["rolloutPercentage"] == 100
    assert body["public"] is True

    learner = await sign_in(client, token="flags-learner-two")
    read = await client.get("/api/v1/flags", headers=auth_headers(learner["tokens"]["accessToken"]))
    assert read.status_code == 200
    assert read.json() == {"flags": {"beta_ui": True}}


@pytest.mark.asyncio
async def test_an_internal_flag_never_reaches_the_public_read(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    admin = await _grant_admin(client, content_db, token="flags-admin-three")
    response = await client.put(
        "/api/v1/admin/flags/ai_kill_switch",
        headers=admin,
        json={"enabled": True, "rollout_percentage": 100, "public": False},
    )
    assert response.status_code == 200

    learner = await sign_in(client, token="flags-learner-three")
    read = await client.get("/api/v1/flags", headers=auth_headers(learner["tokens"]["accessToken"]))
    assert read.json() == {"flags": {}}

    listed = await client.get("/api/v1/admin/flags", headers=admin)
    assert listed.status_code == 200
    assert [item["key"] for item in listed.json()] == ["ai_kill_switch"]


@pytest.mark.asyncio
async def test_a_non_admin_cannot_write_a_flag(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    account = await sign_in(client, token="flags-not-admin-0001")
    response = await client.put(
        "/api/v1/admin/flags/beta_ui",
        headers=auth_headers(account["tokens"]["accessToken"]),
        json={"enabled": True, "rollout_percentage": 100, "public": True},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_ADMIN_REQUIRED"


@pytest.mark.asyncio
async def test_a_rollout_outside_0_100_is_rejected(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    admin = await _grant_admin(client, content_db, token="flags-admin-four")
    response = await client.put(
        "/api/v1/admin/flags/beta_ui",
        headers=admin,
        json={"enabled": True, "rollout_percentage": 150, "public": True},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_flag_write_leaves_an_audit_row(
    client: AsyncClient, content_db: AsyncSession
) -> None:
    admin = await _grant_admin(client, content_db, token="flags-admin-five")
    await client.put(
        "/api/v1/admin/flags/beta_ui",
        headers=admin,
        json={"enabled": True, "rollout_percentage": 100, "public": True},
    )
    content_db.expunge_all()
    actions = (
        (
            await content_db.execute(
                select(AuditLogModel.action).where(AuditLogModel.action == "flags.update")
            )
        )
        .scalars()
        .all()
    )
    assert len(actions) == 1
