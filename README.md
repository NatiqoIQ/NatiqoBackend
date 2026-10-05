# Natiqo — Backend

Production-oriented backend for the **Natiqo** British English learning app, built for
Iraqi learners. FastAPI + PostgreSQL + Redis, organised as a **modular monolith with
Clean Architecture inside every module**.

```
Flutter  ──HTTPS/JSON──►  FastAPI  ──►  PostgreSQL   (authoritative learning state)
                              │      ──►  Redis        (cache + job queue)
                              │      ──►  S3 storage  (audio / media)
                              └─► Learning Engine (lessons → quiz → progress → review → SRS)
```

## Quick start

The repository root **is** the Python project — there is no `backend/` wrapper folder.

```bash
uv sync                                   # install dependencies (creates .venv at the root)
cp .env.example .env                      # then edit DATABASE_URL / secrets
uv run alembic upgrade head               # create the schema (PostgreSQL only)
uv run python -m scripts.seed --dry-run   # preview the demo accounts
uv run python -m scripts.seed             # load the demo accounts
uv run uvicorn app.main:app --reload --port 8100   # http://127.0.0.1:8100/docs
```

**SQLite (zero external services).** Set `DATABASE_URL=sqlite+aiosqlite:///./local.db` and
`REDIS_URL=` (empty selects the in-memory cache/queue adapters), then create the schema
without Alembic — the migrations are PostgreSQL-specific:

```bash
uv run python -c "import asyncio; from app.core.registry import import_all_models; \
from app.core.database import create_all; import_all_models(); asyncio.run(create_all())"
```

**PostgreSQL.** Set `DATABASE_URL=postgresql+asyncpg://natiqo:<password>@127.0.0.1:<port>/natiqo`
(the discrete `POSTGRES_*` values are overridden by `DATABASE_URL`), then run
`uv run alembic upgrade head` as above.

`CORS_ORIGINS` accepts a comma-separated list (`http://localhost:3000,http://localhost:8080`),
which is the form `.env.example` uses and the one to prefer. A JSON array is also accepted.
The field is typed `list[str]`, so it carries `NoDecode` — otherwise `pydantic-settings`
would try to JSON-parse the raw string before any validator ran and the comma-separated
form would fail at startup. Never use `*` while `CORS_ALLOW_CREDENTIALS=true`; startup
validation rejects that combination.

Ports 8000 and 8099 are commonly already taken by local tooling — pass an explicit
`--port` if the bind fails.

## Quality gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

## Demo data

`scripts/seed.py` loads six demo accounts so the app has something realistic to render:
each one sits on a different branch — admin, learner, active, suspended, Arabic-speaking,
different CEFR levels and daily goals.

```bash
uv run python -m scripts.seed --dry-run   # show the plan, write nothing
uv run python -m scripts.seed             # interactive; asks before writing
uv run python -m scripts.seed --yes --only admin --only learner
```

| Flag | Effect |
| --- | --- |
| `--dry-run` | Prints the plan and exits. Writes nothing, in any environment. |
| `--yes` | Skips the confirmation prompt. Required for unattended runs. |
| `--only PERSONA` | Restricts to one persona. Repeatable. |
| `--allow-production` | Permits a production write. Off unless you pass it. |

Every persona uses the password **`Natiqo-Demo-2026`**. It is public and printed here on
purpose — these are throwaway accounts for local and demo use. Never reuse it, and never
seed a real user database: the seeder refuses to write in production without
`--allow-production`.

### Reruns are safe

Run it as often as you like. Accounts are matched by email, and their devices and usage
history are written under deterministic ids, so a second run updates the personas in
place rather than creating duplicates — and it reports zero changes. Run it twice and
compare the reports if you want to see that for yourself.

The one case worth knowing about: if a run is interrupted, `AuthService.register()`
commits each account as it is created, so a partial run can leave an account with no
profile behind it. Simply run the seeder again — it finds the account, completes the
missing rows, and leaves the rest untouched.

### It will not touch accounts it does not own

If an email already exists with a different password, the seeder skips that persona and
says so. It never re-hashes someone else's password to the demo value, and it writes
nothing at all for a persona it skipped.

## Layout

```
app/
  core/          cross-cutting: config, db, security, logging, errors, middleware, events
  api/v1/        HTTP composition root — routers only, no business logic
  modules/<m>/   one folder per bounded context, Clean Architecture layers:
                   domain/         entities, value objects, policies, ports (Protocols)
                   application/    use cases / DTOs — orchestrates domain + ports
                   infrastructure/ SQLAlchemy models, repository implementations
                   presentation/   FastAPI router, request/response schemas
  platform/      outbound adapters for external systems (cache, queue, storage, push, AI)
```

