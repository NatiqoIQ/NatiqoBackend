"""Migration tests.

The rest of the suite builds its schema with ``create_all()``, which reads the models
directly. That is convenient and it is also blind: ``create_all()`` cannot tell you
that a migration is missing a column, that a constraint was never written into a
revision, or that ``downgrade`` does not reverse what ``upgrade`` built. A schema that
only exists in tests and not in any migration is a schema that vanishes on the first
``alembic upgrade`` in production.

So these tests run the real migration path against a throwaway file-backed SQLite
database, which is the one configuration where SQLite's batch mode and PostgreSQL's
DDL both work. Three things are asserted:

1. ``upgrade head`` produces a schema with no drift from the models. This is the
   regression guard that matters most — it fails the moment a model changes without a
   matching revision.
2. Constraints the application relies on are actually enforced by the *migrated*
   schema, not just by the ORM.
3. ``downgrade base`` reverses everything.

These are synchronous tests on purpose. ``env.py`` calls ``asyncio.run`` for online
migrations, so running them from inside a live event loop would raise.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from app.core.config import reset_settings_cache

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Every table the models declare. A downgrade that leaves one behind, or drops one
#: it should not, shows up here as a set difference rather than as a vague later error.
EXPECTED_TABLES = {
    "achievements",
    "ai_conversations",
    "ai_messages",
    "ai_usage_records",
    "audit_logs",
    "lesson_progress",
    "lessons",
    "levels",
    "media_assets",
    "questions",
    "quiz_answers",
    "quiz_sessions",
    "refresh_tokens",
    "review_cards",
    "review_logs",
    "user_daily_challenge_contributions",
    "user_daily_challenges",
    "unit_progress",
    "units",
    "user_achievements",
    "user_devices",
    "user_identities",
    "user_profiles",
    "user_settings",
    "user_stats",
    "users",
}


@pytest.fixture
def alembic_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    """Point Alembic at a throwaway file database for the duration of one test.

    A file rather than ``:memory:`` because Alembic opens and closes connections
    across the whole migration; an in-memory database would be discarded between them
    and the version table would vanish mid-upgrade.
    """
    db_path = tmp_path / "migration_test.db"
    dsn = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", dsn)
    # Settings are cached process-wide, so the new DSN only takes effect once cleared.
    reset_settings_cache()

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", dsn)

    yield config

    reset_settings_cache()


def _db_path(config: Config) -> str:
    """Filesystem path behind the configured SQLite URL.

    ``sqlite3.connect`` takes a path, not a URL, so the ``sqlite:///`` scheme has to
    come off. Handing it the full URL makes it try to create a file literally named
    ``sqlite:`` and fail with a misleading "unable to open database file".
    """
    url = config.get_main_option("sqlalchemy.url")
    assert url is not None
    return url.replace("sqlite+aiosqlite:///", "").replace("+aiosqlite", "")


def _tables(config: Config) -> set[str]:
    """Read table names straight from the migrated database, bypassing the ORM."""
    engine = create_engine(f"sqlite:///{_db_path(config)}")
    try:
        with engine.connect() as connection:
            rows = connection.exec_driver_sql("select name from sqlite_master where type='table'")
            return {row[0] for row in rows}
    finally:
        engine.dispose()


class TestMigrations:
    def test_upgrade_creates_every_table(self, alembic_config: Config) -> None:
        command.upgrade(alembic_config, "head")

        assert _tables(alembic_config) == EXPECTED_TABLES | {"alembic_version"}

    def test_migrated_schema_matches_the_models(self, alembic_config: Config) -> None:
        """The guard that keeps revisions honest.

        ``alembic check`` diffs the live database against ``Base.metadata`` and fails
        if a model change has no corresponding migration. Without this, a model can
        drift for weeks and the first person to find out is whoever deploys.
        """
        command.upgrade(alembic_config, "head")

        # Raises SystemExit / CommandError when differences exist.
        command.check(alembic_config)

    def test_migration_history_is_linear(self, alembic_config: Config) -> None:
        """A single head. Two heads mean two branches nobody has merged.

        The heads are read *before* upgrading, because ``upgrade head`` is the first
        thing to fail when there are two — with a message about the argument rather
        than about the revisions. Deployment hit exactly that: the container exited on
        startup instead of serving, and the cause was two heads nobody had joined.
        """
        script = ScriptDirectory.from_config(alembic_config)
        heads = script.get_heads()
        assert len(heads) == 1, f"multiple heads: {sorted(heads)}"

        command.upgrade(alembic_config, "head")

    def test_downgrade_removes_everything_it_created(self, alembic_config: Config) -> None:
        command.upgrade(alembic_config, "head")
        command.downgrade(alembic_config, "base")

        # Alembic keeps its own version table; everything else must be gone.
        assert _tables(alembic_config) == {"alembic_version"}

    def test_upgrade_is_repeatable_after_a_downgrade(self, alembic_config: Config) -> None:
        command.upgrade(alembic_config, "head")
        command.downgrade(alembic_config, "base")
        command.upgrade(alembic_config, "head")

        assert _tables(alembic_config) == EXPECTED_TABLES | {"alembic_version"}


class TestMigratedConstraints:
    """Constraints the application depends on, checked against migrated DDL.

    The integration suite proves the ORM-level behaviour. These prove the *database*
    would reject the same write even if a future refactor bypassed the ORM — which is
    exactly the guarantee a CHECK or UNIQUE constraint is supposed to provide.
    """

    @pytest.fixture
    def migrated(self, alembic_config: Config) -> Iterator[sqlite3.Connection]:
        command.upgrade(alembic_config, "head")
        connection = sqlite3.connect(_db_path(alembic_config))
        # The application enables this per connection; without it SQLite ignores the
        # foreign keys entirely and every cascade test would pass vacuously.
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
        connection.close()

    @staticmethod
    def _user(connection: sqlite3.Connection) -> str:
        import uuid

        user_id = str(uuid.uuid4())
        connection.execute(
            "insert into users (id, email, status, is_admin, created_at, updated_at)"
            " values (?, ?, 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (user_id, f"{user_id}@example.com"),
        )
        connection.commit()
        return user_id

    def test_duplicate_push_token_for_one_user_is_rejected(self, migrated) -> None:
        import uuid

        user_id = self._user(migrated)
        # SQLite raises at statement time, not at commit, so the guard wraps the whole
        # write rather than just the commit.
        with pytest.raises(sqlite3.IntegrityError):
            for _ in range(2):
                migrated.execute(
                    "insert into user_devices (id, user_id, platform, push_token,"
                    " created_at, updated_at) values (?, ?, 'android', 'tok-1',"
                    " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    (str(uuid.uuid4()), user_id),
                )
            migrated.commit()

    def test_same_push_token_under_two_users_is_allowed(self, migrated) -> None:
        """Documents the deliberate design choice.

        Uniqueness is per account, not per handset, so signing into a second account on
        a shared device is allowed. The trade-off — one physical token can belong to
        two accounts — is why notification delivery must be scoped to the recipient
        user id rather than to the token alone.
        """
        import uuid

        first, second = self._user(migrated), self._user(migrated)
        for user_id in (first, second):
            migrated.execute(
                "insert into user_devices (id, user_id, platform, push_token,"
                " created_at, updated_at) values (?, ?, 'ios', 'shared-tok',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (str(uuid.uuid4()), user_id),
            )
        migrated.commit()

    def test_status_check_constraint_rejects_unknown_state(self, migrated) -> None:
        import uuid

        user_id = str(uuid.uuid4())
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(
                "insert into users (id, email, status, is_admin, created_at, updated_at)"
                " values (?, ?, 'zombie', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (user_id, f"{user_id}@example.com"),
            )
            migrated.commit()

    def test_deleting_a_user_cascades_to_tokens_and_devices(self, migrated) -> None:
        import uuid

        user_id = self._user(migrated)
        device_id = str(uuid.uuid4())
        migrated.execute(
            "insert into user_devices (id, user_id, platform, created_at, updated_at)"
            " values (?, ?, 'android', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (device_id, user_id),
        )
        migrated.execute(
            "insert into refresh_tokens (id, user_id, jti_hash, family_id, expires_at,"
            " created_at, updated_at) values (?, ?, 'hash', 'fam', CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (str(uuid.uuid4()), user_id),
        )
        migrated.commit()

        migrated.execute("delete from users where id = ?", (user_id,))
        migrated.commit()

        # One literal statement instead of interpolating table names, so there is no
        # dynamically built SQL to reason about.
        survivors = migrated.execute(
            "select (select count(*) from user_devices where user_id = ?)"
            "     + (select count(*) from refresh_tokens where user_id = ?)",
            (user_id, user_id),
        ).fetchone()
        assert survivors is not None
        assert survivors[0] == 0, "child rows survived the cascade"

    def test_revealing_a_device_id_is_refused_by_the_foreign_key(self, migrated) -> None:
        """A refresh token cannot point at a device that does not exist."""
        import uuid

        user_id = self._user(migrated)
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(
                "insert into refresh_tokens (id, user_id, device_id, family_id,"
                " expires_at, created_at, updated_at) values (?, ?, ?, 'fam',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (str(uuid.uuid4()), user_id, str(uuid.uuid4())),
            )
            migrated.commit()


class TestMigratedContentConstraints:
    """The catalogue guards, checked against migrated DDL rather than ORM behaviour.

    Same reasoning as :class:`TestMigratedConstraints`, and it matters more here: the
    sibling-position ``UNIQUE`` is what makes "the third unit" mean one row forever, and
    a migration that dropped it would still let every read path and every unit test pass
    while quietly making manual data edits ambiguous.
    """

    @pytest.fixture
    def migrated(self, alembic_config: Config) -> Iterator[sqlite3.Connection]:
        command.upgrade(alembic_config, "head")
        connection = sqlite3.connect(_db_path(alembic_config))
        connection.execute("PRAGMA foreign_keys=ON")
        yield connection
        connection.close()

    @staticmethod
    def _level(connection: sqlite3.Connection, code: str = "A1", position: int = 1) -> str:
        import uuid

        level_id = str(uuid.uuid4())
        connection.execute(
            "insert into levels (id, code, title, position, state, created_at, updated_at)"
            " values (?, ?, 'Level', ?, 'published', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (level_id, code, position),
        )
        connection.commit()
        return level_id

    @staticmethod
    def _unit(
        connection: sqlite3.Connection, level_id: str, position: int = 1, state: str = "published"
    ) -> str:
        import uuid

        unit_id = str(uuid.uuid4())
        connection.execute(
            "insert into units (id, level_id, title, position, state, created_at, updated_at)"
            " values (?, ?, 'Unit', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (unit_id, level_id, position, state),
        )
        connection.commit()
        return unit_id

    @staticmethod
    def _lesson(
        connection: sqlite3.Connection, unit_id: str, position: int = 1, state: str = "published"
    ) -> str:
        import uuid

        lesson_id = str(uuid.uuid4())
        connection.execute(
            "insert into lessons (id, unit_id, title, summary, position, estimated_minutes,"
            " state, created_at, updated_at) values (?, ?, 'Lesson', 'Summary', ?, 10, ?,"
            " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (lesson_id, unit_id, position, state),
        )
        connection.commit()
        return lesson_id

    @staticmethod
    def _question(
        connection: sqlite3.Connection,
        lesson_id: str,
        position: int = 1,
        correct_index: int | None = 0,
    ) -> str:
        import uuid

        question_id = str(uuid.uuid4())
        connection.execute(
            "insert into questions (id, lesson_id, position, kind, skill, prompt, choices,"
            " accepted_answers, correct_index, points, created_at, updated_at)"
            " values (?, ?, ?, 'multiple_choice', 'vocabulary', 'Prompt', '[\"a\", \"b\"]',"
            " '[]', ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (question_id, lesson_id, position, correct_index),
        )
        connection.commit()
        return question_id

    def test_two_units_cannot_share_a_position(self, migrated) -> None:
        level_id = self._level(migrated)
        self._unit(migrated, level_id, position=1)
        with pytest.raises(sqlite3.IntegrityError):
            self._unit(migrated, level_id, position=1)
        migrated.commit()

    def test_the_same_position_under_different_levels_is_fine(self, migrated) -> None:
        """Positions are scoped to a parent, not global."""
        first = self._level(migrated, code="A1", position=1)
        second = self._level(migrated, code="A2", position=1)
        self._unit(migrated, first, position=1)
        self._unit(migrated, second, position=1)

        count = migrated.execute("select count(*) from units").fetchone()
        assert count is not None
        assert count[0] == 2

    def test_two_lessons_cannot_share_a_position(self, migrated) -> None:
        unit_id = self._unit(migrated, self._level(migrated))
        self._lesson(migrated, unit_id, position=1)
        with pytest.raises(sqlite3.IntegrityError):
            self._lesson(migrated, unit_id, position=1)
        migrated.commit()

    def test_two_questions_cannot_share_a_position(self, migrated) -> None:
        lesson_id = self._lesson(migrated, self._unit(migrated, self._level(migrated)))
        self._question(migrated, lesson_id, position=1)
        with pytest.raises(sqlite3.IntegrityError):
            self._question(migrated, lesson_id, position=1)
        migrated.commit()

    def test_an_unknown_content_state_is_rejected(self, migrated) -> None:
        """``nearly_ready`` is not a state. The column is text, so only a CHECK keeps
        the lifecycle from sprouting informal values that no query filters on."""
        import uuid

        level_id = self._level(migrated)
        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(
                "insert into units (id, level_id, title, position, state, created_at,"
                " updated_at) values (?, ?, 'Unit', 9, 'nearly_ready',"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (str(uuid.uuid4()), level_id),
            )
            migrated.commit()

    def test_position_zero_is_rejected(self, migrated) -> None:
        """Ordering starts at 1, so there is no such thing as position 0."""
        import uuid

        with pytest.raises(sqlite3.IntegrityError):
            migrated.execute(
                "insert into levels (id, code, title, position, state, created_at, updated_at)"
                " values (?, 'B1', 'Level', 0, 'published', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (str(uuid.uuid4()),),
            )
            migrated.commit()

    def test_a_fill_blank_question_may_omit_the_correct_index(self, migrated) -> None:
        """``NULL`` is the documented marker for free-text items, not a missing value."""
        lesson_id = self._lesson(migrated, self._unit(migrated, self._level(migrated)))
        self._question(migrated, lesson_id, correct_index=None)

        row = migrated.execute(
            "select correct_index from questions where lesson_id = ?", (lesson_id,)
        ).fetchone()
        assert row is not None
        assert row[0] is None

    def test_deleting_a_level_cascades_through_the_whole_curriculum(self, migrated) -> None:
        level_id = self._level(migrated)
        unit_id = self._unit(migrated, level_id)
        lesson_id = self._lesson(migrated, unit_id)
        self._question(migrated, lesson_id)

        migrated.execute("delete from levels where id = ?", (level_id,))
        migrated.commit()

        survivors = migrated.execute(
            "select (select count(*) from units where id = ?)"
            "     + (select count(*) from lessons where id = ?)"
            "     + (select count(*) from questions where lesson_id = ?)",
            (unit_id, lesson_id, lesson_id),
        ).fetchone()
        assert survivors is not None
        assert survivors[0] == 0, "content below the deleted level survived the cascade"


class TestCheckpointBackfill:
    """The grandfather rule, exercised against a database with real rows in it.

    ``upgrade head`` on an empty database runs the backfill's SQL exactly zero times, so
    every other test here only proves the statement parses — not that it decides
    anything. These tests stop one revision short, seed the situation the backfill exists
    for (learners who finished units before checkpoints existed), then finish the
    upgrade and read what it wrote.

    The verdict the tests pin down: a ``(user, unit)`` pair is grandfathered exactly when
    every *published* lesson of that unit has a ``completed`` row for that user. Draft
    lessons do not count towards the bar — completing one while its published sibling is
    outstanding leaves the unit locked.
    """

    @staticmethod
    def _user(connection: sqlite3.Connection, tag: str) -> str:
        import uuid

        user_id = str(uuid.uuid4())
        # Seeded at the revision below, where accounts still carry a role list; the
        # roles-to-is_admin migration runs later in the upgrade and has to cope with it.
        connection.execute(
            "insert into users (id, email, status, roles, created_at, updated_at)"
            " values (?, ?, 'active', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (user_id, f"{tag}@example.com"),
        )
        return user_id

    @staticmethod
    def _curriculum(connection: sqlite3.Connection) -> dict[str, str]:
        """One level, two units.

        Unit 1 holds two published lessons; unit 2 holds one published and one draft —
        the pair of edges the backfill's filter has to tell apart.
        """
        import uuid

        ids = {name: str(uuid.uuid4()) for name in ("u1", "u2", "l1", "l2", "l3", "l4")}
        connection.execute(
            "insert into levels (id, code, title, position, state, created_at, updated_at)"
            " values (?, 'A1', 'Level', 1, 'published', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (str(uuid.uuid4()),),
        )
        level_id = connection.execute("select id from levels").fetchone()
        assert level_id is not None
        for tag, unit_key in (("u1", "u1"), ("u2", "u2")):
            connection.execute(
                "insert into units (id, level_id, title, position, state, created_at,"
                " updated_at) values (?, ?, ?, ?, 'published', CURRENT_TIMESTAMP,"
                " CURRENT_TIMESTAMP)",
                (ids[unit_key], level_id[0], tag, int(unit_key[1])),
            )
        for lesson_key, unit_key, position, state in (
            ("l1", "u1", 1, "published"),
            ("l2", "u1", 2, "published"),
            ("l3", "u2", 1, "published"),
            ("l4", "u2", 2, "draft"),
        ):
            connection.execute(
                "insert into lessons (id, unit_id, title, summary, position,"
                " estimated_minutes, state, created_at, updated_at) values (?, ?, ?,"
                " 'Summary', ?, 10, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (ids[lesson_key], ids[unit_key], lesson_key, position, state),
            )
        return ids

    @staticmethod
    def _completed(connection: sqlite3.Connection, user_id: str, lesson_id: str) -> None:
        import uuid

        connection.execute(
            "insert into lesson_progress (id, user_id, lesson_id, state, best_score,"
            " attempts, xp_earned, created_at, updated_at) values (?, ?, ?, 'completed',"
            " 100, 1, 10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (str(uuid.uuid4()), user_id, lesson_id),
        )

    @pytest.fixture
    def seeded_then_upgraded(self, alembic_config: Config) -> Iterator[sqlite3.Connection]:
        command.upgrade(alembic_config, "f6c35e9b1a84")
        connection = sqlite3.connect(_db_path(alembic_config))
        connection.execute("PRAGMA foreign_keys=ON")
        ids = self._curriculum(connection)
        finished = self._user(connection, "finished")
        partial = self._user(connection, "partial")
        # "finished" completed every lesson in both units, drafts included.
        for lesson_key in ("l1", "l2", "l3", "l4"):
            self._completed(connection, finished, ids[lesson_key])
        # "partial" finished unit 1, then touched only unit 2's *draft* lesson — the
        # exact edge the filter exists for: some progress in the unit, even a completed
        # row, but the published lesson is still outstanding.
        for lesson_key in ("l1", "l2", "l4"):
            self._completed(connection, partial, ids[lesson_key])
        connection.commit()

        command.upgrade(alembic_config, "head")
        yield connection
        connection.close()

    def test_every_lesson_finished_earns_a_grandfathered_pass(
        self, seeded_then_upgraded: sqlite3.Connection
    ) -> None:
        rows = seeded_then_upgraded.execute(
            "select user_id, unit_id, best_score, attempts, passed_at from unit_progress"
        ).fetchall()

        # Three pairs: "finished" for both units, "partial" for unit 1 only. Unit 2 stays
        # open for "partial" — its published lesson is unfinished, no matter that the
        # draft lesson next to it has a completed row.
        assert len(rows) == 3, f"unexpected grandfathering: {rows}"

        for _, _, best_score, attempts, passed_at in rows:
            assert best_score == 0, "nobody scored these units; the score must stay honest"
            assert attempts == 0
            assert passed_at is not None, "without passed_at the unit would stay locked"

    def test_a_unit_short_of_one_published_lesson_stays_locked(
        self, seeded_then_upgraded: sqlite3.Connection
    ) -> None:
        rows = seeded_then_upgraded.execute(
            "select count(*) from unit_progress up"
            " join users u on u.id = up.user_id"
            " where u.email = 'partial@example.com'"
            " and up.unit_id = (select id from units where position = 2)"
        ).fetchone()
        assert rows is not None
        assert rows[0] == 0, "a unit with an unfinished published lesson was grandfathered"

    def test_the_migrated_mode_check_admits_checkpoint_and_nothing_new(
        self, seeded_then_upgraded: sqlite3.Connection
    ) -> None:
        """The widened CHECK is the whole point of the batch ALTER, so read it back.

        Admitting ``checkpoint`` is the positive half; rejecting a value that was never
        in the vocabulary is the negative half — a constraint that accepts everything is
        indistinguishable from no constraint until someone relies on it.
        """
        import uuid

        connection = seeded_then_upgraded
        user_id = connection.execute("select id from users limit 1").fetchone()
        unit_id = connection.execute("select id from units limit 1").fetchone()
        assert user_id is not None and unit_id is not None

        def insert(mode: str) -> None:
            connection.execute(
                "insert into quiz_sessions (id, user_id, mode, unit_id, status,"
                " question_ids, correct_count, xp_awarded, started_at, expires_at,"
                " created_at, updated_at) values (?, ?, ?, ?, 'active', '[]', 0, 0,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,"
                " CURRENT_TIMESTAMP)",
                (str(uuid.uuid4()), user_id[0], mode, unit_id[0]),
            )

        insert("checkpoint")
        with pytest.raises(sqlite3.IntegrityError):
            insert("speedrun")
        connection.rollback()


def test_alembic_ini_hardcodes_no_credentials() -> None:
    """A committed DSN means a committed password.

    ``env.py`` reads the real database from application settings precisely so that this
    file can stay empty; a future edit that pastes a URL in here would leak it.
    """
    contents = (BACKEND_ROOT / "alembic.ini").read_text(encoding="utf-8")
    url_lines = [
        line for line in contents.splitlines() if line.strip().startswith("sqlalchemy.url")
    ]
    assert url_lines, "sqlalchemy.url must still be declared"
    for line in url_lines:
        _, _, value = line.partition("=")
        assert value.strip() == "", f"alembic.ini must not carry a DSN: {line!r}"

    # Scan values only. The comments in this file legitimately *describe* the secrets
    # policy, so scanning raw text would match its own explanation.
    values = "\n".join(
        line for line in contents.splitlines() if not line.strip().startswith((";", "#"))
    ).lower()
    for marker in ("password", "postgres://", "postgresql://", "secret"):
        assert marker not in values, f"alembic.ini appears to carry a {marker!r}"


def test_env_py_imports_every_model_module() -> None:
    """Guards the failure mode that autogenerate cannot report.

    An unimported model is invisible to ``Base.metadata``, and autogenerate responds by
    emitting a ``DROP TABLE`` for it — a green, successful, destructive migration. The
    registry exists so the list of model modules is explicit and reviewable; this test
    makes sure ``env.py`` keeps using it rather than importing models ad hoc.
    """
    env_source = (BACKEND_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")

    assert "import_all_models()" in env_source
    # Models must be imported *before* target_metadata is captured.
    assert env_source.index("import_all_models()") < env_source.index("target_metadata =")


def test_registry_covers_every_model_module_on_disk() -> None:
    """The registry is the contract; this is what keeps it honest.

    A new ``models.py`` that nobody adds to ``MODEL_MODULES`` is invisible to both
    ``create_all()`` and Alembic, and neither fails loudly. Comparing the registry
    against the filesystem is the only cheap way to catch it before a deploy does.
    """
    from app.core.registry import MODEL_MODULES

    # app/modules/<module>/infrastructure/models.py -> app.modules.<module>.infrastructure.models
    on_disk = {
        f"app.modules.{path.parent.parent.name}.{path.parent.name}.{path.stem}"
        for path in (BACKEND_ROOT / "app" / "modules").rglob("models.py")
    }

    assert on_disk, "no model modules found — has the layout changed?"
    assert on_disk <= set(MODEL_MODULES), (
        f"models.py files missing from MODEL_MODULES: {sorted(on_disk - set(MODEL_MODULES))}"
    )


def test_migration_files_are_never_shadowed_by_a_stale_env() -> None:
    """``os.environ`` must not win over the explicit URL these tests set.

    A developer shell with ``DATABASE_URL`` exported would otherwise point every
    migration test at whatever that is — the exact failure the throwaway-file fixture
    exists to make impossible.

    The suite may itself be pointed at PostgreSQL through ``TEST_DATABASE_URL``, so the
    check is equality with the *resolved* test DSN rather than a hard-coded SQLite
    prefix — still the suite deciding, never the shell.
    """
    expected = os.environ.get("TEST_DATABASE_URL") or "sqlite+aiosqlite:///:memory:"
    assert os.environ["DATABASE_URL"] == expected


#: The indexes the dashboard's aggregate reads rely on, table by table.
#:
#: Listed by hand rather than derived from the models on purpose: this is the check that a
#: revision actually *creates* them. Deriving the list from the metadata would assert that
#: the models agree with themselves, which ``test_migrated_schema_matches_the_models``
#: already covers through ``alembic check``.
ANALYTICS_INDEXES: dict[str, set[str]] = {
    "users": {"ix_users_created_at"},
    "user_identities": {"ix_identity_last_login"},
    "quiz_sessions": {
        "ix_quiz_sessions_completed_at",
        "ix_quiz_sessions_status_completed_at",
    },
    "quiz_answers": {"ix_quiz_answers_question"},
    "lesson_progress": {"ix_lesson_progress_lesson"},
    "audit_logs": {"ix_audit_logs_created_at"},
    "ai_usage_records": {"ix_ai_usage_created_at"},
    "media_assets": {"ix_media_assets_created_at"},
}


def test_the_analytics_indexes_exist_in_the_migrated_schema(
    alembic_config: Config,
) -> None:
    """An index that only exists in the models costs a full scan in production.

    The dashboard's screens are aggregates over tables that grow without bound, so a missing
    index is not a slow query — it is a query that degrades every week. Reading
    ``sqlite_master`` after a real ``upgrade head`` is the only check that proves the
    revision created them, rather than a developer's local ``create_all`` doing it.
    """
    command.upgrade(alembic_config, "head")

    connection = sqlite3.connect(_db_path(alembic_config))
    try:
        for table, expected in ANALYTICS_INDEXES.items():
            rows = connection.execute(
                "select name from sqlite_master where type = 'index' and tbl_name = ?",
                (table,),
            ).fetchall()
            present = {row[0] for row in rows}
            assert expected <= present, f"{table} is missing {sorted(expected - present)}"
    finally:
        connection.close()


def test_the_analytics_indexes_are_dropped_by_a_downgrade(alembic_config: Config) -> None:
    """A downgrade that leaves indexes behind makes the next upgrade fail on a duplicate."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "d4c9a17b6e02")

    connection = sqlite3.connect(_db_path(alembic_config))
    try:
        for table, dropped in ANALYTICS_INDEXES.items():
            rows = connection.execute(
                "select name from sqlite_master where type = 'index' and tbl_name = ?",
                (table,),
            ).fetchall()
            present = {row[0] for row in rows}
            assert not (dropped & present), f"{table} kept {sorted(dropped & present)}"
    finally:
        connection.close()


