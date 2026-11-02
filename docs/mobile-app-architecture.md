# Natiqo mobile app — architecture, features and design blueprint

This document is for the mobile engineer building the **Natiqo** Flutter app. It answers
three questions the generated schema cannot answer on its own:

1. **How should the app be structured?** — layers, folders, state, navigation, storage.
2. **What are we building?** — every backend feature mapped to screens, with its
   readiness status (live today vs. planned backend phase).
3. **In what order?** — milestones that only depend on endpoints that exist or are
   scheduled, so no screen is ever built against a fictional API.

Sources of truth, in order:

| Source | Answers |
| --- | --- |
| `GET /openapi.json` | The wire contract. Generate models from it; never hand-copy fields. |
| [`README.md`](../README.md) § *API surface* | The operation inventory, kept next to the code. |

> **Reality check.** The learning loop is no longer "catalogue only". The attempt engine
> (lessons **and** unit checkpoints), sequential unlock, progress reads, spaced
> repetition, gamification, the daily challenge, the AI tutor, runtime feature flags and
> learner media uploads are all shipped. Still missing: subscriptions/billing,
> notification delivery (inbox and push), vocabulary & grammar, a placement test and
> `GET /me/export`. Every readiness label in §6 was read from the mounted routers, not
> from a roadmap.

---

## 1. System overview

```
┌───────────────────────────── Flutter ─────────────────────────────┐
│  UI (widgets)  ←  Bloc/Cubit  ←  Repositories  ←  Local DB (drift)│
│                                              ↖                    │
│                                    Dio + interceptors             │
└───────────────────────────────────┬───────────────────────────────┘
                                    │ HTTPS / JSON (camelCase)
                       ┌────────────▼─────────────┐
                       │  FastAPI backend         │
                       │  /api/v1/*               │
                       └───┬──────────┬────────┬──┘
                     PostgreSQL     Redis   S3 / /media
```

**What the backend gives you today — 36 learner-facing operations under `/api/v1`**
(plus `POST /auth/dev-login`, mounted only when `DEV_LOGIN_ENABLED` is true — off by
default and refused in production, so a local `.env` opts in).

```
Auth & account (11)                  Learning loop (3)
  POST   /auth/google                  POST   /quiz/sessions            (lesson | checkpoint)
  POST   /auth/apple                   GET    /quiz/sessions/{id}
  POST   /auth/refresh                 POST   /quiz/sessions/{id}       (op: answer|complete|abandon)
  POST   /auth/logout
  GET    /me                         Progress (3)
  PATCH  /me                           GET    /progress                 (summary + XP page)
  DELETE /me                           GET    /progress/unlocks         (curriculum-ordered map)
  GET    /me/devices                   GET    /progress/units           (unit checkpoints)
  DELETE /me/devices/{id}
  GET    /me/daily-challenge         Spaced repetition (4)
  POST   /me/daily-challenge/claim     GET    /review/due
                                       GET    /review/stats
Catalogue (public, no auth, 2)         GET    /review/preview
  GET    /content/tree                 POST   /review/cards/{id}/answer
  GET    /content/lessons/{id}

Gamification (5)                     AI tutor (4)
  GET    /gamification/summary         POST   /ai/explain
  GET    /achievements                 POST   /ai/tutor
  POST   /achievements/{code}/claim    GET    /ai/tutor/{id}
  GET    /streak                       GET    /ai/quota
  POST   /streak/freeze

Feature flags (1)                    Media — learner uploads (3)
  GET    /flags                        POST   /media/presign
                                       POST   /media/confirm
                                       GET    /media/{id}
```

Outside `/api/v1`:

```
GET /health  ·  GET /ready  ·  GET /metrics          (no auth, unversioned)
GET /media/...                                       (published curriculum aids: static,
                                                      same-origin, no auth, no /api/v1)
```

The `/media` static mount only exists when the deployment actually ships a media
directory; a build without one still serves the API. Treat a `404` there as "this aid is
not deployed", not as an error to retry.

**What does not exist yet:** subscriptions/billing, notification delivery (inbox and
push), vocabulary & grammar, a placement test, and `GET /me/export`. Any request to those
prefixes returns `404`. §6 maps each one to the phase that will unlock it. **Do not build
a screen for a feature that is still `404`.**

The `/admin/*` surface (30 operations: content authoring, users, feature-flag writes) is
live and web-only. Do not build it into the learner app.

---

## 2. Project structure

Feature-first: features own their code; `core/` owns cross-cutting machinery; nothing in
`core/` imports a feature.

```
lib/
  main.dart                     # bootstrap: DI, runApp
  app.dart                      # MaterialApp.router, theme, BlocObserver
  core/
    config/                     # env (dev/staging/prod), constants
    network/
      api_client.dart           # Dio instance, base URL, timeouts
      interceptors/
        auth_interceptor.dart       # attaches Bearer access token
        refresh_interceptor.dart    # single-flight refresh (§4.3)
        request_id_interceptor.dart # X-Request-ID generation + logging
        idempotency_interceptor.dart# one UUID per user action (§4.4)
        error_interceptor.dart      # envelope → typed Failure
      paginated_list.dart       # cursor page<T> model + next-page fetch
    error/
      failure.dart              # sealed Failure hierarchy
      failure_mapper.dart       # error envelope → Failure
    storage/
      token_store.dart          # secure storage (refresh) + memory (access)
      local_database.dart       # drift schema: cached catalogue, session id, settings
    router/
      app_router.dart           # go_router + auth redirect
      routes.dart               # path constants
    theme/                      # colors, typography, spacing tokens
  shared/
    widgets/                    # skeletons, error views, offline banner, retry
    pagination/                 # infinite-scroll controller over cursor pages
  features/
    auth/
      data/         # auth_repository, dto models, google/apple sign-in wrappers
      bloc/         # auth_bloc, auth_state, auth_event
      presentation/ # sign_in_screen, splash_screen
    catalogue/
      data/         # catalogue_repository, level/unit/lesson models
      bloc/         # catalogue_bloc (levels), unit_list_cubit, lesson_list_cubit
      presentation/ # levels_screen, units_screen, lessons_screen
    lesson/
      data/         # lesson_repository (catalogue endpoints, local cache)
      bloc/         # lesson_bloc, question_viewer_cubit
      presentation/ # lesson_screen, question_card.dart
    quiz/
      data/         # quiz_repository (start lesson/checkpoint, resume, answer, complete)
      bloc/         # quiz_bloc, checkpoint_launcher_cubit, session_resume_cubit
      presentation/ # quiz_screen, answer_feedback.dart, result_screen,
                    # checkpoint_intro_screen, checkpoint_result_screen
    progress/
      data/         # progress_repository (overview, unlock map, units, XP feed)
      bloc/         # progress_bloc, unlock_map_cubit, units_cubit
      presentation/ # home_screen (dashboard), progress_screen, lesson_map_screen,
                    # unit_map_screen, xp_history_screen
    review/
      data/         # review_repository (due queue, answer, stats, preview)
      bloc/         # review_bloc (due → answer → reschedule), review_preview_cubit
      presentation/ # review_screen, review_card.dart, review_stats_screen
    gamification/
      data/         # gamification_repository (summary, achievements, streak)
      bloc/         # hub_cubit, achievement_list_cubit, streak_cubit
      presentation/ # hub_screen, achievements_screen, streak_screen
    ai/
      data/         # ai_repository (explain, tutor thread, quota)
      bloc/         # tutor_bloc, explain_cubit, quota_cubit
      presentation/ # tutor_screen, explain_sheet.dart
    flags/
      data/         # flags_repository (GET /flags, cached for the session)
      bloc/         # flags_cubit (one read after sign-in)
    media/
      data/         # media_repository (presign → PUT → confirm), avatar upload
      presentation/ # avatar_picker.dart, pronunciation_recorder.dart (future answers)
    profile/
      data/         # me_repository
      bloc/         # profile_bloc
      presentation/ # profile_screen, devices_screen, edit_profile_sheet
    settings/
      data/         # settings_repository (PATCH /me)
      bloc/         # settings_bloc
      presentation/ # settings_screen, notification_prefs_sheet
    # --- future features, same shape (see §6) ---
    subscription/ notifications/ vocabulary/
```

**Per-feature layout rule:** `presentation` depends on `bloc`; `bloc` depends on
`data`; `data` depends on `core/network` + `core/storage`. Nothing imports downwards
across features — shared code moves to `shared/`.

---

## 3. Layers and data flow

```
Screen  ──►  Bloc/Cubit  ──►  Repository  ──┬──►  RemoteDataSource (Dio)
   ▲              │                         └──►  LocalDataSource  (drift)
   └──── state ───┘
```

- **Bloc/Cubit** holds UI state and orchestration only. It never imports Dio, never
  touches storage directly, never formats JSON.
- **Repository** decides remote-vs-cache, maps DTOs to domain models, and is the only
  layer that knows both sources.
- **Failure, not exceptions, crosses the bloc boundary.** Every call returns
  `Result<T>` or throws a typed `Failure`; screens switch over one sealed class:

