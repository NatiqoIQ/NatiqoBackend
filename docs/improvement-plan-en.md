# Natiqo Backend — Improvement Plan (English)

> **Baseline measured:** 2026-10-15 · branch `main` · HEAD `290f792` ("v8").
> **Re-measured during this pass:** 2026-10-19 · HEAD `9ab3748` ("v11").
> **Scope measured:** the committed tree plus the working tree, both read directly.
>
> **Relationship to the other documents.** This is not a feature roadmap — that is
> [`backend-roadmap-ar.md`](backend-roadmap-ar.md) (what to build). This document is about
> *what has to be fixed underneath it first*. Every item below is preceded by evidence I
> measured from this tree, and closed by an acceptance criterion that is a command. An item
> with no evidence or no measurement does not belong here.
>
> **Arabic counterpart:** [`improvement-plan-ar.md`](improvement-plan-ar.md) (same plan,
> written first, in Arabic).

---

## 0. Summary — three findings

### Finding 1: the tree is red, and deploy does not wait for it 🔴

`ruff format --check app tests scripts` — the exact gate `ci.yml` runs — exits **1** on four
**committed** files:

| File | Lines |
| --- | --- |
| `app/modules/admin/application/content_service.py` | 116, 254, 375, 464, 735, 740, 744 |
| `app/modules/admin/application/users_service.py` | 69 |
| `app/modules/admin/domain/ports.py` | 53 |
| `app/modules/admin/infrastructure/repositories.py` | 325 |

Verified against the **committed blob**, not the working tree:
`git show HEAD:app/modules/admin/application/content_service.py | ruff format --check -` →
exit **1**. HEAD itself is red; this is not an uncommitted change.

These files were written *after* the phase-1 audit declared item M-2 "fixed" (twelve files,
then zero). The drift counter silently restarted.

Meanwhile `deploy.yml` publishes to production on every push to `main` with no `needs:` and
no `workflow_run`. ⇒ **A commit that fails CI reaches production.** That is audit item H-1,
and it is no longer a theoretical recommendation: it now has a live witness.

### Finding 2: the asynchronous layer is fully designed and connected to nothing 🟠

`app/platform/queue/` has `port.py` + `memory.py` + `redis.py`, `app/platform/push/` has
`port.py` + `fcm.py` + `logging_push.py`, and `app/api/deps.py` builds both and exposes them
as `QueueDep` / `PushDep`. But:

- **`enqueue()` is called nowhere** outside tests (`tests/unit/test_local_adapters.py`).
- **No module injects `QueueDep` or `PushDep`** — neither a producer nor a consumer.
- **`app/workers/main.py` is listed in `[tool.coverage.run] omit` and does not exist on
  disk.** A worker process was planned; the configuration arrived before the code.
- **`docker-compose.yml` has no worker service** — only `db`, `redis`, `app`.
- `JobName` declares eight job kinds (`SEND_NOTIFICATION`, `PROCESS_ANALYTICS`,
  `CLEANUP_EXPIRED_TOKENS`, `AI_TASK`, …) that nothing executes.

Direct consequence: the "receives a reminder notification" item in the original definition
of done is not verifiable today, and neither are phase 5 (notifications) or the asynchronous
promise of phase 7 (AI generation through `jobId`). Every component exists except the loop
that runs it.

### Finding 3: not one audit gap is measured in CI 🟡

| Audit gap | Fixed? | Evidence today |
| --- | --- | --- |
| H-1 remainder: gate deploy on CI | ❌ | `grep -c "needs:\|workflow_run" deploy.yml` → 0 |
| H-2: Postgres in CI | ❌ | `grep -c postgres .github/workflows/ci.yml` → **0** |
| M-1: adapter coverage | ❌ | `cache/redis.py`, `queue/redis.py`, `storage/s3.py`, `push/fcm.py` → **0%** |
| M-3: secret scanning | ❌ | `grep -c "gitleaks\|trufflehog" ci.yml` → **0** |
| M-4: staging environment | ❌ | no compose file, no separate branch |
| M-5: monitoring | ❌ | `otel_enabled` → **0** uses outside `config.py` |

The dangerous part is that these gaps are **invisible locally**: every gate passes on this
machine (see §1), and nothing in CI type-checks against Postgres or refuses a production
adapter that has never been executed.

