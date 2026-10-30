"""Media is served cacheable, unlike every other response.

The default ``no-store`` is right for profiles and tokens and wrong for lesson audio:
it made the phone re-download the same immutable MP3 on every launch, and put that
traffic on the API instead of the CDN. These tests pin the exception and, just as
importantly, that it does not extend to a missing file.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_an_existing_media_file_is_publicly_cacheable() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        response = await client.get("/media/sources.json")

    assert response.status_code == 200, response.text
    cache_control = response.headers["cache-control"]
    assert cache_control.startswith("public")
    assert "max-age=604800" in cache_control
    # The CDN (or a webview on another origin) must be allowed to embed it.
    assert response.headers["cross-origin-resource-policy"] == "cross-origin"


@pytest.mark.asyncio
async def test_a_missing_media_file_is_never_cached() -> None:
    """A week-long 404 would survive the upload that fixes it."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        response = await client.get("/media/does-not-exist.png")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
