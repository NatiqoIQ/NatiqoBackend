"""Read-replica wiring tests.

The replica is optional and must behave exactly like today when it is absent. These
tests pin the two failure modes that matter: a replica URL that is not normalised to
asyncpg, and a second pool opened against the same database under a different name.
"""

from __future__ import annotations

from app.core import database
from app.core.config import Settings


def build(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestReadDsn:
    def test_absent_replica_falls_back_to_the_primary(self) -> None:
        settings = build(database_url="postgresql+asyncpg://u:p@db:5432/natiqo")
        assert settings.read_sqlalchemy_dsn == settings.sqlalchemy_dsn

    def test_plain_postgres_url_is_normalised(self) -> None:
        settings = build(database_read_url="postgresql://u:p@replica:5432/natiqo")
        assert settings.read_sqlalchemy_dsn == "postgresql+asyncpg://u:p@replica:5432/natiqo"

    def test_short_postgres_url_is_normalised(self) -> None:
        settings = build(database_read_url="postgres://u:p@replica:5432/natiqo")
        assert settings.read_sqlalchemy_dsn == "postgresql+asyncpg://u:p@replica:5432/natiqo"

    def test_replica_does_not_change_the_primary_dsn(self) -> None:
        settings = build(
            database_url="postgresql+asyncpg://primary:5432/natiqo",
            database_read_url="postgresql+asyncpg://replica:5432/natiqo",
        )
        assert settings.sqlalchemy_dsn == "postgresql+asyncpg://primary:5432/natiqo"


class TestEngineSelection:
    async def test_no_replica_shares_the_primary_engine(self, monkeypatch) -> None:
        settings = build(database_url="sqlite+aiosqlite:///:memory:")
        monkeypatch.setattr(database, "get_settings", lambda: settings)
        monkeypatch.setattr(database, "_engine", None)
        monkeypatch.setattr(database, "_read_engine", None)
        assert database.get_read_engine() is database.get_engine()
        assert database.get_read_session_factory() is database.get_session_factory()
        await database.dispose_engine()

    async def test_replica_with_the_same_dsn_shares_the_primary_engine(self, monkeypatch) -> None:
        """Two names for one database must not open two pools."""
        settings = build(
            database_url="sqlite+aiosqlite:///:memory:",
            database_read_url="sqlite+aiosqlite:///:memory:",
        )
        monkeypatch.setattr(database, "get_settings", lambda: settings)
        monkeypatch.setattr(database, "_engine", None)
        monkeypatch.setattr(database, "_read_engine", None)
        assert database.get_read_engine() is database.get_engine()
        await database.dispose_engine()

    async def test_a_distinct_replica_gets_its_own_engine(self, monkeypatch) -> None:
        settings = build(
            database_url="sqlite+aiosqlite:///:memory:",
            database_read_url="sqlite+aiosqlite:///./replica-not-opened.db",
        )
        monkeypatch.setattr(database, "get_settings", lambda: settings)
        monkeypatch.setattr(database, "_engine", None)
        monkeypatch.setattr(database, "_read_engine", None)
        assert database.get_read_engine() is not database.get_engine()
        await database.dispose_engine()
        assert database._read_engine is None and database._engine is None