---

## 1. Measured baseline

Every gate was run with the command CI runs, keeping the exit status:

| Check | Command | Result | Note |
| --- | --- | --- | --- |
| lint | `uv run ruff check .` | ✅ **EXIT=0** | all rules pass |
| format | `uv run ruff format --check app tests scripts` | 🔴 **EXIT=1** | 4 files · 205 formatted |
| types | `uv run pyright` | ✅ **EXIT=0** | 0 errors / 0 warnings |
| tests + cov | `uv run pytest -q --cov=app` | ✅ **EXIT=0** | full suite green |
| coverage | same command | ⚠️ **81%** | gate is 80% ⇒ **1%** of headroom |
| schema | `uv run alembic check` | ✅ **EXIT=0** | "No new upgrade operations detected" |
| migrations | `uv run alembic heads` | ✅ single head | `b13ae24dcdfd`, 14 revisions |

### Lowest-coverage files and what that means

| File | Coverage | Reading |
| --- | --- | --- |
| `app/platform/cache/redis.py` | **0%** | runs in production, never tested |
| `app/platform/queue/redis.py` | **0%** | same |
| `app/platform/storage/s3.py` | **0%** | same — the whole media path depends on it |
| `app/platform/push/fcm.py` | **0%** | same |
| `app/platform/ai/service.py` | 32% | budget and subscription limits |
| `app/platform/ai/providers.py` | 37% | provider integration |
| `app/platform/identity/jwks.py` | 33% | **sign-in path** — Google/Apple key fetch |
| `app/platform/identity/verifiers.py` | 48% | **sign-in path** — ID-token verification |
| `app/modules/gamification/application/service.py` | 51% | hearts/XP/streak logic |
| `app/modules/gamification/presentation/routes.py` | 65% | |

**The essential reading:** the suite clears 80% because tests substitute fakes for the real
adapters. So the gate is green while the code that **actually runs in production** has not
executed once in CI. 81% is an accurate number that does not mean what its reader assumes.

**One positive to lock in:** `alembic check` is completely clean. That is an achievement
that should become a gate rather than a coincidence (item B-2).

### Re-measured today (2026-10-19)

Two failures are live in the tree right now, independent of the four red files:

- `tests/api/test_endpoints.py` → the public question payload carries `videoUrl` while
  `PUBLIC_QUESTION_FIELDS` in the contract test does not, so the docs/API contract tests
  fail (`test_a_free_text_question_keeps_the_same_field_set` and its siblings).
- `tests/integration/test_seed_activity.py` → the whole module fails at seeding:
  `scripts/seed_activity.py` writes `multi_select` (and the other of the seven kinds) with
  no answer key, and the seeder refuses its own output.

And the suites that pass today: `tests/integration/test_quiz_engine.py`,
`tests/integration/test_progress_reads.py`, `tests/unit/` — all green.

---

## 2. Decisions taken, and why they are the contract

The plan below is not neutral. These choices close real exploits or real data losses, and
each one is now enforced in code rather than left to client behaviour:

| Decision | Reason |
| --- | --- |
| **Development sign-in is off by default, and production refuses to boot while it is on.** | Its credentials are fixed strings committed to the repository. An enabled backdoor on a public deployment is an open door for anyone who has read them. The 10-year token TTL made it worse. |
| **A checkpoint seals each question on its first answer.** | Retries exist so a learner can learn. Inside a graded gate they are only a way to fish for the pass, because the bar reads the sitting's final score. A checkpoint is re-sat whole, from a fresh sample, so nothing a learner is entitled to is taken away. |
| **XP is awarded for the first completion of a lesson only.** | The XP ledger backs a leaderboard and the gamification projection. A replay loop must not mint points; the replay is still recorded (attempt count, best score) but pays nothing. |
| **Hard delete is refused for anything that is not a draft.** | Once content leaves `draft`, rows are referenced by sessions, answers and progress. Deleting them rewrites a learner's history or strands a foreign key. `archived` is the supported retirement path, and `draft` is the only state that guarantees no learner ever saw the row. |
| **Fixes before features.** | Production is publicly exposed. Every hour spent on a new module is an hour the current surface stays unverified. |

---

## 3. Already landed and verified

