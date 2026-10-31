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
import re
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

#: Any loopback origin, on any port. Only in force outside production and only while the
#: development sign-in is mounted (see :attr:`Settings.cors_origin_regex`).
#:
#: The port cannot be part of a static allowlist: the dashboard dev server takes whatever
#: port is free (Vite moves 5173 -> 5174 the moment a second one is running), and a
#: ``CORS_ORIGINS`` miss is not a polite error — the browser blocks the *response*, so
#: ``POST /auth/dev-login`` appears to fail with no status code and no server-side trace.
#: Matching loopback instead of enumerating ports is what makes the backdoor reachable
#: from a browser at all.
DEV_CORS_ORIGIN_REGEX = r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?"


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
    # Expose the interactive API docs even in production. Defaults to False: in
    # production /docs and /openapi.json stay hidden unless this is set.
    docs_enabled: bool = False

    # ----------------------------------------------------------- database
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = "natiqo"
    postgres_password: SecretStr = SecretStr("change-me")
    postgres_db: str = "natiqo"
    database_url: str | None = None

    database_pool_size: int = 10
    database_max_overflow: int = 20
    #: Fail fast rather than queue behind a saturated pool. 30 seconds meant a request
    #: could wait half a minute before reaching the database at all, which turns a slow
    #: query into a whole-API stall instead of a quick 503.
    database_pool_timeout: int = 3
    database_pool_recycle: int = 1800
    database_echo: bool = False
    #: Server-side guards. A statement or lock that runs past these is an incident, not
    #: a request to wait for: without them one bad query holds a pooled connection until
    #: the client gives up, and every later request queues behind it.
    database_statement_timeout_ms: int = 4_000
    database_lock_timeout_ms: int = 1_500
    database_idle_transaction_timeout_ms: int = 8_000

    # -------------------------------------------------------------- redis
    redis_url: str | None = None
    #: Optional second Redis for background jobs. Separate on purpose: the cache needs
    #: ``allkeys-lru`` so memory pressure evicts the least useful entry, while the queue
    #: needs ``noeviction`` so a job is never silently discarded. One instance cannot be
    #: both. Empty means "reuse REDIS_URL" for development only.
    queue_redis_url: str | None = None
    cache_ttl_seconds: int = 300
    #: How long the assembled catalogue tree may be served from the cache. Short on
    #: purpose: publishing is rare but an editor expects to see it, and 30s of staleness
    #: costs one rebuild while a longer TTL would make "I published it" look broken.
    catalogue_cache_ttl_seconds: int = 30
    queue_enabled: bool = False

    # ----------------------------------------------------------- security
    jwt_secret: SecretStr = SecretStr("dev-only-access-secret-change-me-0000000000000000000000")
    jwt_refresh_secret: SecretStr = SecretStr("dev-only-refresh-secret-change-me-00000000000000")
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "natiqo"
    jwt_audience: str = "natiqo-clients"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2_592_000

    # ------------------------------------------------------- social sign-in
    #: Accepted audiences for a Google ID token. The mobile client obtains the token
    #: from the Google Sign-In SDK and posts it to ``/auth/google``; the server verifies
    #: its signature against Google's published keys and checks that it was minted for
    #: one of these client ids. ``NoDecode`` for the same reason as ``cors_origins``:
    #: the value is a comma-separated list in the environment, not JSON.
    google_client_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Accepted audiences for an Apple ID token (the app's bundle id and, when a web
    #: flow is added, its service id).
    apple_client_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Overridable so a deployment — or a test — can point at a different key set
    #: without a code change.
    google_jwks_url: str = "https://www.googleapis.com/oauth2/v3/certs"
    apple_jwks_url: str = "https://appleid.apple.com/auth/keys"
    #: How long a fetched JWK set is reused before it is fetched again.
    jwks_cache_seconds: int = 3600

    #: ``NoDecode`` is load-bearing, not decoration. Without it pydantic-settings
    #: JSON-decodes any complex field before validators run, so the comma-separated
    #: form documented in ``.env.example`` raises ``SettingsError`` at startup and
    #: :meth:`_split_list` never executes. With it the raw string arrives intact.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cors_allow_credentials: bool = True
    #: Optional regex matched against the whole ``Origin`` header, for clients whose
    #: origin is not known ahead of time (a dev server that picks the next free port, a
    #: preview deployment per branch). Kept separate from ``cors_origins`` because the
    #: two are matched differently by Starlette: the list is exact, the regex is not, so
    #: the exact list should stay the default and this is the deliberate exception.
    cors_allow_origin_regex: str | None = None

    #: Only enable behind a reverse proxy you control. When false, X-Forwarded-For
    #: is ignored for rate limiting so a client cannot spoof it to dodge its budget.
    trust_forwarded_for: bool = False

    # ------------------------------------------------- development sign-in
    #: A username/password sign-in that exists so a developer or QA can enter the app
    #: without configuring a provider client id. **Off by default**, and production refuses
    #: to boot while it is on: the credentials are fixed strings (``dev_admin_user`` /
    #: ``dev_admin_password``) that live in the repository, so an enabled backdoor on a
    #: public deployment is an open door for anyone who has read them. A local ``.env``
    #: opts in explicitly.
    dev_login_enabled: bool = False
    dev_admin_user: str = "admin"
    dev_admin_password: SecretStr = SecretStr("admin")
    #: Sessions minted through the dev sign-in live this long, so a test run is never
    #: interrupted by a 15-minute expiry. Normal sign-in keeps the token TTLs above.
    dev_login_token_ttl_seconds: int = 315_360_000  # 10 years

    # ------------------------------------------------------------ limits
    rate_limit_enabled: bool = True
    rate_limit_requests_per_minute: int = 120
    rate_limit_auth_requests_per_minute: int = 120
    #: The catalogue is public, unauthenticated and cacheable, so it gets its own budget
    #: rather than competing with the write path. A learner scrolling a level list while
    #: the app refreshes a token should not be able to exhaust one another's allowance.
    rate_limit_catalogue_requests_per_minute: int = 1_200
    #: Grading an answer is a write per question, so a lesson costs a burst of them. The
    #: default scope (120/min) is the right order of magnitude for a normal lesson.
    rate_limit_quiz_requests_per_minute: int = 120
    #: Progress reads are authenticated but read-only, and a client polling a summary after
    #: every lesson should not have to share the write budget. Separate scope, own allowance.
    rate_limit_progress_requests_per_minute: int = 240
    #: Reviewing is a write per card but the reads are polled, so the scope mirrors the
    #: progress budget rather than the tighter write one: a learner working a queue of
    #: twenty cards should not share the allowance with their progress polling.
    rate_limit_review_requests_per_minute: int = 240
    #: The tutor is the one surface where a request can trigger an upstream, billable call,
    #: so it gets its own, tighter allowance. A runaway client looping the chat burns money
    #: rather than merely bandwidth, which is why this is the tightest scope in the app.
    rate_limit_ai_requests_per_minute: int = 30
    #: The gamification screens are authenticated reads with two rare writes (claim, freeze),
    #: so the scope mirrors the progress budget: a client refreshing a badge list should not
    #: share the allowance with the progress summary or the quiz answer burst.
    rate_limit_gamification_requests_per_minute: int = 240
    #: The admin surface is authenticated, low-traffic and write-heavy (an editor saving a
    #: lesson, reordering questions), so it gets a modest budget of its own. It is deliberately
    #: *not* the generous public allowance: this is staff tooling, not a learner-facing path, and
    #: a runaway script here edits the catalogue rather than merely reading it.
    rate_limit_admin_requests_per_minute: int = 120
    #: The media upload surface. Separate from the admin scope because it is a learner path
    #: (avatars, pronunciation clips) rather than staff tooling, and because the presign and
    #: confirm calls bracket a real upload — a client retrying a flaky PUT should not be able
    #: to spend an editor's budget, nor an editor a learner's.
    rate_limit_media_requests_per_minute: int = 120
    max_request_body_bytes: int = 1_048_576
    #: How long a client-supplied ``Idempotency-Key`` is honoured. 24h covers a client
    #: retrying after a night of no signal, which is the realistic mobile failure.
    idempotency_ttl_seconds: int = 86_400
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
    #: Directory of shipped, read-only curriculum media (image and audio aids), served
    #: same-origin at ``/media``. This is content baked with the release, not user
    #: uploads — those go through the object-storage port above.
    media_root: str = "./media"
    #: Ceilings applied when an upload is confirmed, measured from the stored object rather
    #: than trusted from a request header. Images and audio are separate because the two are
    #: genuinely different sizes: a 5 MiB avatar is already generous, while a 5 MiB
    #: pronunciation clip would cut off mid-sentence. These never meet the request-body
    #: ceiling above — the bytes go straight to storage, not through the API process.
    media_max_image_bytes: int = 5_242_880
    media_max_audio_bytes: int = 10_485_760

    # --------------------------------------------------------- push (FCM)
    fcm_credentials_json: str | None = None
    fcm_project_id: str | None = None

    # ---------------------------------------------------------------- ai
    ai_enabled: bool = False
    ai_provider: Literal["none", "openai", "anthropic"] = "none"
    ai_api_key: SecretStr | None = None
    ai_model: str | None = None
    #: A model that has not answered in ten seconds is not going to improve the lesson.
    #: The old 20s kept a request (and a database connection) waiting far past the point
    #: where the deterministic fallback is the better answer.
    ai_timeout_seconds: int = 10
    ai_max_input_chars: int = 4000
    ai_max_output_tokens: int = 800
    ai_daily_user_budget_usd: float = 0.50
    #: Days of AI usage kept for cost attribution. Zero disables the purge job.
    ai_usage_retention_days: int = 180
    #: Bulkhead: the most upstream model calls one process will run at once. Beyond this
    #: the service falls back immediately instead of queueing behind a slow provider.
    ai_max_concurrent_requests: int = 20

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
    #: Free streak freezes a learner may hold. Granted when their stats row is first built.
    max_streak_freezes: int = 2
    #: The simple premium gate for this phase. Real subscriptions arrive in a later phase;
    #: until then every learner is treated as non-premium, so ``POST /streak/freeze`` answers
    #: ``PREMIUM_REQUIRED`` with the purchase link below. Flip this on to exercise the paid
    #: path — there is no per-account plan yet, on purpose.
    premium_enabled: bool = False
    premium_purchase_url: str = "https://natiqo.app/premium"

    # ----------------------------------------------------- observability
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    #: Prometheus exposition at ``/metrics``. Off would make an orchestrator scrape
    #: nothing; the flag exists for an environment that has no scraper and would rather
    #: not publish the route at all.
    metrics_enabled: bool = True
    otel_enabled: bool = False
    otel_service_name: str = "natiqo-api"
    otel_exporter_otlp_endpoint: str | None = None
    sentry_dsn: str | None = None

    # ---------------------------------------------------------- testing
    allow_sqlite_tests: bool = False

    # ------------------------------------------------------- validators
    @field_validator("cors_origins", "google_client_ids", "apple_client_ids", mode="before")
    @classmethod
    def _split_list(cls, value: object) -> object:
        """Accept both ``a,b`` and ``["a","b"]`` for every comma-separated setting.

        The comma-separated form is what ``.env.example`` documents and what a human
        writes in a shell, so it is the primary case. JSON is still accepted because it
        is the only way to express a value containing a comma, and because
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
                    "value looks like JSON but is not valid JSON; "
                    "use a comma-separated list instead."
                ) from exc
            if not isinstance(decoded, list):
                raise ValueError("value must be a JSON array of strings.")
            return [str(item).strip() for item in decoded if str(item).strip()]

        return [item.strip() for item in text.split(",") if item.strip()]

    @field_validator("cors_allow_origin_regex")
    @classmethod
    def _valid_origin_regex(cls, value: str | None) -> str | None:
        """Reject a malformed pattern at settings time, not at first preflight.

        ``CORSMiddleware`` compiles the pattern while the app is being assembled, so a
        typo there surfaces as a bare ``re.error`` traceback before the lifespan even runs
        — the one place where this project's "refuse to start with a readable reason"
        rule had no readable reason. An empty value means "no pattern", the same as unset.
        """
        if value is None or not value.strip():
            return None
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"is not a valid regular expression: {exc}") from exc
        return value

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

    @property
    def cors_origin_regex(self) -> str | None:
        """The regex to hand ``CORSMiddleware``, or ``None`` when no pattern applies.

        Combines the configured pattern with the loopback allowance granted by the
        development sign-in. The allowance is scoped to the backdoor on purpose: it
        exists so a browser on ``localhost:<any>`` can reach ``/auth/dev-login``, and it
        disappears in production and whenever ``DEV_LOGIN_ENABLED=false``, where the
        exact ``CORS_ORIGINS`` list is the only grant.

        A plain property rather than a computed field: this is wiring for the middleware,
        not a value a client or a log line should see.
        """
        patterns: list[str] = []
        if self.cors_allow_origin_regex:
            patterns.append(f"(?:{self.cors_allow_origin_regex})")
        if self.dev_login_enabled and not self.is_production:
            patterns.append(f"(?:{DEV_CORS_ORIGIN_REGEX})")
        if not patterns:
            return None
        return "|".join(patterns)

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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uses_google(self) -> bool:
        return bool(self.google_client_ids)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uses_apple(self) -> bool:
        return bool(self.apple_client_ids)

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
        if self.uses_redis and not self.queue_enabled:
            # Without this, the API enqueues into an in-memory queue inside its own
            # process and the worker — a different process — sees an empty Redis. The
            # only visible symptom would be background work that never happens.
            problems.append(
                "QUEUE_ENABLED must be true when REDIS_URL is set: otherwise the API "
                "enqueues into a per-process memory queue the worker cannot see"
            )
        if not self.uses_google and not self.uses_apple:
            problems.append("Configure GOOGLE_CLIENT_IDS and/or APPLE_CLIENT_IDS in production")
        if self.ai_enabled and self.ai_provider == "none":
            problems.append("AI_ENABLED requires a real AI_PROVIDER")
        if self.dev_login_enabled:
            problems.append(
                "DEV_LOGIN_ENABLED must be false in production: its credentials are fixed "
                "development strings, not deployment secrets"
            )
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Use this everywhere instead of module constants."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the cache. Test-only helper."""
    get_settings.cache_clear()
