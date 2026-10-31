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
COPY media ./media
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

# Migrations are *not* run here. They run once, in the dedicated ``migrate`` service
# (docker-compose) or in the release phase of the platform. Starting the web process
# with ``alembic upgrade head`` meant every replica raced the same DDL on a rolling
# deploy, and a slow migration kept the instance out of the load balancer for its whole
# duration instead of failing fast.
#
# ``--proxy-headers`` is what makes ``X-Forwarded-For`` real inside the process: without
# it every request appears to come from the proxy's address, and the rate limiter — and
# every audit log — sees one client. ``--forwarded-allow-ips`` is limited to the private
# ranges a load balancer lives in, not '*', so a direct client cannot forge the header.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '10.0.0.0/8,172.16.0.0/12,192.168.0.0/16' --workers ${WEB_CONCURRENCY:-2} --timeout-keep-alive 75 --timeout-graceful-shutdown 20"]