```dart
sealed class Failure {
  const Failure(this.message, {this.code, this.requestId, this.details});
}
class NetworkFailure    extends Failure { }            // timeout / no connection
class AuthFailure       extends Failure { final String code; }
  // AUTH_TOKEN_EXPIRED, AUTH_TOKEN_INVALID, AUTH_TOKEN_REVOKED,
  // AUTH_REFRESH_INVALID, AUTH_REFRESH_REUSED, USER_DELETED,
  // AUTH_ACCOUNT_DISABLED, AUTH_PROVIDER_UNAVAILABLE, AUTH_IDENTITY_* …
class ForbiddenFailure  extends Failure { final String code; }        // 403
  // LESSON_LOCKED (sequential unlock), AUTH_FORBIDDEN, AUTH_ADMIN_REQUIRED
class ValidationFailure extends Failure { final List<FieldIssue> fields; } // 422
class RateLimitedFailure extends Failure { final Duration retryAfter; }    // 429
class NotFoundFailure   extends Failure { final String code; }             // 404
class ConflictFailure   extends Failure { final String code; }             // 409
  // QUIZ_ALREADY_COMPLETED, QUIZ_NOT_COMPLETED, QUESTION_ALREADY_ANSWERED,
  // QUIZ_SESSION_EXPIRED, CHECKPOINT_NOT_READY, IDEMPOTENCY_KEY_REUSED,
  // IDEMPOTENCY_IN_PROGRESS, ALREADY_CLAIMED, NO_FREEZES_AVAILABLE,
  // DAILY_CHALLENGE_ALREADY_CLAIMED, DAILY_CHALLENGE_DATE_CHANGED,
  // DAILY_CHALLENGE_NOT_STARTED, CONFLICT
class PremiumFailure    extends Failure { final String? purchaseUrl; }      // 402
class ServerFailure     extends Failure { }             // 5xx (incl. 503 DB error)
```

- **Dependency injection:** `get_it` registers data-layer services (Dio, stores,
  repositories) at bootstrap; blocs are provided per screen with
  `BlocProvider(create: ...)`. No service locator calls from widgets other than
  `context.read/watch`.

### Error envelope → Failure

Every non-2xx response is exactly:

```json
{ "error": { "code": "AUTH_TOKEN_EXPIRED", "message": "…",
             "details": { }, "requestId": "…" } }
```

`details` and `requestId` are omitted when empty/null. A `422` carries
`details.fields = [{ "field": "answer.choices", "issue": "…", "type": "…" }]`.

Map by **`code` first, HTTP status second.** `401` + `AUTH_TOKEN_EXPIRED` means
"refresh"; `401` + `AUTH_REFRESH_REUSED` means "revoke everything and sign in again";
`401` + `USER_DELETED` means the same, permanently. `402` is `PREMIUM_REQUIRED` (the
`details` carry `purchase_url` — open the paywall, do not show a generic error). `403` +
`LESSON_LOCKED` is the **unlock chain**, not a permission bug: re-read
`GET /progress/unlocks` and show what the learner must finish first. `409` from the
learning loop is a *state* condition, not a failure to retry blindly:
`QUIZ_ALREADY_COMPLETED`, `QUESTION_ALREADY_ANSWERED`, `ALREADY_CLAIMED` mean the UI is
out of date — refresh, do not retry. Never parse `message` for logic — it is human text
and may be localised later. Always keep `requestId` for the "report a problem" flow.

---

## 4. Network layer (Dio)

### 4.1 Client

```dart
final dio = Dio(BaseOptions(
  baseUrl: 'https://api.staging.natiqo.app/api/v1',
  connectTimeout: const Duration(seconds: 10),
  receiveTimeout: const Duration(seconds: 20),
  headers: { 'Accept-Encoding': 'gzip' },   // responses < 400 bytes skip gzip anyway
))
  ..addInterceptor(AuthInterceptor(tokenStore))
  ..addInterceptor(RefreshInterceptor(authRepository))
  ..addInterceptor(RequestIdInterceptor())
  ..addInterceptor(IdempotencyInterceptor())
  ..addInterceptor(ErrorInterceptor());
```

Rules the client must honour:

- Send `Authorization: Bearer <access token>` on every call except `/auth/google`,
  `/auth/apple`, `/auth/refresh`, `/health`, `/ready` and the whole `/content/*`
  catalogue. **Everything else — `/me`, `/quiz/*`, `/progress/*`, `/review/*`, `/ai/*`,
  `/achievements`, `/streak`, `/gamification/*`, `/flags`, `/media/*` — requires it**,
  because it is account data.
- **Delete `cursor` when it is null** — sending `cursor=` yields `422`. This is the
  single most common integration bug.
- No trailing slash. `/content/tree/` answers `307` and some clients drop the query
  string on redirect.
- Generate an `X-Request-ID` per request; the server echoes it — attach it to any
  error report.
- GZip is transparent; do not decompress manually.
- Media aids (`imageUrl`, `audioUrl`, `videoUrl`) are absolute-path `/media/...` URLs
  served **outside** `/api/v1`; resolve them against the origin, not the API base.
- On `429`, read `Retry-After` and surface it to the UI — do **not** auto-retry in a
  loop.

**Rate-limit scopes (per minute, per principal — not per IP):**

| Scope | Routes | Default |
| --- | --- | --- |
| auth | `/auth/google`, `/auth/apple`, `DELETE /me` | **120** |
| catalogue | `/content/*`, `GET /flags` | **1200** |
| quiz | `POST /quiz/sessions`, `POST /quiz/sessions/{id}` | **120** |
| progress | `/progress`, `/progress/unlocks`, `/progress/units` | **240** |
| review | `/review/*` | **240** |
| ai | `/ai/*` | **30** — tightest, a call can bill upstream |
| gamification | `/gamification`, `/achievements`, `/streak` | **240** |
| media | `/media/presign`, `/media/confirm`, `/media/{id}` | **120** |
| admin | `/admin/*` | 120 (staff only) |
| default | `GET/POST /me/daily-challenge*` | **120** |

The budget is keyed per **principal**, in this order: the signed-in user id, else a
SHA-256 digest of the presented access token, else the client IP. Two learners on one
device therefore do not share an allowance, and signing in does not spend the login
budget. Sign-in itself is per IP, which is what a credential-stuffing attempt hits.

Some routes declare **no limiter at all**: `POST /auth/refresh`, `POST /auth/logout`,
`GET/PATCH /me`, and the device routes. Do not poll them; there is no `Retry-After` to
save you from a loop you wrote.

A `429` is `RATE_LIMITED` with `Retry-After: 60` (whole-minute windows). If Redis is
down the API fails open and keeps serving, so an occasional `429` under load is a budget
being enforced, not a broken client.

### 4.2 Token storage

| Token | Where | Why |
| --- | --- | --- |
| `refreshToken` | `flutter_secure_storage` (Keychain / Keystore) | survives restarts, encrypted at rest |
| `accessToken` | memory only | 30-minute lifetime; losing it is free |
| `deviceId` | secure storage | stable key for `/me/devices` and rate-limit identity |

The token response is `{ accessToken, refreshToken, tokenType: "Bearer", expiresIn,
refreshExpiresIn }` with **`expiresIn = 1800` s (30 minutes)** and
**`refreshExpiresIn = 2_592_000` s (30 days)**. The server stores only a SHA-256 hash of
the refresh `jti`, so a leaked local DB row is not replayable — but the token itself is
still a 30-day credential: secure storage is not optional.

### 4.3 Single-flight refresh — the non-negotiable rule

Several requests can fail with `401` at the same moment (app resume, parallel screen
loads). If each fires its own `POST /auth/refresh`:

- the first succeeds and **rotates** the refresh token;
- the second presents the already-consumed token → server sees **reuse** →
  revokes the **entire token family** → the user is signed out of **every device**.

Implementation: one `Completer` shared by all concurrent 401s.

```dart
class RefreshInterceptor {
  Future<Completer<String>?> _inFlight;   // null when idle

  Future<String?> refreshOnce() {
    if (_inFlight != null) return _inFlight!.future;   // everyone waits here
    final c = Completer<String>();
    _inFlight = c;
    // ... call /auth/refresh exactly once, complete c with the new access token,
    //     clear _inFlight in finally ...
    return c.future;
  }
}
```

Flow: request → `401 AUTH_TOKEN_EXPIRED` → single-flight refresh → retry original
request once. If refresh fails with `AUTH_REFRESH_INVALID`/`AUTH_REFRESH_REUSED` →
clear both tokens → navigate to sign-in. **Never retry twice; never refresh in
parallel.** Also send `device` (`platform`, optional `pushToken`, `appVersion`) with the
refresh body, or a device's push registration never follows its token rotation.

### 4.4 Idempotency

The backend accepts an `Idempotency-Key` header on unsafe methods (POST/PUT/PATCH/DELETE):

- a repeat of the same key + same body replays the stored 2xx response for **24 hours**,
  with `Idempotent-Replay: true` on the response;
- the same key with a *different* body returns `409 IDEMPOTENCY_KEY_REUSED`;
- an identical request that is **still executing** returns
  `409 IDEMPOTENCY_IN_PROGRESS` with `Retry-After: 1` — wait a second and retry **the
  same key**, do not mint a new one;
- responses larger than 64 KiB are not stored; that request is simply not replayable.

Keys are namespaced per method + path + caller, so two screens cannot collide. Generate
one UUID per user action and reuse it **verbatim** on network retries. This matters on:

- `POST /quiz/sessions` — a lost response must replay the same `sessionId`, not open a
  second sitting (lesson **or** checkpoint).
- `POST /quiz/sessions/{id}` with `{"op":"answer"}` — a retry must not double-count.
- `POST /me/daily-challenge/claim` and `POST /achievements/{code}/claim` — a retry
  should read the recorded outcome, not a second conflict.