Dependency rule: `presentation → application → domain`, and `infrastructure`/`platform`
implement ports declared in `domain`. `domain` never imports FastAPI, SQLAlchemy or Redis.

## API surface

The table below is generated from the running application, not from intent. Every
operation listed under **Implemented** is exercised by a test that calls it over HTTP
through the ASGI app; `test_every_published_operation_is_covered` fails the build if a
route is added without one.

Client-facing documentation:

- **[Mobile integration guide (Arabic)](docs/mobile-integration-ar.md)** — the reference
  for building the phone app: every endpoint, auth and token lifecycle, the single-flight
  refresh requirement, error codes, caching, rate limits, and what does not exist yet.
- **[Backend roadmap (Arabic)](docs/backend-roadmap-ar.md)** — the phased plan for the
  missing API, ordered by what unblocks the app first, with the schema and endpoints each
  phase adds and the product decisions it waits on.
- [Client contract (English)](docs/client-contract.md) — the wire-level contract, naming
  and error conventions.
- `GET /openapi.json` — machine-readable schema for client generation. Every versioned
  operation publishes the real `{ "error": { code, message, details, requestId } }`
  envelope for 400/401/403/404/409/422/429/500, so generated clients match the wire.

### Implemented

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `GET` | `/health` | — | Liveness probe |
| `GET` | `/ready` | — | Readiness; verifies the database and cache, `503` when unhealthy |
| `POST` | `/api/v1/auth/register` | — | Create an account, issues access + refresh tokens |
| `POST` | `/api/v1/auth/login` | — | Exchange credentials for tokens |
| `POST` | `/api/v1/auth/refresh` | refresh token | Rotate a refresh token; replay is rejected |
| `POST` | `/api/v1/auth/logout` | refresh token | Revoke this device, or `all_devices: true` |
| `POST` | `/api/v1/auth/forgot-password` | — | Request a reset; the same `200` either way, never reveals whether the account exists |
| `POST` | `/api/v1/auth/reset-password` | — | Consume a reset token; revokes every existing session |
| `GET` | `/api/v1/me` | Bearer | Current profile and settings |
| `PATCH` | `/api/v1/me` | Bearer | PATCH semantics — omit a field to leave it alone. Accepts profile and settings fields under either spelling (see [Client contract](docs/client-contract.md)); deliberately refuses `currentLevel` so a learner cannot bypass the placement test |
| `GET` | `/api/v1/me/devices` | Bearer | List the caller's devices |
| `DELETE` | `/api/v1/me/devices/{device_id}` | Bearer | Revoke one of the caller's devices |
| `GET` | `/api/v1/content/levels` | — | Published CEFR levels, each with its unit count. Cacheable |
| `GET` | `/api/v1/content/levels/{level_code}/units` | — | Published units of one level. Cacheable |
| `GET` | `/api/v1/content/units/{unit_id}/lessons` | — | Published lessons of one unit. Cacheable |
| `GET` | `/api/v1/content/lessons/{lesson_id}` | — | A lesson and its questions, with **no answer key**. Cacheable |

The four catalogue routes are deliberately unauthenticated: an app shell has to be able to
show that a beginner course exists before anyone signs in. That is safe only because the
question schema withholds `correct_index`, `accepted_answers` and `explanation` — the
practisable question is readable before an attempt, the graded feedback only after one.

Populate them with `uv run python -m scripts.seed_content --yes` (see `scripts/`).

### Not yet implemented

These are planned bounded contexts. No router is mounted, so any request to these
prefixes currently returns `404` — they are listed here to make the gap explicit
rather than to imply they work.

| Context | Planned prefix |
| --- | --- |
| Content (authoring) | `/api/v1/content/*` — writes, review queue, publishing |
| Vocabulary / Grammar | `/api/v1/vocabulary`, `/api/v1/grammar` |
| Quiz | `/api/v1/quiz/sessions` |
| Progress | `/api/v1/progress`, `/api/v1/review` |
| Gamification | `/api/v1/gamification/*`, `/api/v1/streak` |
| Notifications | `/api/v1/notifications/*` |
| Subscriptions | `/api/v1/subscriptions/*` |
| AI | `/api/v1/ai/*` |
| Admin | `/api/v1/admin/*` |

`app/platform/ai/prompts.py` already holds the prompt templates and CEFR guidance the
AI context will need, and is not yet imported by any route.

#   N a t i q o  
 