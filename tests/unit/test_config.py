"""Settings loading tests.

These exist because the `.env.example` file and the settings parser disagreed about
the format of ``CORS_ORIGINS``, and nothing noticed until a test happened to read
the file. The value was documented in the form the parser rejected, so the failure
was invisible to anyone who set the variable in their shell and invisible to CI on a
machine without a ``.env``.

The general rule these encode: **a documented configuration value must be loadable.**
A parser that accepts only a format nobody writes down is not a tested feature.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def build(**overrides: object) -> Settings:
    """Build settings directly, bypassing env and ``.env`` entirely.

    Constructing the model keeps these tests independent of the developer's own
    ``.env``, which is the file that hid this bug in the first place.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestCorsOrigins:
    def test_comma_separated_form_loads(self) -> None:
        """The form written in ``.env.example`` must actually work."""
        settings = build(
            cors_origins="http://localhost:3000,http://localhost:8080,http://localhost:8000"
        )
        assert settings.cors_origins == [
            "http://localhost:3000",
            "http://localhost:8080",
            "http://localhost:8000",
        ]

    def test_json_array_form_still_loads(self) -> None:
        """JSON is the only way to express an origin containing a comma."""
        settings = build(cors_origins='["https://a.test","https://b.test"]')
        assert settings.cors_origins == ["https://a.test", "https://b.test"]

    def test_surrounding_whitespace_and_empty_entries_are_dropped(self) -> None:
        settings = build(cors_origins=" http://a.test , ,http://b.test ,")
        assert settings.cors_origins == ["http://a.test", "http://b.test"]

    def test_single_origin_needs_no_delimiter(self) -> None:
        assert build(cors_origins="http://only.test").cors_origins == ["http://only.test"]

    def test_empty_value_yields_no_origins(self) -> None:
        assert build(cors_origins="").cors_origins == []

    def test_default_is_empty(self) -> None:
        assert build().cors_origins == []

    def test_a_list_is_passed_through_untouched(self) -> None:
        settings = build(cors_origins=["http://a.test"])
        assert settings.cors_origins == ["http://a.test"]

    def test_malformed_json_reports_a_readable_error(self) -> None:
        """A typo must not surface as a bare ``JSONDecodeError`` from pydantic."""
        with pytest.raises(ValidationError, match="comma-separated list"):
            build(cors_origins='["http://a.test",')

    def test_json_non_array_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="JSON array"):
            build(cors_origins='{"origin": "http://a.test"}')

    def test_wildcard_with_credentials_is_flagged(self) -> None:
        """A wildcard plus credentials lets any origin read authenticated responses."""
        settings = build(cors_origins="*", cors_allow_credentials=True, app_env="production")
        assert any(
            "CORS_ALLOW_CREDENTIALS" in problem for problem in settings.validate_environment()
        )


class TestDevLoginGuard:
    """The development sign-in must be impossible to ship by accident."""

    def test_production_refuses_the_flag(self) -> None:
        settings = build(app_env="production", dev_login_enabled=True)
        assert any(
            "DEV_LOGIN_ENABLED" in problem for problem in settings.validate_environment()
        )

    def test_production_is_clean_without_the_flag(self) -> None:
        settings = build(app_env="production", dev_login_enabled=False)
        assert not any(
            "DEV_LOGIN_ENABLED" in problem for problem in settings.validate_environment()
        )

    def test_development_may_enable_the_flag(self) -> None:
        """The guard is a production rule, not a ban: local development needs it."""
        settings = build(app_env="development", dev_login_enabled=True)
        assert not any(
            "DEV_LOGIN_ENABLED" in problem for problem in settings.validate_environment()
        )