| Change | Where | Proof |
| --- | --- | --- |
| Non-draft content cannot be hard-deleted (`CONTENT_NOT_EDITABLE`, 409) | `app/core/errors.py`, `app/modules/content/domain/policy.py`, `app/modules/admin/application/content_service.py` | API test asserts a published lesson's question delete is refused, and that a draft question is still deletable |
| Checkpoint answers are sealed on first attempt | `app/modules/quiz/application/quiz_service.py` | API test asserts the second answer is `409 QUESTION_ALREADY_ANSWERED`; `retryAvailable` is now explicitly "re-answerable in this sitting" and false for checkpoints |
| XP on first completion only | `quiz_service.complete_session` | API test asserts a replay leaves `totalXp` unchanged while `attempts` becomes 2 |
| Dev-login off by default; production refuses to boot with it on | `app/core/config.py` (`dev_login_enabled = False`, `validate_environment`) | `tests/unit/test_config.py::TestDevLogin` (off by default, production flags it, development may opt in) + `tests/api/test_dev_login.py` |

Verified by running the affected suites: `tests/api/test_endpoints.py -k "checkpoint or
replay or delete or question"` (8 passed), `tests/unit/`, `tests/api/test_dev_login.py`,
`tests/api/test_docs_contract.py`, `tests/integration/test_quiz_engine.py`,
`tests/integration/test_progress_reads.py`.

---

## 4. Phase A — put the tree back to green (hours)

1. **Format the four committed files** (`uv run ruff format app tests scripts`) — the drift
   must be zero before anything else is trusted.
2. **Gate deploy on CI.** Either start Deploy from the CI result, or add `workflow_run`
   with a `conclusion == 'success'` condition, plus Branch Protection on `main`. Without
   this, every other item in this document is advisory.
3. **Fix the two live red suites** measured today: the `videoUrl` public-field contract and
   the seeder that cannot seed its own seven question kinds.

**Acceptance:** `uv run ruff format --check app tests scripts` → exit 0;
`uv run pytest -q` → exit 0; a failing check cannot reach production.

---

## 5. Phase B — gates that lock in what is already correct (one working day)

1. **Postgres in CI** (fixes H-2 and 0.11 — the most dangerous operational gap). The suite
   has never run on the engine production uses.
2. **`alembic check` as a drift gate** — it is clean today; make it stay clean.
3. **Secret scanning** (`gitleaks`) in CI (fixes M-3).
4. **Unify the format scope**, or document the difference — one command, one truth.

**Acceptance:** CI has a Postgres 16 job running the same gates; `gitleaks` runs on every
push; `alembic check` is a required step.

---

## 6. Phase C — the worker that does not exist (3–5 days · blocks phase 5)

1. **A real worker loop** instead of definitions with no consumer: a process that reads the
   queue port, plus the compose service that runs it.
2. **Implement one job end to end** before building notifications —
   `CLEANUP_EXPIRED_TOKENS` is the natural first: it is bounded, testable and has no
   external dependency.
3. **Only then** phase 5 (notifications), which is indistinguishable from an unfinished
   feature until step 1 exists.

**Acceptance:** an enqueued job is executed by a running worker in `docker-compose up`, and
the test suite proves the enqueue → execute → side-effect chain.

---

## 7. Phase D — test what actually runs in production (2–3 days · fixes M-1)

1. **Contract tests for the adapters** (Redis cache/queue, S3, FCM) — exercised, not faked.
2. **Coverage priority: the sign-in path** (`identity/jwks.py`, `identity/verifiers.py`) —
   the entry point to every account, currently 33% and 48%.
3. **A coverage floor for critical files**, not just a global average that fake-heavy
   modules can carry.

**Acceptance:** every adapter at 0% today has at least one test that runs against the real
service in CI; the sign-in path is above its floor.

---

## 8. Phase E — monitoring (one day · fixes M-5)

1. **Sentry** (or the chosen equivalent), wired through settings, with the release marker.
2. **Decide the fate of `/metrics`** — either it exists and is scraped, or it leaves the
   checklist. A route that exists but is not collected is worse than absent.

**Acceptance:** an exception raised in a request is visible in the monitoring tool with the
request id; the readiness probe and the dashboard agree.

---

