"""The Prometheus endpoint, over the real ASGI stack.

What only an HTTP test can prove: the route is mounted outside the version prefix, the
metrics middleware actually observes a request that went through the whole stack, and
disabling the endpoint removes it rather than publishing an empty document.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import reset_settings_cache
from app.core.metrics import reset_metrics
from app.main import app


@pytest.fixture(autouse=True)
def _clean_metrics() -> AsyncIterator[None]:
    """Counters are process-global; a previous test's traffic must not leak in."""
    reset_metrics()
    yield
    reset_metrics()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as http:
        yield http


@pytest.mark.asyncio
async def test_metrics_reports_the_request_it_just_served(client: AsyncClient) -> None:
    await client.get("/")

    response = await client.get("/metrics")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "natiqo_http_requests_total" in body
    # The route *template* is the label, not the concrete path: bounded cardinality.
    assert 'route="/"' in body
    assert 'status="200"' in body
    assert "natiqo_http_request_duration_seconds_bucket" in body
    assert "natiqo_requests_in_flight" in body


@pytest.mark.asyncio
async def test_metrics_is_not_itself_counted_as_traffic(client: AsyncClient) -> None:
    """A scrape every 15s must not dominate the request rate it is meant to measure."""
    await client.get("/metrics")

    body = (await client.get("/metrics")).text

    assert 'route="/metrics"' not in body


@pytest.mark.asyncio
async def test_metrics_can_be_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("METRICS_ENABLED", "false")
    reset_settings_cache()

    response = await client.get("/metrics")

    assert response.status_code == 404
