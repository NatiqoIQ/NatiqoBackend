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

## Sign-in

Accounts are created and reached through **Google or Apple**; there is no password
anywhere in the system, so there is nothing to hash, reset or leak.

The client obtains an ID token from the provider's native SDK and POSTs it to
`/api/v1/auth/google` or `/api/v1/auth/apple`. The server verifies the signature against
the provider's published keys, checks the issuer, the audience and the expiry, and then
keys the account on the provider's `(provider, subject)` pair. The token itself is never
stored and the provider is never called on the learner's behalf.

Set the client ids of every app that signs in, or that provider is not offered:

```bash
GOOGLE_CLIENT_IDS=1234567890-abc.apps.googleusercontent.com,1234567890-def.apps.googleusercontent.com
APPLE_CLIENT_IDS=app.natiqo.ios,app.natiqo.android
```

Both accept a comma-separated list or a JSON array, like `CORS_ORIGINS`. An id token
minted for a client id that is not listed is rejected, so a token lifted from an
attacker-controlled app cannot be replayed here. In production at least one of the two
must be set, and a request to a provider with no configured client ids answers `401`
`AUTH_PROVIDER_UNAVAILABLE` rather than trusting the token.

An email is only used to *link* a second provider to an existing account, and only when
the provider asserts the address is verified — otherwise an unverified address could be
used to take the account over. See the
[Mobile integration guide](docs/mobile-integration-ar.md) for the client side.

## Quality gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

## Demo data

Two seeders, deliberately separate: one owns **accounts**, the other owns the
**curriculum**. Merging them would mean a routine `--yes` to create a test learner could
also rewrite published course material. The account seeder additionally writes the
**learning history** an account needs for its screens to have something on them — that is
account data, so it belongs with the accounts rather than in a third command.

### The curriculum — `scripts/seed_content.py`

A complete six-level course, so the catalogue routes return a course rather than a
sample. 6 levels (A1–C2), 18 units, 37 lessons and 114 questions, every row published and
every question graded — multiple choice, true/false and fill-in-the-blank, spread across
all six tracked skills.

```bash
uv run python -m scripts.seed_content --dry-run        # show the plan, write nothing
uv run python -m scripts.seed_content --yes            # no interactive confirmation
uv run python -m scripts.seed_content --yes --include-drafts
```

The declaration is [scripts/seed_curriculum.py](scripts/seed_curriculum.py) and the
machinery is [scripts/seed_content.py](scripts/seed_content.py). Rows are built as domain
entities before anything is written, so a bad position or an unanswerable question fails
the script instead of becoming a row the catalogue serves.

**A slug is permanent.** Every id is `uuid5` of its slug path, so renaming a title is
free while renaming a slug orphans whatever progress is keyed on the old id. `--include-drafts`
adds one unpublished unit and one unpublished lesson for exercising the `404` paths; their
positions are computed from the declaration, because a literal one would collide with a
published sibling as soon as the course grows.

### The accounts — `scripts/seed.py`

Six demo accounts so the app has something realistic to render: each one sits on a
different branch — admin, learner, active, suspended, Arabic-speaking, different CEFR
levels and daily goals.

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

