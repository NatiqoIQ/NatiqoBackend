"""Application configuration.

Single source of truth for every environment-dependent value. Loaded from
environment variables (and an optional ``.env`` file) through ``pydantic-settings``.

Rules:
- Nothing here is read at import time by domain code; use ``get_settings()``.
- No secret ever has a usable default in production: ``validate_environment()``
  refuses to boot with placeholder values when ``APP_ENV`` is production.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

AppEnvironment = Literal["development", "testing", "staging", "production"]

_PLACEHOLDER_SECRETS = {
    "change-me",
    "dev-only-access-secret-change-me-0000000000000000000000",
    "dev-only-refresh-secret-change-me-00000000000000",
}


class Settings(BaseSettings):
    """Typed application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------- runtime
    app_env: AppEnvironment = "development"
    app_name: str = "Natiqo API"
    api_v1_prefix: str = "/api/v1"
    debug: bool = False
    host: str = "0.0.0.0"  # noqa: S104 - container/VM binding, overridden by reverse proxy
    port: int = 8000
    root_path: str = ""

    # ----------------------------------------------------------- database
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = "natiqo"
    postgres_password: SecretStr = SecretStr("change-me")
    postgres_db: str = "natiqo"
    database_url: str | None = None

    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_timeout: int = 30
    database_pool_recycle: int = 1800
    database_echo: bool = False

    # -------------------------------------------------------------- redis
    redis_url: str | None = None
    cache_ttl_seconds: int = 300
    queue_enabled: bool = False

    # ----------------------------------------------------------- security
    jwt_secret: SecretStr = SecretStr("dev-only-access-secret-change-me-0000000000000000000000")
    jwt_refresh_secret: SecretStr = SecretStr("dev-only-refresh-secret-change-me-00000000000000")
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "natiqo"
    jwt_audience: str = "natiqo-clients"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2_592_000
    password_argon2_time_cost: int = 3
    password_argon2_memory_cost: int = 65_536
    password_argon2_parallelism: int = 4
    password_min_length: int = 10

    #: ``NoDecode`` is load-bearing, not decoration. Without it pydantic-settings
    #: JSON-decodes any complex field before validators run, so the comma-separated
    #: form documented in ``.env.example`` raises ``SettingsError`` at startup and
    #: :meth:`_split_origins` never executes. With it the raw string arrives intact.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cors_allow_credentials: bool = True

    #: Only enable behind a reverse proxy you control. When false, X-Forwarded-For
    #: is ignored for rate limiting so a client cannot spoof it to dodge its budget.
    trust_forwarded_for: bool = False

    # ------------------------------------------------------------ limits
    rate_limit_enabled: bool = True
    rate_limit_requests_per_minute: int = 120
    rate_limit_auth_requests_per_minute: int = 10
    #: The catalogue is public, unauthenticated and cacheable, so it gets its own budget
    #: rather than competing with the write path. A learner scrolling a level list while
    #: the app refreshes a token should not be able to exhaust one another's allowance.
    rate_limit_catalogue_requests_per_minute: int = 240
    #: Grading an answer is a write per question, so a lesson costs a burst of them. The
    #: default scope (120/min) is the right order of magnitude for a normal lesson.
    rate_limit_quiz_requests_per_minute: int = 120
    max_request_body_bytes: int = 1_048_576
    login_max_failed_attempts: int = 5
    login_lockout_seconds: int = 900
    #: How long a client-supplied ``Idempotency-Key`` is honoured. 24h covers a client
    #: retrying after a night of no signal, which is the realistic mobile failure.
    idempotency_ttl_seconds: int = 86_400
    #: SMTP for password reset, email verification and notifications. Unset means
    #: "development": the logging adapter is used and nobody receives an email. That is
    #: fine locally and a silent production failure otherwise, so it is checked at boot.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str = "noreply@natiqo.app"
    smtp_use_tls: bool = True
    #: Base URL used to build links inside emails (verification, password reset).
    public_web_base_url: str = "https://natiqo.app"
    #: A quiz session left open is abandoned after this long. Long enough for a learner who
    #: pauses mid-lesson, short enough that a resumed session still feels like theirs.
    quiz_session_ttl_seconds: int = 7_200

    # ----------------------------------------------------------- storage
    s3_endpoint: str | None = None
    s3_region: str = "eu-west-2"
    s3_bucket: str = "natiqo-media"
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_force_path_style: bool = False
    signed_url_ttl_seconds: int = 900
    local_storage_path: str = "./media_local"

    # --------------------------------------------------------- push (FCM)
    fcm_credentials_json: str | None = None
    fcm_project_id: str | None = None

    # ---------------------------------------------------------------- ai
    ai_enabled: bool = False
    ai_provider: Literal["none", "openai", "anthropic"] = "none"
    ai_api_key: SecretStr | None = None
    ai_model: str | None = None
    ai_timeout_seconds: int = 20
    ai_max_input_chars: int = 4000
    ai_max_output_tokens: int = 800
    ai_daily_user_budget_usd: float = 0.50

    # ---------------------------------------------------------- payments
    payment_provider: Literal["none", "stripe"] = "none"
    payment_api_key: SecretStr | None = None
    payment_webhook_secret: SecretStr | None = None
    currency: str = "IQD"

    # ------------------------------------------------------ gamification
    max_hearts: int = 5
    heart_regen_seconds: int = 1800
    premium_unlimited_hearts: bool = True
    daily_goal_default: int = 30

    # ----------------------------------------------------- observability
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    otel_enabled: bool = False
    otel_service_name: str = "natiqo-api"
    otel_exporter_otlp_endpoint: str | None = None
    sentry_dsn: str | None = None

    # ---------------------------------------------------------- testing
    allow_sqlite_tests: bool = False

    # ------------------------------------------------------- validators
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept both ``a,b`` and ``["a","b"]``.

        The comma-separated form is what ``.env.example`` documents and what a human
        writes in a shell, so it is the primary case. JSON is still accepted because it
        is the only way to express an origin containing a comma, and because
        deployments that predate this validator were already written in JSON.
        """
        if not isinstance(value, str):
            return value

        text = value.strip()
        if text.startswith(("[", "{")):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "cors_origins looks like JSON but is not valid JSON; "
                    "use a comma-separated list instead."
                ) from exc
            if not isinstance(decoded, list):
                raise ValueError("cors_origins must be a JSON array of strings.")
            return [str(item).strip() for item in decoded if str(item).strip()]

        return [item.strip() for item in text.split(",") if item.strip()]

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        return value.upper()

    # -------------------------------------------------- computed values
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_testing(self) -> bool:
        return self.app_env == "testing"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_dsn(self) -> str:
        """Async SQLAlchemy DSN. Never log this value."""
        if self.database_url:
            url = self.database_url
            if url.startswith("postgresql://"):
                return url.replace("postgresql://", "postgresql+asyncpg://", 1)
            if url.startswith("postgres://"):
                return url.replace("postgres://", "postgresql+asyncpg://", 1)
            return url
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_postgres_dsn(self) -> str:
        """psycopg-free sync DSN used by Alembic's offline mode only."""
        if self.database_url:
            return self.database_url.replace("+asyncpg", "")
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uses_redis(self) -> bool:
        return bool(self.redis_url)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uses_s3(self) -> bool:
        return bool(self.s3_endpoint and self.s3_access_key_id)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uses_fcm(self) -> bool:
        return bool(self.fcm_credentials_json)

    # ------------------------------------------------------ guardrails
    def validate_environment(self) -> list[str]:
        """Return a list of configuration problems. Empty list means safe to boot."""
        problems: list[str] = []

        if self.cors_origins and "*" in self.cors_origins and self.cors_allow_credentials:
            problems.append("CORS_ORIGINS must not contain '*' when CORS_ALLOW_CREDENTIALS is true")

        if not self.is_production:
            return problems

        for name, secret in (
            ("JWT_SECRET", self.jwt_secret),
            ("JWT_REFRESH_SECRET", self.jwt_refresh_secret),
        ):
            if secret.get_secret_value() in _PLACEHOLDER_SECRETS:
                problems.append(f"{name} still holds a development placeholder")
        if self.jwt_secret.get_secret_value() == self.jwt_refresh_secret.get_secret_value():
            problems.append("JWT_SECRET and JWT_REFRESH_SECRET must be different values")
        if len(self.jwt_secret.get_secret_value()) < 32:
            problems.append("JWT_SECRET must be at least 32 characters")
        if self.debug:
            problems.append("DEBUG must be false in production")
        if self.postgres_password.get_secret_value() in _PLACEHOLDER_SECRETS:
            problems.append("POSTGRES_PASSWORD still holds a development placeholder")
        if not self.uses_redis:
            problems.append("REDIS_URL must be configured in production")
        if self.ai_enabled and self.ai_provider == "none":
            problems.append("AI_ENABLED requires a real AI_PROVIDER")
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Use this everywhere instead of module constants."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cache. Test-only helper."""
    get_settings.cache_clear()