### 4.5 Caching

Every response is `Cache-Control: no-store` **unless** the route opts in:

| Surface | Headers | Client behaviour |
| --- | --- | --- |
| `GET /content/tree`, `GET /content/lessons/{id}` | `public, max-age=60, stale-while-revalidate=600` + `ETag` | cache body + ETag in drift; revalidate with `If-None-Match`; `304` means keep what you have |
| `GET /media/...` (static aids) | `public, max-age=604800, stale-while-revalidate=86400` | cache for the life of the install; keys are immutable |
| everything else | `no-store` (error responses never cache) | re-fetch; do not build a local truth mirror |

`GET /progress` is cached **server-side** for 60 seconds and invalidated on every
mutation, so a pull-to-refresh right after finishing a lesson is cheap — but the response
itself is still `no-store`, so the client must not persist it.

---

## 5. State management — Bloc / Cubit

**Choice: `flutter_bloc`.** Rule of thumb in this app:

- **Cubit** for single-flow screens: catalogue lists, lesson loading, settings form,
  the gamification hub, the units map, the AI quota, flags.
- **Bloc** for multi-event lifecycles: `AuthBloc`, `QuizBloc` (start → answer →
  feedback → next → complete), `ReviewBloc` (due → answer → reschedule), `TutorBloc`,
  future `SubscriptionBloc`.

### 5.1 Auth state machine

```
        signIn (google/apple token)
guest ─────────────────────────────► authenticating ──► authenticated
  ▲                                      │                     │
  │                                      ▼                     │ refresh failed /
  └────────── signOut ◄────────── sessionExpired ◄─────────────┘ token revoked
```

```dart
sealed class AuthState {}
class Guest extends AuthState {}
class Authenticating extends AuthState {}
class Authenticated extends AuthState { final Me user; }
class SessionExpired extends AuthState {}   // show sign-in, keep nothing
```

- `AuthBloc` is a **root-level** bloc (provided above `MaterialApp`), because the
  router redirect and the shell both depend on it.
- Emit `Authenticated` only after `GET /me` succeeds — the tokens alone do not prove
  the account is alive (`USER_DELETED`, `AUTH_ACCOUNT_DISABLED` arrive there).
- `AuthResponse` already carries the full `MeResponse` (`user`) beside the tokens, so
  sign-in and refresh can hydrate the profile without a second call; still refresh it
  on foreground.
- On cold start: restore refresh token → attempt `POST /auth/refresh` silently →
  `GET /me`. Do not flash the sign-in screen while this runs: keep the splash until
  the state is decided.
- After `Authenticated`, read `GET /flags` once (see §6.9); an absent flag is `false`.

### 5.2 Quiz state machine

`QuizBloc` owns one sitting end-to-end and is **the only place a score is decided** (by
reading the server's verdict, never by grading locally). It serves both modes:

```
        start(lessonId) | startCheckpoint(unitId)
idle ──────────────────────────────────────────► presenting ──► grading ──► feedback
        │                   ▲                                    │
        │                   └────────── next ─────────────────────┘
        │
        └── resume(sessionId) ──► (rebuild from GET /sessions/{id})
                                                              ▼
                                       complete ──► completed (scorePercent, passed?)
                                       abandon  ──► abandoned
                                       expiry   ──► expired (QUIZ_SESSION_EXPIRED)
```

- Persist `sessionId` (secure storage or drift) the moment `POST /quiz/sessions`
  returns; on relaunch, resume instead of starting a new session.
- `isComplete` on each graded answer is what enables the **Finish** action — never
  enable it on `answeredCount == totalCount` you computed yourself.
- A `409 QUESTION_ALREADY_ANSWERED` means the local view was stale: call
  `GET /quiz/sessions/{id}` and reconcile rather than surfacing an error toast.
- **Retry before finishing.** A missed question can be re-answered in a lesson while the
  session is open (worth half); once `op:complete` returns, the sitting is closed and
  any further answer is `409 QUIZ_ALREADY_COMPLETED`. A checkpoint takes no retries at
  all — it is re-sat as a whole.
- A checkpoint that failed is offered as "try again" (a fresh session, fresh sample);
  a lesson that had misses offers "retry the missed ones" **only before** finishing.

### 5.3 Review state machine

`ReviewBloc` runs the due queue. It reuses the exact answer contract the quiz uses, so it
can share the answer-feedback widgets:

```
        loadDue                      answer(cardId, payload)
idle ────────────► presenting ─────────────────────► grading ──► rescheduled
        │               ▲                                          │
        │               └────────── next due card ◄───────────────┘
        │
        └── empty (dueCount == 0) ──► nothingDue  (a success state, not an error)
```

- `GET /review/preview` gives just `dueCount`, for badging the Review tab without
  loading the queue.
- The answer response carries the new SM-2 schedule (`dueAt`, `intervalDays`,
  `repetitions`, `lapses`, `state`, `quality`) — show "next in 6 days" from that, never
  compute it.
- `GET /review/due?strict=true` turns an empty queue into `404 REVIEW_NOTHING_DUE`; the
  default (`strict=false`) returns an empty page. Either is a **success** state.

### 5.4 Unlock-chain state

One extra read, no client-side rule:

- `GET /progress/unlocks` gives per-lesson `locked`; `GET /progress/units` gives per-unit
  `locked` + `checkpointReady` + the checkpoint record.
- A locked tile is **disabled with an explanation** ("finish Unit 2's lessons"), never a
  dead button and never locally re-derived. The server is the only thing that decides
  whether a lesson or checkpoint may start.
- After any completion, invalidate both cubits — a passing checkpoint flips the very next
  unit open, and `unlockedUnitId` tells you where to navigate.

### 5.5 Server state vs. client state

| Kind | Owner | Example |
| --- | --- | --- |
| Server truth | repository + bloc | profile, catalogue, devices, quiz session, progress, units, review queue, achievements, flags |
| Session | `AuthBloc` | tokens, current user |
| UI/local | plain `Cubit` or `ValueNotifier` | scroll position, selected tab, form drafts |
| Cached server data | drift + `etag` | catalogue pages for offline reading |

Never mirror server data into `shared_preferences`. `GET /me` is the single source of
truth for profile and preferences (the server refuses `currentLevel` on `PATCH /me` on
purpose). A quiz session lives on the server: the client holds only its id and what it
has been told, and re-reads on resume. Progress, review and gamification are all derived
server-side; the client renders them, it never re-derives them.

### 5.6 Hygiene

- One `BlocObserver` in debug that logs transitions and errors with the current
  `requestId`.
- No `BuildContext` in blocs; navigation from blocs goes through a `NavigationCubit`
  or stream listened to by the router.
- Every bloc state carries `loading | loaded | Failure` explicitly — screens render
  three states, not two.

---

## 6. Feature map — screens ⇄ endpoints ⇄ readiness

Legend: **LIVE** = endpoint exists today · **PHASE n** = unblocked by backend phase *n* ·
**404 today**.

### 6.1 Accounts — LIVE

| Screen | Endpoints | Notes |
| --- | --- | --- |
| Splash / session restore | `POST /auth/refresh`, `GET /me` | silent; keep splash until decided |
| Sign-in (Google + Apple) | `POST /auth/google`, `POST /auth/apple` | **first sign-in creates the account** — no register screen and no forgot-password screen, ever |
| Home shell | `GET /me` | greeting, level chip, daily goal, days-on-app from `daysSinceSignup` (server-derived — never count from the device clock) |
| Profile | `GET/PATCH /me` | cannot edit `currentLevel`; omit a field to leave it untouched; unknown fields are `422` (`extra="forbid"`) |
| Avatar | `POST /media/presign` → PUT → `POST /media/confirm` → `PATCH /me { avatarKey }` | four steps, see §6.10; `avatarKey` is not set by the upload itself |
| Devices | `GET/DELETE /me/devices` | shows last-seen and `pushEnabled`; revoking a device revokes its sessions |
| Close account | `DELETE /me` | destructive: confirm dialog; immediate and permanent server-side; idempotent `204`; revokes every refresh chain |
| Sign out | `POST /auth/logout` | `allDevices: false` default; clears local tokens regardless |
| Dev sign-in (debug builds only) | `POST /auth/dev-login` | `admin`/`admin`; off by default, refused in production, 10-year tokens — a QA backdoor, never shown to learners |

`GET /me` shape: `id`, `email`, `status`, `isAdmin`, `emailVerified`, `createdAt`,
`daysSinceSignup`, and nested `profile` / `settings`:

- `profile`: `displayName`, `avatarKey`, `nativeLanguage`, `englishVariant`, `timezone`,
  `currentLevel`.
- `settings`: `locale`, `explanationLanguage`, `notificationPreferences`
  (`email`, `push`, `dailyReminder`, `streakWarning`, `reviewDue`, `weeklyReport`,
  `quietHoursStart`, `quietHoursEnd`), `dailyGoalMinutes`, `dailyGoalNewWords`.

`PATCH /me` accepts `displayName`, `avatarKey`, `nativeLanguage`, `englishVariant`,
`timezone`, `locale`, `explanationLanguage`, a **partial** `notificationPreferences`
object, `dailyGoalMinutes` (5–600) and `dailyGoalNewWords` (1–200). Send only changed
fields; the response is the full `MeResponse`.

Advisory fields on sign-in (`displayName`, `nativeLanguage`, `englishVariant`,
`timezone`, `locale`) apply **only when the account is created**; they never overwrite
an existing learner's choices. Send `device` (`platform: ios|android`, `appVersion`,
`pushToken`) on sign-in and refresh.