## 9. Phase F — the missing features (behind your decisions, not behind effort)

| Phase | State | Blocker |
| --- | --- | --- |
| 9 — vocabulary and grammar | ❌ `WORD_NOT_FOUND` / `GRAMMAR_TOPIC_NOT_FOUND` have no route | **no blocker** — reads plus a favourites write. Cheapest available phase, cleanest functional win |
| 5 — notifications | ❌ | needs phase C |
| 7 — remaining AI (`explain`, `drafts`, jobs) | `ai/quota` exists; the rest does not | needs C for the asynchronous parts |
| 4 — subscriptions | ❌ | **your decision** (the plan recommends skipping Stripe and keeping premium as limits/gems) |
| 10 — open decisions | — | eight decisions; the clearest are lesson-unlock rules, scoring, and the placement test |
| 6 — media | ✅ implemented | missing: `DELETE` for a media asset ⇒ orphaned objects with no path that frees them |
| M-4 — staging | ❌ | nothing blocks it technically: a `staging` branch with its own compose, or a second service |

**Recommended order:** phase 9 immediately after A and B — cheapest win, no decisions
required, no contact with the asynchronous layer.

---

## 10. Execution order and estimate

```
now (hours)     A-1 format → A-2 gate deploy on CI + Branch Protection
day 1           B-1 Postgres in CI      (highest operational value)
day 1           B-2 alembic check · B-3 gitleaks · B-4 unify the format scope
day 2-3         D-1 adapter contracts · D-2 sign-in path tests
day 3           E-1 Sentry · E-2 settle /metrics
day 4-8         C-1 worker loop → C-2 CLEANUP_EXPIRED_TOKENS → C-3 notifications
after that      F-9 vocabulary/grammar (cheapest win) → remaining AI → payment if you decide
```

**Estimated total: ~9 working days** until the tree is protected, everything that runs in
production is measured, and the asynchronous layer is alive.

**The ordering rule:** do not build a feature on an unverified foundation. Phases A and B
ship not one line of product — which is exactly why they come first: every phase after them
is measured by a gate that vouches for it.

---

## 11. Acceptance criteria (the single end gate)

```bash
uv run ruff format --check app tests scripts    # EXIT=0
uv run ruff check .                             # EXIT=0
uv run pyright                                  # 0 errors
uv run pytest -q --cov=app --cov-fail-under=80  # EXIT=0
uv run alembic check                            # no drift
# + in CI: the same gates against Postgres 16, gitleaks, and a floor for adapters
```

The most important check is the one no command above shows: **does any of this survive the
next commit?** Every item in this plan is either pinned by an automated gate or it will
drift again — and we have already watched that happen once, in M-2.

---

## 12. Explicitly out of scope

- New features (phase F is separate, and its decisions are not technical).
- Architectural rework: the current split (`modules` + `platform`) is mature and coherent —
  there is no reason to touch it.
- Frontend / Flutter: a different repository.
- Dependency upgrades: no observable cause (`ruff check` clean, `pyright` clean).

---

## 13. Limits of this measurement

What I could **not** verify, stated rather than left implicit:

1. **A real Postgres:** all test measurements were on SQLite (which is gap H-2 itself).
   `alembic check` ran against a local Postgres, but the **test suite was never run on
   Postgres**. The 81% is an SQLite number.
2. **Real CI on GitHub:** every result comes from running the same commands locally. I did
   not push and did not trigger GitHub Actions, so the "red branch" claim is an inference
   from a local gate failing with the identical command — strong, but not a runner
   execution.
3. **`deploy.yml` / GitHub protection:** read from the files. Repository settings in the
   GitHub UI are not visible to me.
4. **Real adapters (Redis/S3/FCM):** never executed; coverage 0% per the report.
5. **The working tree is not clean, and it is moving.** Beyond the media work noted in the
   Arabic plan (+246 uncommitted lines), a role-model refactor was in flight *while this
   document was written* (`app/core/types.py`, `app/core/security.py`,
   `app/modules/users/domain/entities.py` all modified within the same minute). Coverage
   numbers will move when it lands, and the four red files can change if other work is
   running in parallel.

**Every number in §1 is reproducible with the command next to it. Any item in this plan
without a measurement should be rejected until it has one.**
