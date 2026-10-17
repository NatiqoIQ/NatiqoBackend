"""Shared pytest fixtures.

Three isolation rules are enforced here rather than left to each test:

1. **The composition root is reset between tests.** Adapters are ``lru_cache``d, so
   a cached Redis client from one test would otherwise leak into the next.
2. **Settings are pinned to testing.** No test can accidentally read a developer's
   ``.env`` and, for example, point at a real database.
3. **The database is chosen by the suite, not by the shell.** The default is an
   in-memory SQLite per test — no server, no migration, no chance of touching a real
   one. Exporting ``TEST_DATABASE_URL`` runs the same suite against a real PostgreSQL
   server instead, which is what CI does: SQLite differs from production exactly where
   this suite would otherwise be blind (``JSONB`` vs ``JSON``, ``TIMESTAMPTZ``, native
   ``UUID``, ``ON CONFLICT``, text length limits). Whatever the engine, the schema is
   rebuilt before every test, because "the database was empty" is a property one
   engine gives away for free and the other does not.

The fixtures below are ``autouse``. A fixture that a test has to remember to request is
a fixture some test will forget, and the failure it causes — a suite that quietly reuses
one engine, or writes to a developer's database — is expensive to diagnose later.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

#: Opt-in escape hatch from the SQLite default, for CI and for a developer who wants to
#: reproduce a dialect-specific failure locally::
#:
#:     TEST_DATABASE_URL=postgresql+asyncpg://natiqo:natiqo@127.0.0.1:5432/natiqo_test uv run pytest
#:
#: Point it at a *dedicated* database. Every test drops and rebuilds the schema, so it
#: would take a real one apart.
_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///:memory:"

# Assigned, not `setdefault`: a developer's shell may already export APP_ENV, and the
# whole point of this block is that the value is ours. `setdefault` would defer to
# whatever happened to be in the environment, which is how a test run ends up
# pointing at a real database.
_TEST_ENV = {
    "APP_ENV": "testing",
    "AI_ENABLED": "false",
    "AI_PROVIDER": "none",
    "RATE_LIMIT_ENABLED": "false",
    "QUEUE_ENABLED": "false",
    "LOG_FORMAT": "console",
    "LOG_LEVEL": "WARNING",
    # A developer's .env may enable the development sign-in; the suite decides for
    # itself, per test, whether the route exists — see the ``dev_client`` fixture.
    "DEV_LOGIN_ENABLED": "false",
    # An in-memory database per test by default: no server, no schema migration, no
    # chance of touching a real one. A real DSN arrives only through TEST_DATABASE_URL.
    "DATABASE_URL": _TEST_DATABASE_URL,
}
os.environ.update(_TEST_ENV)

from app.api.deps import reset_composition_root  # noqa: E402
from app.core.config import get_settings, reset_settings_cache  # noqa: E402
from app.core.database import (  # noqa: E402
    create_all,
    dispose_engine,
    drop_all,
    get_session_factory,
)
from app.core.events import reset_event_bus  # noqa: E402
from app.core.registry import import_all_models  # noqa: E402
from app.modules.ai.infrastructure.usage import clear_buffer  # noqa: E402


def _uses_sqlite() -> bool:
    """True when the suite is running on the disposable in-memory default."""
    return get_settings().sqlalchemy_dsn.startswith("sqlite")


@pytest.fixture(autouse=True)
def _clean_process_state() -> Iterator[None]:
    """Guarantee every test starts and ends with no cached global state."""
    reset_settings_cache()
    reset_composition_root()
    reset_event_bus()
    clear_buffer()
    yield
    reset_settings_cache()
    reset_composition_root()
    reset_event_bus()
    clear_buffer()


@pytest.fixture(autouse=True)
async def _empty_schema() -> AsyncIterator[None]:
    """Rebuild the schema before every test that does not get one for free.

    In-memory SQLite is already empty: the database lives inside the connection and
    dies with it. PostgreSQL keeps everything, so without this the rows a test wrote
    are still there for the next one — and a test that passes because it read another
    test's fixture data is worse than a test that fails, because nothing reports it.

    ``drop_all`` before ``create_all`` rather than the reverse: ``checkfirst`` makes
    both idempotent, so the first test on a virgin database pays nothing extra.
    """
    import_all_models()
    if not _uses_sqlite():
        await drop_all()
    await create_all()
    yield


@pytest.fixture(autouse=True)
async def disposed_engine() -> AsyncIterator[None]:
    """Disposes the async engine after the test so no connection outlives it.

    Autouse rather than opt-in: the engine is a process-wide singleton, so a test
    that leaves it open hands its pooled connections to the next test regardless of
    whether that test knows an engine exists.
    """
    yield
    await dispose_engine()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def event_bus():
    from app.core.events import get_event_bus

    return get_event_bus()


@pytest_asyncio.fixture
async def session():
    """A session against a freshly created schema.

    The schema is created and dropped per test rather than once per session: a suite
    that creates data must not depend on test ordering, and a leftover row from a
    failing test would otherwise surface as an unrelated failure ten tests later.
    """
    import_all_models()
    await create_all()
    factory = get_session_factory()
    async with factory() as open_session:
        yield open_session
    await dispose_engine()