### 6.2 Content catalogue — LIVE

| Screen | Endpoint | Design notes |
| --- | --- | --- |
| Curriculum (levels → units → lessons) | `GET /content/tree` | public (no auth gate!) — usable pre-sign-in; nested, with `unitCount`/`lessonCount`/`questionCount`; one response, no pagination |
| Lesson detail | `GET /content/lessons/{id}` | questions **without** `correctIndex`, `acceptedAnswers`, `explanation` — plus optional `imageUrl`/`audioUrl`/`videoUrl` aids and `prompts` |

- Both are cacheable: `ETag` + `Cache-Control: max-age=60,
  stale-while-revalidate=600`. Cache the body + ETag in drift; revalidate with
  `If-None-Match` and serve `304` from cache.
- The tree returns the **whole** published hierarchy at once, each level carrying its
  units and each unit its lessons — the app renders the curriculum from a single cached
  response instead of walking three paginated lists.
- Rate limit 1200/min per principal — generous, but still surface `429`.
- Question kinds are `multiple_choice`, `multi_select`, `true_false`, `fill_blank`,
  `word_bank`, `ordering`, `matching`.
- The catalogue is for **reading**. It deliberately withholds what would reveal the key:
  an `ordering` question returns **no** `choices` (they are stored in the correct
  sequence), and a `matching` question returns its `prompts` but **no** `choices` (the
  bank is only safe once shuffled per attempt). The moment the learner wants to answer
  for real, hand off to the **quiz player (§6.3)**, which grades on the server and
  returns the explanation per answer.
- `imageUrl`/`audioUrl`/`videoUrl` are same-origin `/media/...` paths (not under
  `/api/v1`), cacheable for a week. A question that is unpublished or unknown is a `404`
  (`QUESTION_NOT_FOUND`), indistinguishable from a never-existed question on purpose.

### 6.3 Lesson quiz — LIVE (the attempt engine)

Three authenticated endpoints under `/quiz`; every session is scoped to the caller and
**graded on the server** — the answer key never reaches the client before an answer is
committed.

| Screen | Endpoints | Notes |
| --- | --- | --- |
| Lesson → "Start practice" | `POST /quiz/sessions` | `{ "mode": "lesson", "lessonId": "uuid" }` → `201` with the session and its questions **without** an answer key. Send an `Idempotency-Key` |
| Quiz player (resume) | `GET /quiz/sessions/{id}` | **resume after app kill** — session + questions + `answeredQuestionIds` |
| Answer / finish / give up | `POST /quiz/sessions/{id}` | one route, three verbs chosen by `op` |

Starting a lesson is **gated** by the unlock chain (§6.4): an unpublished lesson is
`404 LESSON_NOT_FOUND`, a shut unit or a lesson whose predecessor is not complete is
`403 LESSON_LOCKED`, and a published lesson with no questions is `404
LESSON_NOT_AVAILABLE`.

Wire shapes (camelCase; field names are exact):

```jsonc
// POST /quiz/sessions        → 201
{
  "session": { "id":"uuid", "mode":"lesson", "lessonId":"uuid", "unitId":null,
               "status":"active",
               "startedAt":"…", "expiresAt":"…", "completedAt":null,
               "totalCount":6, "answeredCount":0, "correctCount":0, "xpAwarded":0,
               "answeredQuestionIds":[] },
  "questions": [ { "id":"uuid", "position":0, "kind":"multiple_choice",
                   "skill":"vocabulary", "prompt":"…", "choices":["a","b","c"],
                    "points":1, "imageUrl":"/media/images/….jpg",
                    "audioUrl":"/media/audio/….mp3", "videoUrl":null, "prompts":[] } ]
}   // ← no correctIndex / explanation; a matching question fills "prompts" instead of
    // leaving it empty, and its "choices" are the bank, shuffled so no option faces its
    // own prompt. Every other kind serves "prompts": [].

// POST /quiz/sessions/{id}   — one route, the verb is chosen by op
{ "op":"answer", "questionId":"uuid", "answer": { "choice":"b" } }   // or { "text":"meet" }
// or  { "op":"complete" }        // or  { "op":"abandon" }
// multi_select sends every chosen option:
{ "op":"answer", "questionId":"uuid", "answer": { "choices":["a","b"] } }
// ordering sends the items back in the order the learner arranged them:
{ "op":"answer", "questionId":"uuid", "answer": { "order":["Hello!","How are you?","I am fine.","Goodbye!"] } }
// matching sends every pair the learner built, as "prompt => option"; their order is irrelevant:
{ "op":"answer", "questionId":"uuid",
  "answer": { "matching":["Monday => 1","Tuesday => 2","Wednesday => 3"] } }

// 200 (op=answer)
{ "op":"answer",
  "result": { "isCorrect":true, "correctAnswer":{"choice":"c"},      // only after answering
              "explanation":"…", "pointsAwarded":1,
              "answeredCount":1, "totalCount":6, "correctCount":1, "isComplete":false } }
// multi_select reports the full correct set (comma-joined, in the served order):
//   "correctAnswer": { "choices": "a, b" }
// ordering reports the correct sequence (arrow-joined, in the authored order):
//   "correctAnswer": { "order": "Hello! → How are you? → I am fine. → Goodbye!" }
// matching reports the correct pairing (double arrow, semicolon-separated, sorted):
//   "correctAnswer": { "pairs": "Monday ⇒ 1; Tuesday ⇒ 2; Wednesday ⇒ 3" }

// POST /quiz/sessions/{id}  { "op":"complete" } → 200
{ "op":"complete",
  "result": { "session": { … , "status":"completed", "xpAwarded":5 },
              "scorePercent":100, "wordsLearned":4,
              "passed":null, "unlockedUnitId":null,
              "correctQuestionIds":["uuid"], "missedQuestionIds":[],
              "retryAvailable":false } }
```

Client rules:

- **Questions arrive without `correctIndex`, `acceptedAnswers` or `explanation`.**
  Correctness only comes back per answer. Never expect a key in the session payload.
- **Submit answers one at a time, each as `{"op":"answer", …}` with an `Idempotency-Key`.**
  A network retry must not double-count.
- For a choice question, send the **choice's text** (`{"choice":"b"}` also accepts the
  zero-based index or the letter `a`–`f`); for `fill_blank` and `word_bank`, send
  `{"text":"…"}` (graded case- and whitespace-insensitively — a `word_bank`'s `choices`
  are the tiles to assemble from, not options to pick). For `multi_select`, send
  `{"choices":[…]}` — the full set the learner picked; grading is exact-set, so a partial
  pick is wrong. For `ordering`, send `{"order":[…]}` — every item exactly once, in the
  learner's chosen sequence; grading is element-wise and all-or-nothing, and the served
  order is **never** the correct one, so never replay it. For `matching`, send
  `{"matching":[…]}` — every prompt exactly once, each entry as `"prompt => option"` with
  the option's text as served; grading is set equality. Sending the wrong shape, an item
  not in the question, or an incomplete set is `422`, not a silent "wrong".
- **The session survives process death.** Persist `sessionId`; on relaunch call
  `GET /quiz/sessions/{id}` and skip exactly `answeredQuestionIds`.
- **Scoring is server-owned and "first try":** a correct answer on the **first attempt**
  is worth full points; a correct retry is worth **half** (integer division, rounded
  down). A **wrong** answer can be re-answered and overwrites the same answer slot, so a
  question counts once. A question already answered **correctly** is closed — re-answering
  it is `409 QUESTION_ALREADY_ANSWERED`; reconcile by re-reading the session.
- Replaying a lesson that is **already completed** earns **0 XP** (the first completion
  owns the ledger) — do not advertise XP for a replay.
- `complete` refuses a partial sitting with `409 QUIZ_NOT_COMPLETED`; enable **Finish**
  only when the last answer came back `isComplete: true`.
- The completion response also carries `correctQuestionIds`, `missedQuestionIds`,
  `wordsLearned` and `retryAvailable` (lesson only). `wordsLearned` is counted on the
  server — never sum question points or counts yourself.
- The session is a **lease**: it expires after 7200 s. Once expired, answering or
  completing returns `409 QUIZ_SESSION_EXPIRED`; start again. A completed or abandoned
  session can still be read back for its result.
- Rate limit **120/min** for the quiz scope — surface `429` with `Retry-After`.
- Session questions carry `imageUrl`/`audioUrl`/`videoUrl` aids; render them — they teach
  the item, they do not reveal the answer.
- **Finishing a lesson also seeds spaced-repetition cards** for the questions answered
  correctly (§6.6), and feeds XP/streak/achievements (§6.7) — all server-side, in the
  same transaction. The client does nothing extra.

Errors: `QUIZ_SESSION_NOT_FOUND` · `QUIZ_SESSION_EXPIRED` · `QUIZ_ALREADY_COMPLETED` ·
`QUIZ_NOT_COMPLETED` · `QUESTION_NOT_FOUND` · `QUESTION_NOT_IN_SESSION` ·
`QUESTION_ALREADY_ANSWERED` · `QUESTION_TYPE_UNSUPPORTED` · `LESSON_NOT_FOUND` ·
`LESSON_NOT_AVAILABLE` · `LESSON_LOCKED` (403).

**Not yet in the loop:** hearts. There is no `sessionHearts` column and no penalty on a
wrong answer, so do not draw a heart meter.

