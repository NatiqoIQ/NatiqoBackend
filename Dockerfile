# syntax=docker/dockerfile:1
# ------------------------------------------------------------------------------
# Natiqo backend — production image.
#
# Stage 1 resolves the lockfile and builds the project into /app/.venv.
# Stage 2 ships that venv on python:3.12-slim with no toolchain, no package
# manager writes and no root process.
#
#   docker compose up -d --build
# ------------------------------------------------------------------------------

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency layer: unchanged by the source, so it stays cached across deploys.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY alembic ./alembic
COPY scripts ./scripts
COPY alembic.ini README.md ./
RUN uv sync --frozen --no-dev

# ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /app/media_local \
    && chown -R app:app /app

WORKDIR /app

COPY --from=builder --chown=app:app /app /app

USER app

EXPOSE 8000

# Liveness only: /health is dependency-free by design, so a database
# outage reports a healthy process rather than a restart loop.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Migrations run on every start: a container serving a schema its code no longer
# expects fails here, before the first request, instead of at query time.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