The report counts what the activity pass wrote on its own line, and names the breakdown
under the table: `admin: 16 sessions, 48 answers, 41 cards, 16 reviews, 2 threads,
5 achievements`. See [The learning history](#the-learning-history--the-same-command).

Every persona owns a **deterministic provider identity** instead of a password: the
seeder signs each one in through the real use case with a `seed:<persona>` subject on
the `google` provider. There is no demo credential to leak or reuse, and the identity is
what makes an account recognisably the seeder's on the next run. Never seed a real user
database: the seeder refuses to write in production without `--allow-production`.

### The learning history — the same command

A fresh account shows empty screens: no progress, no streak, no due reviews, no badges
unlocked, a tutor nobody has spoken to. So the same run also walks the **published course**
for the personas that declare an activity plan (the **admin** today) and writes what came
of it: fourteen finished lessons spread over nine days, a lesson left half answered, one
abandoned, forty-odd review cards with eight of them carrying a real SM-2 history, two tutor
threads, and the achievements those numbers earn.

What it deliberately does **not** do is decide anything the app decides:

* Every answer is graded by `quiz.domain.policy`, with the key from the catalogue, so the
  verdict and the points are the engine's — and a question the engine could not grade fails
  the seed instead of being recorded as a wrong answer.
* XP is the sum of those points, the schedule is SM-2 (`quiz.domain.review`), the streak is
  the shared calendar in `app.core.time`, the league and the badges are read with the same
  predicates the hub uses, and a tutor reply is the app's own fallback text. The plan picks
  *when* things happened and how well they went; it never picks what they were worth.
* The whole history is built as domain entities before a single row is written, which is
  why `--dry-run` reports the exact numbers the real run writes.

It is anchored to whatever is published, so run `scripts.seed_content` first (or accept an
account with no history, which is what an empty catalogue produces). A lesson that already
has a progress row — a real attempt, or a previous run — is left exactly as it is.

### Reruns are safe

Run it as often as you like. Accounts are matched by the seeded provider identity, and
their devices, usage history and learning history are written under deterministic ids, so a
second run updates the personas in place rather than creating duplicates — and it reports
zero changes. Run it twice and compare the reports if you want to see that for yourself.

The one case worth knowing about: if a run is interrupted,
`AuthService.login_with_identity()` commits each account as it is created, so a partial
run can leave an account with no profile behind it. Simply run the seeder again — it
finds the account, completes the missing rows, and leaves the rest untouched.

### It will not touch accounts it does not own

If an address already belongs to an account with no `seed:<persona>` identity behind it,
the seeder skips that persona and says so. It never links the seeded identity onto
somebody else's account, and it writes nothing at all for a persona it skipped.

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
| `POST` | `/api/v1/auth/google` | — | Sign in with a Google ID token, creating the account on first sight. Issues access + refresh tokens |
| `POST` | `/api/v1/auth/apple` | — | Sign in with an Apple ID token, same contract |
| `POST` | `/api/v1/auth/refresh` | refresh token | Rotate a refresh token; replay is rejected |
| `POST` | `/api/v1/auth/logout` | refresh token | Revoke this device, or `all_devices: true` |
| `DELETE` | `/api/v1/me` | Bearer | Close the caller's own account and revoke every session |
| `GET` | `/api/v1/me` | Bearer | Current profile and settings |
| `PATCH` | `/api/v1/me` | Bearer | PATCH semantics — omit a field to leave it alone. Accepts profile and settings fields under either spelling (see [Client contract](docs/client-contract.md)); deliberately refuses `currentLevel` so a learner cannot bypass the placement test |
| `GET` | `/api/v1/me/devices` | Bearer | List the caller's devices |
| `DELETE` | `/api/v1/me/devices/{device_id}` | Bearer | Revoke one of the caller's devices |
| `GET` | `/api/v1/content/levels` | — | Published CEFR levels, each with its unit count. Cacheable |
| `GET` | `/api/v1/content/levels/{level_code}/units` | — | Published units of one level. Cacheable |
| `GET` | `/api/v1/content/units/{unit_id}/lessons` | — | Published lessons of one unit. Cacheable |
| `GET` | `/api/v1/content/lessons/{lesson_id}` | — | A lesson and its questions, with **no answer key**. Cacheable |
| `GET` | `/api/v1/content/questions/{question_id}/explanation` | — | A single question's rationale, asked for after the learner has answered. Cacheable |

The five catalogue routes are deliberately unauthenticated: an app shell has to be able to
show that a beginner course exists before anyone signs in. That is safe only because the
question schema withholds `correct_index`, `accepted_answers` and `explanation` — the
practisable question is readable before an attempt, and the graded feedback, served only
by the standalone explanation route, after one.

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