### 6.4 Unit checkpoints and the unlock chain — LIVE

The curriculum is **sequential**, and a unit's checkpoint is the gate that opens the next
unit. Both halves are enforced by the server: the read views colour the map, and the
engine refuses a sitting that the rule has not reached.

**The rule, in one place:**

- the **first published unit is always open**; every other unit opens only once its
  immediate predecessor has been **passed** (the chain is global, so the first B1 unit
  waits for the last A2 unit);
- inside an open unit the **first lesson is open**; every other lesson opens once its
  immediate predecessor is **completed**; a completed lesson stays open for review and
  re-taking.

| Screen | Endpoints | Notes |
| --- | --- | --- |
| Lesson map | `GET /progress/unlocks` | per-lesson `locked`, `state`, `isCompleted`, `isUnlocked`, record |
| Unit map / "checkpoint ready" | `GET /progress/units` | per-unit `locked`, `passed`, `bestScore`, `attempts`, `passedAt`, `lessonsTotal`, `lessonsCompleted`, `checkpointReady` |
| Checkpoint intro → start | `POST /quiz/sessions` | `{ "mode": "checkpoint", "unitId": "uuid" }` → `201`; send an `Idempotency-Key` |
| Checkpoint player | same quiz player (§6.3) | identical answer/complete contract, with checkpoint rules below |
| Checkpoint result | `op:"complete"` | `passed`, `unlockedUnitId`, `scorePercent` |

```jsonc
// POST /quiz/sessions   { "mode": "checkpoint", "unitId": "uuid" } → 201
// Same envelope as a lesson session, except:
//   "session": { "mode":"checkpoint", "lessonId":null, "unitId":"uuid", … }
//   and up to 12 questions sampled across the unit (every lesson is represented).
```

Checkpoint rules:

- **Three gates, three answers.** Unpublished/unknown unit → `404 UNIT_NOT_FOUND`.
  Predecessor not passed → `403 LESSON_LOCKED`. A published lesson in the unit is not
  complete → `409 CHECKPOINT_NOT_READY` with `details.lessonsRemaining` (every lesson
  must be finished before the gate can be sat). A unit with no questions →
  `404 LESSON_NOT_AVAILABLE`.
- **The sample is the server's.** It targets 12 questions, guarantees a question from
  every lesson that has one, and weights toward what the learner got wrong, lapsed or has
  due. The list is frozen into the session — resume serves the same questions in the same
  order.
- **A checkpoint seals every question on its first answer, right or wrong.** Re-answering
  any of them is `409 QUESTION_ALREADY_ANSWERED`. There is no in-sitting retry: the gate
  is re-sat from the start, which draws a fresh sample.
- **Passing is 80%.** `op:"complete"` returns `passed` and, on a pass, `unlockedUnitId`
  — the next unit in curriculum order, or `null` at the end of the course. A later failed
  attempt never takes back an earlier pass (`bestScore` and the first `passedAt` are
  kept).
- **A checkpoint earns no XP of its own** (`session.xpAwarded` is `0`) and does not count
  as a completed lesson. Its reward is the unlock. `retryAvailable` is always `false`;
  re-sitting means a new session.
- `wordsLearned` is still counted and still feeds the daily challenge's word count, but
  `lessonsCompletedToday` only counts lesson sittings.

### 6.5 Progress — LIVE

Completion **writes** durable progress (state `in_progress` at start, `completed` at the
end; `bestScore` keeps the maximum, `attempts` increments, `xpEarned` accumulates), and
three authenticated reads serve it back. Every figure is **derived** from what the
engine wrote; anything without a backing store is omitted rather than returned as a zero.

| Screen | Endpoints |
| --- | --- |
| Home / dashboard + XP history | `GET /progress?limit=&cursor=` |
| Lesson map | `GET /progress/unlocks` |
| Unit map / checkpoint standing | `GET /progress/units` |

`GET /progress` returns the summary fields **flattened** plus the first page of the XP
history under `xp`, so the dashboard is a single request. Summary: `totalXp`,
`lessonsCompleted`, `lessonsInProgress`, `lessonsStarted`, `correctCount`,
`answeredCount`, `accuracyPercent`, `wordsLearned`, `currentStreakDays`,
`longestStreakDays`, `lastActivityAt`. The streak is measured in the learner's own
timezone (profile `timezone`, `Asia/Baghdad` by default), not UTC.

The `xp` page is `{ items, meta: { limit, hasMore, nextCursor } }` where each row is
`sessionId`, `lessonId` (null for a checkpoint), `unitId`, `xpAwarded`, `wordsLearned`,
`correctCount`, `totalCount`, `scorePercent`, `completedAt`. Newest first, ordered by
`(completedAt, id)`. Cursor is opaque — echo `meta.nextCursor` back verbatim; an
abandoned sitting never appears.

`GET /progress/unlocks` → `{ items: [{ lessonId, unitId, levelCode, title, position,
state, locked, isCompleted, isUnlocked, bestScore, attempts, xpEarned, wordsLearned,
completedAt }], total }`: every *published* lesson in curriculum order
(level → unit → lesson position). Whole-map, not paginated. **`locked` is real now** —
it is the sequential rule from §6.4. A lesson never touched still returns a zeroed record
(`attempts: 0`, `xpEarned: 0`).

`GET /progress/units` → `{ items: [{ unitId, levelCode, title, position, locked, passed,
bestScore, attempts, passedAt, lessonsTotal, lessonsCompleted, checkpointReady }],
total }`: every published unit in curriculum order. `locked` = may I enter the unit;
`checkpointReady` = are its lessons all done. They answer different questions — send both
so the screen can offer "finish lesson 3", "sit the checkpoint" or "pass Unit 1 first"
without a third read.

All three are authenticated, scoped to the caller, and `Cache-Control: no-store`. They
share their own rate-limit scope (240/min). The summary behind `/progress` is cached
server-side for 60 s and invalidated by every mutation, so polling it is cheap — but do
not fan it out into per-lesson probes.

### 6.6 Spaced repetition — LIVE

Four authenticated endpoints under `/review`. Cards are **seeded automatically**: the
first time a learner answers a question correctly (lesson or checkpoint), a card is
created (state `new`, due one day later). The schedule is classic **SM-2**; the engine
produces only two grades — a correct answer is quality **5**, anything else is **2**.

| Screen | Endpoints | Notes |
| --- | --- | --- |
| Daily review | `GET /review/due?limit=&strict=` | `{ cards:[{ cardId, dueAt, state, intervalDays, repetitions, question }], hasMore, dueCount }`, soonest first; `dueCount` is the total owed |
| Review (answer) | `POST /review/cards/{id}/answer` | same body as a quiz answer; returns `isCorrect`, `correctAnswer`, `explanation`, `pointsAwarded`, plus the new schedule: `quality`, `dueAt`, `intervalDays`, `repetitions`, `lapses`, `state` |
| Review tab badge | `GET /review/preview` | just `{ dueCount }` |
| Review stats | `GET /review/stats` | `totalCards`, `dueToday`, `future`, `reviewedTotal`, `retentionPercent` |

Design notes:

- The question served in a due card is the **same answer-free projection** a live session
  uses — the key still only arrives after an answer. Reuse the quiz's question card and
  answer-feedback widgets.
- **"Nothing due today" is a success, not an error.** Render `EmptyView`, not `ErrorView`.
  `REVIEW_NOTHING_DUE` exists only for the `strict=true` caller.
- A card whose question was unpublished is silently skipped, so a card count can exceed a
  page's length — never assume `dueCount == cards.length`.
- Answering a card updates **the schedule only**: it does not add XP, progress or a
  daily-challenge contribution. `pointsAwarded` is the verdict's face value, not a
  ledger write. Do not show a "+XP" animation on review.
- Rate limit 240/min. Answering is a write but shares the review scope with the reads.

### 6.7 Gamification and the daily challenge — LIVE

Five authenticated endpoints. The hub, the achievement catalogue and the streak screen
are each a **single read**, so each is one round trip. Two more — the **daily challenge**
under `/me` — follow.

| Screen | Endpoints | Notes |
| --- | --- | --- |
| Hub (XP, gems, streak, league, badge tally) | `GET /gamification/summary` | `stats` (`xpTotal`, `gems`, `currentStreak`, `longestStreak`, `leagueTier`, `freezesAvailable`, `lastActivityDate`) + `lessonsCompleted`, `achievementsUnlocked`, `achievementsTotal` |
| Achievements | `GET /achievements`, `POST /achievements/{code}/claim` | `{ items:[{ code, titleKey, descriptionKey, iconKey, xpReward, gemsReward, target, progress, unlocked, unlockedAt, claimedAt, claimable }], total }`; `claimable` is the one bit a "claim" button keys off; the claim returns `{ achievement, xpAwarded, gemsAwarded }` |
| Streak detail | `GET /streak`, `POST /streak/freeze` | `{ currentStreak, longestStreak, lastActivityDate, freezesAvailable, premium }`; `premium` tells you whether freezing is offered **before** the tap |

- **League tiers** are read from lifetime XP and travel as **lowercase strings**:
  `bronze` (0), `silver` (100), `gold` (500), `platinum` (1500), `diamond` (4000).
  Do not uppercase them and do not switch on an enum name.
