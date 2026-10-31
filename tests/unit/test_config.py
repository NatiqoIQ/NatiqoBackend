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

import re

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


class TestDevLogin:
    """The development sign-in is a development affordance, and only that.

    Its credentials are fixed strings committed to the repository, so the honest default is
    off and a production boot refuses to start while the door is open. A local ``.env``
    opts in explicitly, which is the only place the backdoor is ever appropriate.
    """

    def test_off_by_default(self) -> None:
        """Read the shipped default off the model: the test env forces the flag off too."""
        assert Settings.model_fields["dev_login_enabled"].default is False

    def test_production_refuses_to_boot_with_it_on(self) -> None:
        settings = build(app_env="production", dev_login_enabled=True)
        assert any("DEV_LOGIN_ENABLED" in problem for problem in settings.validate_environment())

    def test_production_boots_with_it_off(self) -> None:
        settings = build(app_env="production", dev_login_enabled=False)
        assert not any(
            "DEV_LOGIN_ENABLED" in problem for problem in settings.validate_environment()
        )

    def test_development_can_opt_in(self) -> None:
        settings = build(dev_login_enabled=True)
        assert settings.dev_login_enabled is True
        assert not any(
            "DEV_LOGIN_ENABLED" in problem for problem in settings.validate_environment()
        )


class TestDevLoginCors:
    """The loopback grant that makes the backdoor usable from a browser.

    A dev server's port is chosen at run time, so it cannot be enumerated in
    ``CORS_ORIGINS`` — and a missing grant is invisible in the API's logs, because the
    browser blocks the response rather than the server refusing the request.
    """

    def test_any_loopback_port_matches(self) -> None:
        regex = build(dev_login_enabled=True).cors_origin_regex
        assert regex is not None
        for origin in (
            "http://localhost:5173",
            "http://127.0.0.1:8100",
            "http://[::1]:3000",
            "https://localhost",
        ):
            assert re.fullmatch(regex, origin), origin

    def test_lookalike_hosts_do_not_match(self) -> None:
        """``localhost.evil.test`` starts with the word and must still be refused."""
        regex = build(dev_login_enabled=True).cors_origin_regex
        assert regex is not None
        for origin in (
            "https://app.natiqo.app",
            "http://localhost.evil.test:5173",
            "https://127.0.0.1.evil.test",
            "http://notlocalhost:5173",
        ):
            assert re.fullmatch(regex, origin) is None, origin

    def test_production_gets_no_pattern(self) -> None:
        assert build(app_env="production").cors_origin_regex is None

    def test_no_pattern_without_the_backdoor(self) -> None:
        assert build(dev_login_enabled=False).cors_origin_regex is None

    def test_a_configured_pattern_is_combined_with_loopback(self) -> None:
        settings = build(
            dev_login_enabled=True,
            cors_allow_origin_regex=r"https://[a-z0-9-]+\.preview\.natiqo\.app",
        )
        regex = settings.cors_origin_regex
        assert regex is not None
        assert re.fullmatch(regex, "https://pr-42.preview.natiqo.app")
        assert re.fullmatch(regex, "http://localhost:5173")
        assert re.fullmatch(regex, "https://elsewhere.example") is None

    def test_a_malformed_pattern_fails_at_settings_time(self) -> None:
        """Otherwise the typo surfaces as a bare ``re.error`` while the app is assembled."""
        with pytest.raises(ValidationError, match="regular expression"):
            build(cors_allow_origin_regex="[unclosed")

    def test_an_empty_pattern_means_no_pattern(self) -> None:
        """``CORS_ALLOW_ORIGIN_REGEX=`` in an env file must not become a match-everything."""
        assert build(cors_allow_origin_regex="  ").cors_allow_origin_regex is None

    def test_a_configured_pattern_survives_production_alone(self) -> None:
        settings = build(
            app_env="production",
            cors_allow_origin_regex=r"https://[a-z0-9-]+\.preview\.natiqo\.app",
        )
        regex = settings.cors_origin_regex
        assert regex is not None
        assert re.fullmatch(regex, "https://pr-42.preview.natiqo.app")
        assert re.fullmatch(regex, "http://localhost:5173") is None


class TestAccessTokenLifetime:
    """F12: the default is a deliberate load decision, not an accident of a constant."""

    def test_the_default_is_thirty_minutes(self) -> None:
        assert build().access_token_ttl_seconds == 1800

    def test_an_operator_can_still_choose_fifteen(self) -> None:
        assert build(access_token_ttl_seconds=900).access_token_ttl_seconds == 900