class TestDailyChallengeLedgerMigration:
    """The *data* half of the ledger revision, not just its DDL.

    The empty-database tests above would pass even if the backfill silently did nothing,
    and the old counters are the only surviving record of a learner's past days. These
    tests migrate a database that already holds such a day, in both directions.
    """

    @pytest.fixture
    def legacy_day(self, alembic_config: Config) -> Iterator[tuple[Config, uuid.UUID, str]]:
        """A learner with one day of counters under the pre-ledger schema."""
        day = "2026-10-20"
        command.upgrade(alembic_config, "b9e4d2f7c1a5")

        user_id = uuid.uuid4()
        connection = sqlite3.connect(_db_path(alembic_config))
        try:
            connection.execute(
                "insert into users (id, email, status, is_admin, created_at, updated_at)"
                " values (?, ?, 'active', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (user_id.hex, f"{user_id.hex}@example.com"),
            )
            connection.execute(
                "insert into user_daily_challenges (id, user_id, challenge_date,"
                " lessons_completed_today, words_learned_today, claimed,"
                " created_at, updated_at) values (?, ?, ?, 2, 6, 1,"
                " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (uuid.uuid4().hex, user_id.hex, day),
            )
            connection.commit()
        finally:
            connection.close()

        yield alembic_config, user_id, day

    @staticmethod
    def _rows(
        config: Config, sql: str, params: tuple[object, ...] = ()
    ) -> list[tuple[object, ...]]:
        connection = sqlite3.connect(_db_path(config))
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def test_upgrade_turns_each_legacy_day_into_one_contribution(
        self, legacy_day: tuple[Config, uuid.UUID, str]
    ) -> None:
        config, user_id, day = legacy_day

        command.upgrade(config, "head")

        rows = self._rows(
            config,
            "select user_id, challenge_date, lessons_completed, words_learned"
            " from user_daily_challenge_contributions",
        )
        assert rows == [(user_id.hex, day, 2, 6)]
        # The counters are gone, because the totals are now derived from that row.
        columns = {
            row[1] for row in self._rows(config, "pragma table_info('user_daily_challenges')")
        }
        assert "lessons_completed_today" not in columns
        assert "words_learned_today" not in columns

    def test_downgrade_folds_the_ledger_back_into_the_counters(
        self, legacy_day: tuple[Config, uuid.UUID, str]
    ) -> None:
        config, user_id, day = legacy_day
        command.upgrade(config, "head")

        command.downgrade(config, "b9e4d2f7c1a5")

        rows = self._rows(
            config,
            "select user_id, challenge_date, lessons_completed_today,"
            " words_learned_today, claimed from user_daily_challenges",
        )
        assert rows == [(user_id.hex, day, 2, 6, 1)]

    def test_downgrade_keeps_progress_from_a_day_that_was_never_claimed(
        self, legacy_day: tuple[Config, uuid.UUID, str]
    ) -> None:
        """Under the ledger a day needs no row until it is claimed, so one is rebuilt."""
        config, user_id, _ = legacy_day
        command.upgrade(config, "head")
        connection = sqlite3.connect(_db_path(config))
        try:
            connection.execute(
                "insert into user_daily_challenge_contributions (id, user_id, session_id,"
                " challenge_date, lessons_completed, words_learned, created_at, updated_at)"
                " values (?, ?, ?, '2026-10-21', 1, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (uuid.uuid4().hex, user_id.hex, uuid.uuid4().hex),
            )
            connection.commit()
        finally:
            connection.close()

        command.downgrade(config, "b9e4d2f7c1a5")

        rows = self._rows(
            config,
            "select challenge_date, lessons_completed_today, words_learned_today, claimed"
            " from user_daily_challenges where challenge_date = '2026-10-21'",
        )
        assert rows == [("2026-10-21", 1, 3, 0)]