- **Achievements** are a small code-owned catalogue: `first_lesson`, `ten_lessons`,
  `twenty_five_lessons`, `xp_century`, `xp_veteran`, `streak_three`, `streak_week`,
  `streak_month`. Titles/descriptions/icon are **translation keys**
  (`achievement.first_lesson.title`, …) — translate in the app, do not ship English text.
- `ALREADY_CLAIMED` (409) means refresh the list, not an error toast; an unknown or
  not-yet-unlocked code is `404 ACHIEVEMENT_NOT_FOUND`.
- `POST /streak/freeze`: a non-premium deployment answers `402 PREMIUM_REQUIRED` with
  `details.purchase_url`; no freezes left is `409 NO_FREEZES_AVAILABLE`.
- The streak advances on **lesson completions** in the learner's calendar (longest run
  is never revoked by a later gap). A checkpoint does not extend it.
- Rate limit 240/min for the gamification scope.

**Daily challenge — two authenticated endpoints under `/me`.** One entry per learner per
calendar day, counting the sittings finished that day and claimable once. Progress is
**derived from those sittings, not reported by the client**: completing a quiz records
it, so there is nothing for the app to send.

```jsonc
// GET /me/daily-challenge   → 200   (claim returns the same shape)
{ "challengeDate": "2026-10-26", "lessonsCompletedToday": 2,
  "wordsLearnedToday": 5, "claimed": false }
```

| Screen | Endpoints | Notes |
| --- | --- | --- |
| Daily challenge card (hub or home) | `GET /me/daily-challenge` | computed on read — a learner who has done nothing yet reads zeros, never a 404 |
| Claim today's reward | `POST /me/daily-challenge/claim` | optional body `{ "expectedChallengeDate": "2026-10-26" }` guards against claiming the wrong day |

- **The day boundary is the learner's.** `challengeDate` comes from the profile timezone
  (`Asia/Baghdad` by default), so the card rolls over at the learner's midnight. Display
  the server's `challengeDate`; never build the challenge day from the device clock.
- `lessonsCompletedToday` counts **lesson** completions only; a checkpoint contributes
  words but not lessons. `wordsLearnedToday` is first-time-correct vocabulary across the
  day's sittings.
- A second claim is `409 DAILY_CHALLENGE_ALREADY_CLAIMED`; a day with no recorded sitting
  is `409 DAILY_CHALLENGE_NOT_STARTED`; a card from another day is
  `409 DAILY_CHALLENGE_DATE_CHANGED` with the authoritative date in
  `details.challengeDate` — re-render from it, do not retry the same body.
- **Claim only sets the flag today** — no gems or XP are awarded yet, so render a claimed
  *state*, not an award animation.
- Both fall back to the default `/me`-class budget (**120/min**), not the gamification
  scope.

### 6.8 AI tutor — LIVE, but disabled by default

Four authenticated endpoints under `/ai`. **The module is enabled per deployment**
(`ai_enabled`, off by default). When it is off, over the learner's daily budget, over the
**platform-wide daily budget**, or the provider errors/times out, `explain` still returns
the **authored** explanation and `tutor` still returns a deterministic reply — so the UI
never has an "AI is down" branch to write. Every AI-flavoured response carries `source`
(`"ai"` | `"authored"` | `"unavailable"`): render provenance honestly rather than
mislabelling a fallback as model output.

| Screen | Endpoints | Notes |
| --- | --- | --- |
| "Explain this answer" | `POST /ai/explain` | body `{ questionId, answer }`; returns `isCorrect`, `correctAnswer`, `explanation`, `source`. The verdict is computed on the server, exactly as `/quiz` does |
| Tutor chat | `POST /ai/tutor`, `GET /ai/tutor/{id}` | omit `conversationId` to open a thread, pass it to continue; persisted, so it is still there on return. `lessonId` is an optional anchor. Reply is `{ conversationId, reply, source }` |
| Thread read-back | `GET /ai/tutor/{id}` | `{ id, lessonId, messages:[{ id, role, content, createdAt }] }`, oldest first |
| Quota display | `GET /ai/quota` | `enabled`, `allowed`, `limitUsd`, `spentUsd`, `remainingUsd`, `reason` — warn before it runs out |

- **AI never grades.** Points, schedules and scores come only from the attempt engine's
  policy. `explain` is a convenience on top of it.
- `quota` is the **effective** gate: `allowed: false` means the next call falls back,
  and `reason` is a human sentence (per-user budget vs. the platform's daily budget).
  `limitUsd: 0` means unmetered; do not divide by it.
- `AI_QUOTA_EXCEEDED`, `AI_TIMEOUT`, `AI_PROVIDER_ERROR`, `AI_DISABLED` and
  `AI_UNSAFE_INPUT` are normal outcomes — degrade gracefully. An unknown
  `conversationId` is `404 CONVERSATION_NOT_FOUND`. A tutor message is capped at 4000
  characters at the edge.
- Rate limit **30/min** — the tightest scope, because a call can bill upstream. Do not
  poll it.

### 6.9 Feature flags — LIVE

`GET /flags` is authenticated and returns the flags **evaluated for this caller**:

```jsonc
{ "flags": { "ai_tutor_entry": true, "new_onboarding": false } }
```

- Only flags an operator marked `public` are returned; internal kill switches never
  reach a client. **An absent key means off** — never assume a default.
- The rollout percentage is applied server-side, so the app does not reimplement
  bucketing. The same learner keeps the same answer.
- Read it once after sign-in (and on foreground), cache it for the session, and use it to
  hide — not grey out — surface-level entries. A flag is **not** an authorisation check:
  the API still refuses what the learner may not do.
- Scope: catalogue budget (1200/min); response is `no-store` like everything else.

### 6.10 Media — LIVE

Two different things share the name.

**Catalogue aids already work today:** question `imageUrl`/`audioUrl`/`videoUrl` are
public, same-origin `/media/...` files (§6.2), and the quiz session and review card serve
them too — read them directly and cache them for the week the headers allow.

**Learner uploads are live:** a three-step direct-to-storage flow. The bytes never pass
through the API.

| Step | Endpoint | Notes |
| --- | --- | --- |
| 1. Grant | `POST /media/presign` | body `{ "kind": "avatar"\|"image"\|"audio"\|"pronunciation", "contentType": "image/jpeg" }` → `{ mediaKey, uploadUrl, requiredHeaders, contentType, kind, expiresInSeconds }` |
| 2. Upload | `PUT uploadUrl` (S3, not the API) | send **exactly** `requiredHeaders` (the signature was computed over the `Content-Type`) |
| 3. Confirm | `POST /media/confirm` | body `{ mediaKey }` → `201 { id, kind, contentType, sizeBytes, checksumSha256, url, expiresInSeconds, createdAt }`; idempotent |
| Read back | `GET /media/{id}` | any authenticated caller; returns a freshly signed `url` (15-minute TTL) |

- Kinds and limits: `avatar` (JPEG/PNG/WebP), `image` (+GIF), `audio` and
  `pronunciation` (MPEG/MP4/WAV/OGG/AAC). Ceilings are **5 MiB** for images and **10 MiB**
  for audio, measured from the stored object.
- The server **sniffs** the real type from the bytes at confirm and deletes a rejected
  upload. A mislabelled file fails with `MEDIA_TYPE_NOT_ALLOWED` / `MEDIA_UPLOAD_FAILED`;
  an unknown key or id is `MEDIA_NOT_FOUND`.
- A confirmed avatar is not applied by itself: `PATCH /me { "avatarKey": mediaKey }` is
  the separate step that sets it.
- Signed URLs expire — never persist one; persist the `id`/`mediaKey` and re-read.
- Rate limit 120/min for the media scope. Relevant for speaking/pronunciation answers
  later; do not stream audio through the API server.

### 6.11 Subscriptions & billing — PHASE 4 (not built)

| Screen | Endpoints |
| --- | --- |
| Paywall / plans | `GET /plans` |
| Current subscription | `GET /subscription` |
| Checkout | `POST /subscription/checkout` → open the returned Stripe URL |
| Cancel | `POST /subscription/cancel` |
| Billing portal | `POST /subscription/portal` |

Today the only "premium" that exists is the **streak-freeze gate** in §6.7, which returns
`PREMIUM_REQUIRED` with `details.purchase_url`. There is no per-account plan and no
client-side "is premium" flag to build: when the endpoints land, read `GET /subscription`
on start and after every purchase flow. The webhook (Stripe → server) is the source of
truth; the app only reflects it.

### 6.12 Notifications — PHASE 5 (not built)

| Screen | Endpoints |
| --- | --- |
| Inbox | `GET /notifications?cursor=`, `POST /notifications/{id}/read`, `POST /notifications/read-all`, `DELETE /notifications/{id}` |
| Badge | `GET /notifications/unread-count` (poll lightly or refresh on foreground) |
| Push token registration | FCM/APNs token goes in `device.pushToken` on sign-in / refresh (the `device` object already accepts it today) |

Preference switches already exist under `PATCH /me` (`notificationPreferences` —
`email`, `push`, `dailyReminder`, `streakWarning`, `reviewDue`, `weeklyReport`,
`quietHoursStart/End`): build the toggles now, wire them to `/me`; delivery itself waits
for phase 5. These switches are **part of `GET /me`**, not a separate store. The server
has push adapters but no delivery path yet, so a `pushEnabled` device is registered, not
necessarily messaged.

### 6.13 Vocabulary & grammar — PHASE 9 (not built)

`GET /vocabulary`, `GET /vocabulary/{word}`, `POST /vocabulary/{word}/favorite`,
`GET /grammar/topics`, `GET /grammar/topics/{id}`.

### 6.14 Not for the phone app

- **Admin content authoring** (`/admin/*`) — web only, gated by `isAdmin`. Do not build
  it into the learner app, even for staff.
- **Placement test** — no endpoint exists; `currentLevel` is therefore read-only in the
  app until it does.
- `GET /me/export` — not built yet; when it lands, a "download my data" row in Settings
  that triggers a share sheet is enough.

---

## 7. Navigation (go_router)

```dart
final router = GoRouter(
  initialLocation: '/splash',
  redirect: (context, state) {
    final auth = context.read<AuthBloc>().state;
    final signingIn = state.matchedLocation == '/sign-in';
    if (auth is Guest && !publicRoutes.contains(state.matchedLocation)) {
      return signingIn ? null : '/sign-in';
    }
    if (auth is SessionExpired) return '/sign-in?reason=expired';
    return null;
  },
  routes: [
    GoRoute(path: '/splash', ...),
    GoRoute(path: '/sign-in', ...),
    StatefulShellRoute.indexedStack(        // learner shell
      branches: [
        GoRoute(path: '/learn', ...),       // levels → units → lessons
        GoRoute(path: '/review', ...),      // LIVE — the SRS queue of §6.6
        GoRoute(path: '/progress', ...),    // LIVE — dashboard + maps (§6.5)
        GoRoute(path: '/profile', ...),
      ],
    ),
    GoRoute(path: '/lesson/:id', ...),
    GoRoute(path: '/checkpoint/:unitId', ...), // LIVE — intro, then starts a session (§6.4)
    GoRoute(path: '/quiz/:sessionId', ...),    // LIVE — the quiz player of §6.3
    GoRoute(path: '/gamification', ...),       // LIVE — hub / achievements / streak (§6.7)
    GoRoute(path: '/tutor', ...),              // LIVE (AI, deployment-dependent) (§6.8)
  ],
);
```

- **Public routes:** `/splash`, `/sign-in`, `/learn` catalogue browsing. The catalogue
  is deliberately unauthenticated — a new learner can see the course before signing in.
- **The Review tab is live** — badge it from `GET /review/preview`; **hide** (do not
  grey out) any tab for a feature that is still `404`. The quiz player is not a tab: it
  is pushed from a lesson or a checkpoint.
- A checkpoint is entered from the **unit map**, never from a lesson tile, and only when
  `checkpointReady && !locked` (or when re-sitting a failed one). After a pass, navigate
  to `unlockedUnitId` when it is non-null.
- Deep link `natiqo://lesson/{id}` → resolve through the same router; guard with the
  auth redirect so a cold deep link cannot bypass sign-in. `natiqo://quiz/{sessionId}`
  must be auth-guarded too, and should resume via `GET /quiz/sessions/{id}`.

---

## 8. Screen design standards

Every screen implements the same five states — build them once in `shared/widgets`:

| State | Widget | Rule |
| --- | --- | --- |
| Loading | `SkeletonList` / `SkeletonDetail` | shape-matched placeholders, never a bare spinner for lists |
| Loaded | the content | |
| Error | `ErrorView(message, requestId, onRetry)` | retry button; show `requestId` behind a "details" affordance |
| Empty | `EmptyView` | distinct from error — "no reviews due" is success |
| Offline | `OfflineBanner` + cached content | catalogue reads from drift when the network fails |

**Per-screen guidance:**

- **Sign-in:** two buttons (Google, Apple) only. No email field, no password field, no
  "forgot password" link — they can never exist in this system. Show a loading state
  *inside* the tapped button; surface `AUTH_PROVIDER_UNAVAILABLE` as "sign-in with X
  isn't available yet".
- **Catalogue:** render levels → units → lessons from the single cached
  `GET /content/tree` response, with counts on each tile. Keep scroll position on back
  (`ValueKey` per route + `AutomaticKeepAlive`). A tile's **lock** comes from the
  progress map, not the catalogue — merge the two on the lesson map screen.
- **Lesson map / unit map:** draw `locked` tiles as disabled with the reason; show
  `bestScore`, `attempts` and `xpEarned` straight off the map. On the unit map, the
  checkpoint tile has three states: `locked` (pass the previous unit), "finish N lessons",
  and "sit the checkpoint" (or "passed · best 90%" with a re-sit action). Never compute
  `checkpointReady` yourself.
- **Lesson reader:** question card with choice buttons / true-false / text field / tile
  bank per `kind`, showing the `imageUrl`/`audioUrl`/`videoUrl` aid when present. An
  `ordering` question has **no `choices` in the catalogue**; a `matching` question shows
  its `prompts` but **no `choices`**. The reader defers both to the quiz player, which
  serves the shuffled, answerable columns. The catalogue is for **reading**; the moment
  the learner wants to answer for a result, hand off to the quiz player — never grade
  locally.
- **Quiz player:** one question at a time, with a progress indicator
  (`answeredCount`/`totalCount`). Render `multi_select` as checkboxes that collect a set
  before one submit, `ordering` as a reorderable list, and `matching` as two columns —
  `prompts` fixed on the left, the `choices` bank on the right — submitted once every
  prompt has a pair. Never pre-select a pairing from the two lists' positions: the server
  guarantees no option is served opposite the prompt it answers. The others submit on
  tap. Disable the choices while an answer is in flight; show the verdict, correct answer
  and explanation in place; advance on the learner's tap. Persist `sessionId` so an app
  kill resumes. Enable **Finish** only when the server says `isComplete`. On
  `QUIZ_SESSION_EXPIRED`, offer "start again"; on `QUESTION_ALREADY_ANSWERED`, re-read
  the session instead of erroring.
- **Checkpoint intro:** name the unit, state the bar (**80%**), state that answers are
  final for the sitting, and that every lesson in the unit must be finished (the server
  enforces it — handle `CHECKPOINT_NOT_READY` with the count from `details`). No timer;
  the session lease is invisible until it expires.
- **Checkpoint result:** show `passed` + `scorePercent`; on a pass, celebrate the unlock
  and offer "go to the next unit" (`unlockedUnitId`); on a fail, offer "try again"
  (a new session) — never "retry the missed questions".
- **Result screen:** show `scorePercent` from the `complete` response — never recompute
  it. Do not draw a hearts meter.
- **Review:** reuse the quiz question card and feedback widgets — the contract is
  identical. Show the returned next-due ("next in 6 days"). An empty queue is
  `EmptyView`.
- **Gamification hub:** one `GET /gamification/summary` fills the whole screen. Draw
  achievement bars from `progress`/`target`; enable a claim button only when `claimable`;
  treat `ALREADY_CLAIMED` as "refresh". On the streak screen, show the paywall when
  `premium` is false **before** the freeze tap, and open `purchase_url` on `402`.
- **AI surfaces:** render `source` honestly ("AI" vs. "from the lesson"). Design for
  `AI_QUOTA_EXCEEDED`/`AI_TIMEOUT` as normal, not fatal — the authored explanation is
  always available. The tutor is rate-limited to 30/min: no polling.
- **Profile:** `currentLevel` read-only; `PATCH` sends only changed fields — never send
  the whole object back. Avatar: presign → PUT → confirm → PATCH `avatarKey`, with a
  progress state on each step.
- **Destructive actions** (revoke device, close account): confirm dialog that names
  the consequence; `DELETE /me` is immediate and permanent.

**Design tokens:** one `ThemeExtension` for brand color, spacing scale, and type
scale; both locales (`en`, `ar`) with RTL mirroring from the start (`Directionality`
driven by `locale`), because half the audience reads Arabic.

---

## 9. Delivery milestones

Each milestone is shippable and depends only on endpoints that exist (or on a named
phase).

| # | Scope | Depends on | Status |
| --- | --- | --- | --- |
| **M0** | Project scaffold: folder structure, theme, router, DI, Dio + interceptors (auth, single-flight refresh, request-id, idempotency, error mapping), token store, `AuthBloc` state machine, CI lint+test | — | **buildable now** |
| **M1** | Sign-in (Google/Apple), session restore, shell, catalogue tree + lesson detail with ETag cache, flags read | live endpoints | **buildable now** |
| **M2** | Lesson reader, profile (view/edit), devices, settings + notification preference toggles, close account, avatar upload | live endpoints | **buildable now** |
| **M3a** | **Lesson quiz (the core loop):** start / resume / answer / complete / abandon, server-graded feedback, session resume across app kill | live quiz endpoints (§6.3) | **buildable now** |
| **M3b** | Home dashboard, XP history, progress tab, daily-challenge card | live progress reads + `/me/daily-challenge` (§6.5, §6.7) | **buildable now** |
| **M3c** | **Unlock chain + unit checkpoints:** lesson map, unit map, checkpoint intro/player/result, "next unit" navigation | live gating + checkpoint endpoints (§6.4) | **buildable now** |
| **M4** | Daily review (SRS) queue, review tab + badge, review stats | live review endpoints (§6.6) | **buildable now** |
| **M5** | Gamification hub, achievements (with claim), streak detail + freeze gate | live gamification endpoints (§6.7) | **buildable now** |
| **M6** | AI tutor chat + "explain this answer" + quota, behind a feature flag | live AI endpoints (§6.8) | **buildable now** |
| **M7** | Notification inbox + FCM/APNs push delivery | **Phase 5** | blocked |
| **M8** | Paywall, checkout, subscription management | **Phase 4** | blocked |
| **M9** | Vocabulary & grammar sections | **Phase 9** | blocked |

**Parallel work while blocked (M7–M9):** finish M0–M6 to production polish; build the UI
for the blocked screens against fixture JSON with the repository interface already
defined — swapping fixtures for HTTP later is a one-file change — but do **not** ship
dead screens.

---

## 10. Testing and quality gates

```bash
flutter analyze --fatal-infos      # very_good_analysis
flutter test                       # unit + widget
flutter test --coverage            # fail-under 70% for features/, 0 for generated
```

| Layer | Tool | What to test |
| --- | --- | --- |
| Bloc/Cubit | `bloc_test` | every state transition incl. Failure branches |
| Repositories | `mocktail` + fake Dio (`http_mock_adapter`) | envelope → Failure mapping, cursor handling, cache fallback |
| Interceptors | pure unit tests | **single-flight refresh**: N concurrent 401s → exactly 1 refresh call; retry-once; no retry after `AUTH_REFRESH_REUSED`; `IDEMPOTENCY_IN_PROGRESS` retries with the same key |
| Quiz player | `bloc_test` + fake Dio | lesson: start → answer → feedback → next → complete; resume rebuilds from `answeredQuestionIds`; retry allowed only while open; `isComplete` gates Finish; `QUIZ_SESSION_EXPIRED`, `QUESTION_ALREADY_ANSWERED`, `LESSON_LOCKED` handled |
| Checkpoints | `bloc_test` + fake Dio | `mode:"checkpoint"` body; `CHECKPOINT_NOT_READY` shows `lessonsRemaining`; sealed answer → `QUESTION_ALREADY_ANSWERED`; `passed` vs. failed result; `unlockedUnitId` navigation; no "retry missed" action |
| Unlock map | `bloc_test` + fake Dio | `locked` disables tiles; `checkpointReady` vs. `locked` kept distinct; maps invalidated after a completion |
| Review | `bloc_test` + fake Dio | due → answer → rescheduled; empty queue is a success; `strict=true` maps `REVIEW_NOTHING_DUE` to the empty state; no XP animation |
| Gamification | `bloc_test` + fake Dio | hub from one read; `ALREADY_CLAIMED` refreshes; `PREMIUM_REQUIRED` opens the paywall; `NO_FREEZES_AVAILABLE` is graceful; lowercase league tiers |
| AI | `bloc_test` + fake Dio | `source` rendered honestly; `AI_QUOTA_EXCEEDED`/`AI_TIMEOUT` degrade, not crash; `limitUsd: 0` is not a division |
| Flags | `bloc_test` | absent key = off; one read per session; hidden entry, not a permission check |
| Widgets | `flutter_test` + golden | the five screen states; RTL layout |
| E2E | `integration_test` against staging | sign-in → browse → lesson → **start a quiz, answer every question, see the score** → **pass a unit checkpoint and reach the next unit** → **answer a due review card** → **open the hub**; token expiry across 30 min (fast-forward via injected clock) |

Rules:

- Generate models with `freezed` + `json_serializable` **from `openapi.json`** —
  hand-written DTOs drift from the wire and the server does not negotiate.
- Never assert on `message` text; assert on `code`.
- CI (Codemagic or GitHub Actions): analyze → test → coverage; block merges on red.

---

## 11. Performance checklist

- `ListView.builder` (or `ListView.separated`) for every remote list — the catalogue,
  the XP feed and the review queue all grow.
- Cursor pagination + prefetch (§8); never `offset`-style loading.
- ETag/`If-None-Match` revalidation before refetching catalogue pages; serve cached
  body during offline and after `max-age` while `stale-while-revalidate` (600 s)
  applies. Everything else is `no-store`: cache in memory, not on disk.
- Images: `cached_network_image` with fixed aspect ratios (no layout jumps). Question
  aids are same-origin `/media` files served with a one-week TTL, so cache them
  aggressively; audio via `just_audio` with pre-buffering — stream the `/media/...` aid
  directly. Learner uploads go **straight to storage** with the presigned URL; never
  route them through the API.
- Quiz answers are tiny and one-at-a-time; keep the response handling synchronous and do
  **not** optimistically show a verdict — the score is the server's to give.
- One screen, one read: the dashboard is a single `GET /progress`, the hub a single
  `GET /gamification/summary`, the review badge a single `GET /review/preview`, the unit
  map a single `GET /progress/units`. Do not fan these back out into per-item probes.
- Parse large payloads off the main isolate (`compute`) only after profiling — lesson
  and session JSON here is < 1 KB.
- Keep the widget tree shallow under the quiz player; animate with implicit
  animations, not rebuild-heavy controllers.
- Profile on a mid-range Android device, not only an iPhone simulator.

---

## 12. Pre-release checklist

- [ ] Single-flight refresh proven by test; no sign-out storms on app resume
- [ ] `cursor` never sent empty; no `422` on first page
- [ ] `refreshToken` in secure storage; access token only in memory; 30-min/30-day TTLs respected
- [ ] `429` respects `Retry-After`; no route without a limiter is polled in a loop
- [ ] All five screen states implemented on every remote-backed screen
- [ ] `requestId` visible in error details and attached to support reports
- [ ] Catalogue browsable offline from cache; offline banner shown honestly
- [ ] Quiz grades only through the server: no answer key in the session payload, and no
      score shown that `op: answer|complete` did not return
- [ ] A quiz session started twice (retry after a lost response) reuses the same
      `sessionId` via `Idempotency-Key`; answers carry one too
- [ ] `IDEMPOTENCY_IN_PROGRESS` retries the **same** key after `Retry-After: 1`
- [ ] Quiz `sessionId` persisted and `GET /quiz/sessions/{id}` resumes after an app kill;
      **Finish** enabled only when the server returns `isComplete`
- [ ] `QUIZ_SESSION_EXPIRED` offers "start again"; `QUESTION_ALREADY_ANSWERED` reconciles
      by re-reading the session; `LESSON_LOCKED` explains the unlock chain
- [ ] Locked lessons/units come from `/progress/unlocks` and `/progress/units`; nothing
      is re-derived locally
- [ ] Checkpoint: 80% bar shown before starting; sealed answers explained; failure offers
      a fresh sitting (not a retry of missed questions); `unlockedUnitId` used on a pass
- [ ] Checkpoint earns no XP and is not counted as a lesson in the daily challenge
- [ ] Review empty state uses `EmptyView`; next-due rendered from the answer response;
      no XP animation on review
- [ ] Gamification: claim gated on `claimable`; `ALREADY_CLAIMED` refreshes; paywall shown
      when `premium` is false, and `purchase_url` opened on `402`; league tier compared as
      a lowercase string
- [ ] Daily challenge: the day shown is the server's `challengeDate` (learner timezone),
      `DAILY_CHALLENGE_ALREADY_CLAIMED` renders as "already claimed today", and a date
      mismatch re-renders from `details.challengeDate`
- [ ] AI surfaces render `source` honestly and degrade on quota/timeout/disabled
- [ ] Feature flags: absent key = off; flags hide entries but never replace authorisation
- [ ] Avatar flow is presign → PUT with `requiredHeaders` → confirm → `PATCH avatarKey`;
      signed media URLs are never persisted
- [ ] No hearts meter anywhere (hearts are not in the loop yet)
- [ ] No screen for a feature that is still `404` (tabs hidden, not disabled)
- [ ] `PATCH /me` sends only changed fields; `currentLevel` never sent
- [ ] Dev sign-in (`/auth/dev-login`) is not reachable from a release build's UI
- [ ] RTL layout verified for Arabic; both locales in the store listing
- [ ] Close-account flow explains it is immediate and permanent
- [ ] `flutter analyze --fatal-infos` + tests green in CI

---

## 13. Keeping up with the backend

- Treat `GET /openapi.json` as the contract: diff it in CI (a breaking change there
  should fail the mobile build before it fails a user).
- Error codes are reserved ahead of the phases that use them. **Live now:** `AUTH_*`,
  `USER_*`, `CONTENT_*`/`LESSON_*` (incl. `LESSON_LOCKED`), `QUIZ_*`/`QUESTION_*`,
  `CHECKPOINT_NOT_READY`, `REVIEW_*`, `ACHIEVEMENT_*`/`ALREADY_CLAIMED`/
  `NO_FREEZES_AVAILABLE`/`PREMIUM_REQUIRED`, `AI_*`/`CONVERSATION_NOT_FOUND`,
  `DAILY_CHALLENGE_*`, `MEDIA_*`, and `RATE_LIMITED`/`VALIDATION_ERROR`/
  `PAYLOAD_TOO_LARGE`/`IDEMPOTENCY_KEY_REUSED`/`IDEMPOTENCY_IN_PROGRESS`.
  **Reserved for later phases:** `INSUFFICIENT_HEARTS`, `SUBSCRIPTION_NOT_FOUND`,
  `PLAN_NOT_FOUND`, `PAYMENT_*`, `NOTIFICATION_DISABLED`, `DEVICE_*`, `WORD_NOT_FOUND`,
  `GRAMMAR_TOPIC_NOT_FOUND`, `JOB_*`. Handle unknown codes defensively (map to a generic
  `ServerFailure` with the raw code kept for logs) so a new backend code never crashes
  an old app build.
- The API applies schema changes behind the same `/api/v1` prefix; when a field is added,
  generated models pick it up. When a field is **replaced**, the mobile release train is
  the slow part — check the release notes before bumping the generated models.
