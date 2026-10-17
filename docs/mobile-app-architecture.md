# Natiqo mobile app — architecture, features and design blueprint

This document is for the mobile engineer building the **Natiqo** Flutter app. It answers
three questions the wire-level docs deliberately do not:

1. **How should the app be structured?** — layers, folders, state, navigation, storage.
2. **What are we building?** — every backend feature mapped to screens, with its
   readiness status (live today vs. planned backend phase).
3. **In what order?** — milestones that only depend on endpoints that exist or are
   scheduled, so no screen is ever built against a fictional API.

It complements, and never repeats:

| Document | Answers |
| --- | --- |
| [`mobile-integration-ar.md`](mobile-integration-ar.md) (Arabic) | The wire contract: auth, tokens, error codes, cursor/caching rules. **Read its §5 (auth) before writing the network layer.** |
| [`client-contract.md`](client-contract.md) (English) | Naming, error envelope, caching, redirects and rate-limit conventions. |
| [`backend-roadmap-ar.md`](backend-roadmap-ar.md) (Arabic) | When each missing endpoint lands, and the product decisions behind it. |
| `GET /openapi.json` | Machine-readable schema — generate types from it, do not hand-copy them. |

> **Reality check.** The backend has moved well past the earlier "catalogue only" state.
> The attempt engine, progress reads, **spaced repetition**, **gamification** and the
> **AI tutor** are all shipped and live. Only subscriptions, notification delivery, learner
> media uploads, vocabulary/grammar and admin/CMS are still ahead. Every readiness label in
> §6 was read from the mounted routers, not from the roadmap.

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
                       └───┬───────────┬───────┬──┘
                     PostgreSQL     Redis    S3 / /media
```

**What the backend gives you today — 31 operations.** (Plus `POST /api/v1/auth/dev-login`,
mounted only when `DEV_LOGIN_ENABLED` is true, which it is by default.)

```
Auth & account                       Learning loop
  POST   /auth/google                  POST   /quiz/sessions
  POST   /auth/apple                   GET    /quiz/sessions/{id}
  POST   /auth/refresh                 POST   /quiz/sessions/{id}   (op: answer|complete|abandon)
  POST   /auth/logout
  GET    /me                           Progress
  PATCH  /me                             GET    /progress             (summary + XP page)
  DELETE /me                             GET    /progress/unlocks     (curriculum-ordered map)
  GET    /me/devices
  DELETE /me/devices/{id}              Spaced repetition
                                         GET    /review/due
Catalogue (public, no auth)              GET    /review/stats
  GET    /content/tree                   GET    /review/preview
  GET    /content/lessons/{id}           POST   /review/cards/{id}/answer

Gamification                         AI tutor
  GET    /gamification/summary         POST   /ai/explain
  GET    /achievements                 POST   /ai/tutor
  POST   /achievements/{code}/claim    GET    /ai/tutor/{id}
  GET    /streak                       GET    /ai/quota
  POST   /streak/freeze

Health (no auth, not under /api/v1)
  GET    /health                        GET    /ready
```

**What does not exist yet:** subscriptions/billing, notification delivery (the inbox and
push), learner media uploads, vocabulary & grammar, admin/CMS, and `GET /me/export`.
Any request to those prefixes returns `404`. Section 6 maps each of them to the roadmap
phase that will unlock it. **Do not build a screen for a feature that is still `404`.**

Companion media (`/media/...`) is public, same-origin and served outside `/api/v1`.

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
      data/         # quiz_repository (session lifecycle, resume, answer grading)
      bloc/         # quiz_bloc, session_resume_cubit (survives app kill)
      presentation/ # quiz_screen, answer_feedback.dart, result_screen
    progress/
      data/         # progress_repository (overview, unlock map, XP feed)
      bloc/         # progress_bloc, unlock_map_cubit
      presentation/ # home_screen (dashboard), progress_screen, xp_history_screen
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
    profile/
      data/         # me_repository
      bloc/         # profile_bloc
      presentation/ # profile_screen, devices_screen, edit_profile_sheet
    settings/
      data/         # settings_repository (PATCH /me)
      bloc/         # settings_bloc
      presentation/ # settings_screen, notification_prefs_sheet
    # --- future features, same shape (see §6) ---
    subscription/ notifications/ media/ vocabulary/
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
  const Failure(this.message, {this.code, this.requestId, this.fields});
}
class NetworkFailure   extends Failure { }            // timeout / no connection
class AuthFailure      extends Failure { final String code; }
  // AUTH_TOKEN_EXPIRED, AUTH_TOKEN_INVALID, AUTH_REFRESH_REUSED, USER_DELETED,
  // AUTH_ACCOUNT_DISABLED, AUTH_PROVIDER_UNAVAILABLE, AUTH_IDENTITY_* …
class ValidationFailure extends Failure { final Map<String, String> fields; } // 422
class RateLimitedFailure extends Failure { final Duration retryAfter; }        // 429
class NotFoundFailure  extends Failure { final String code; }                  // 404
class ConflictFailure  extends Failure { final String code; }                  // 409
  // QUIZ_ALREADY_COMPLETED, QUIZ_NOT_COMPLETED, QUESTION_ALREADY_ANSWERED,
  // QUIZ_SESSION_EXPIRED, IDEMPOTENCY_KEY_REUSED, ALREADY_CLAIMED, NO_FREEZES_AVAILABLE …
class PremiumFailure   extends Failure { final String? purchaseUrl; }          // 402
class ServerFailure    extends Failure { }             // 5xx
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

Map by **`code` first, HTTP status second.** `401` + `AUTH_TOKEN_EXPIRED` means
"refresh"; `401` + `AUTH_REFRESH_REUSED` means "revoke everything and sign in again";
`401` + `USER_DELETED` means the same, permanently. `402` is `PREMIUM_REQUIRED` (the
`details` carry a `purchase_url` — open the paywall, do not show a generic error). `409`
from the learning loop is a *state* condition, not a failure to retry blindly:
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
  ..addInterceptor(RefreshInterceptor(authRepository))   // runs before Auth? see below
  ..addInterceptor(RequestIdInterceptor())
  ..addInterceptor(ErrorInterceptor());
```

Rules the client must honour (details in `mobile-integration-ar.md`):

- Send `Authorization: Bearer <access token>` on every call except `/auth/google`,
  `/auth/apple`, `/health`, `/ready` and the whole `/content/*` catalogue. **Everything
  else — `/me`, `/quiz/*`, `/progress/*`, `/review/*`, `/ai/*`, `/achievements`,
  `/streak`, `/gamification/*` — requires it**, because it is account data.
- **Delete `cursor` when it is null** — sending `cursor=` yields `422`. This is the
  single most common integration bug.
- No trailing slash. `/content/tree/` answers `307` and some clients drop the query
  string on redirect.
- Generate an `X-Request-ID` per request; the server echoes it — attach it to any
  error report.
- GZip is transparent; do not decompress manually.
- On `429`, read `Retry-After` and surface it to the UI — do **not** auto-retry in a
  loop. See the scope table below.

**Rate-limit scopes (per client IP, per minute):**

| Scope | Routes | Default |
| --- | --- | --- |
| auth | `/auth/*` | **10** |
| catalogue | `/content/*` | **240** |
| quiz | `/quiz/*` | **120** |
| progress | `/progress/*` | **240** |
| review | `/review/*` | **240** |
| ai | `/ai/*` | **30** — tightest, a call can bill upstream |
| gamification | `/gamification`, `/achievements`, `/streak` | **240** |
| default | everything else (incl. `/me`) | **120** |

### 4.2 Token storage

| Token | Where | Why |
| --- | --- | --- |
| `refreshToken` | `flutter_secure_storage` (Keychain / Keystore) | survives restarts, encrypted at rest |
| `accessToken` | memory only | 15-minute lifetime; losing it is free |
| `deviceId` | secure storage | stable key for `/me/devices` and rate-limit identity |

The server stores only a SHA-256 hash of the refresh `jti`, so a leaked local DB row
is not replayable — but the token itself is still a 30-day credential: secure storage
is not optional.

### 4.3 Single-flight refresh — the non-negotiable rule

Access tokens live **15 minutes** (`900` s). Several requests can fail with `401` at the
same moment (app resume, parallel screen loads). If each fires its own
`POST /auth/refresh`:

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
parallel.** Also send `device` with the refresh body, or `pushEnabled` detaches from the
device. (Full sequence: `mobile-integration-ar.md` §5.5.)

### 4.4 Idempotency

The backend accepts an `Idempotency-Key` header on unsafe methods: a repeat of the
same key + body replays the stored 2xx response for 24 hours; the same key with a
*different* body returns `409 IDEMPOTENCY_KEY_REUSED`. Generate one UUID per user
action and reuse it **verbatim** on network retries. This matters on:

- `POST /quiz/sessions` — a lost response must replay the same `sessionId`, not open a
  second sitting.
- `POST /quiz/sessions/{id}` with `{"op":"answer"}` — a retry must not double-count an
  answer or re-grade it as a fresh attempt.

---

## 5. State management — Bloc / Cubit

**Choice: `flutter_bloc`.** Rule of thumb in this app:

- **Cubit** for single-flow screens: catalogue lists, lesson loading, settings form,
  the gamification hub, the AI quota.
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
- On cold start: restore refresh token → attempt `POST /auth/refresh` silently →
  `GET /me`. Do not flash the sign-in screen while this runs: keep the splash until
  the state is decided.

### 5.2 Quiz state machine

`QuizBloc` owns one sitting end-to-end and is **the only place a score is decided** (by
reading the server's verdict, never by grading locally):

```
        start(lessonId)              answer(questionId, payload)
idle ─────────────────► presenting ─────────────────────► grading ──► feedback
        │                   ▲                                   │
        │                   └────────── next ────────────────────┘
        │                                                       │
        └── resume(sessionId) ──► (rebuild from GET /sessions/{id})
                                                                 ▼
                                            complete ──► completed (scorePercent)
                                            abandon  ──► abandoned
                                            expiry   ──► expired (QUIZ_SESSION_EXPIRED)
```

- Persist `sessionId` (secure storage or drift) the moment `POST /quiz/sessions`
  returns; on relaunch, resume instead of starting a new session.
- `isComplete` on each graded answer is what enables the **Finish** action — never
  enable it on `answeredCount == totalCount` you computed yourself.
- A `409 QUESTION_ALREADY_ANSWERED` means the local view was stale: call
  `GET /quiz/sessions/{id}` and reconcile rather than surfacing an error toast.

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
- The answer response carries the new SM-2 schedule (`due_at`, `interval_days`,
  `repetitions`, `state`) — show "next in 6 days" from that, never compute it.
- `GET /review/due?strict=true` turns an empty queue into `404 REVIEW_NOTHING_DUE`; the
  default (`strict=false`) returns an empty page. Either is a **success** state.

### 5.4 Server state vs. client state

| Kind | Owner | Example |
| --- | --- | --- |
| Server truth | repository + bloc | profile, catalogue, devices, quiz session, progress, review queue, achievements |
| Session | `AuthBloc` | tokens, current user |
| UI/local | plain `Cubit` or `ValueNotifier` | scroll position, selected tab, form drafts |
| Cached server data | drift + `etag` | catalogue pages for offline reading |

Never mirror server data into `shared_preferences`; `GET /me` is the single source of
truth for profile and preferences (the server refuses `currentLevel` on `PATCH /me` on
purpose — the placement test owns it). A quiz session lives on the server: the client
holds only its id and what it has been told, and re-reads on resume. Progress, review and
gamification are all derived server-side; the client renders them, it never re-derives
them.

### 5.5 Hygiene

- One `BlocObserver` in debug that logs transitions and errors with the current
  `requestId`.
- No `BuildContext` in blocs; navigation from blocs goes through a `NavigationCubit`
  or stream listened to by the router.
- Every bloc state carries `loading | loaded | Failure` explicitly — screens render
  three states, not two.

---

## 6. Feature map — screens ⇄ endpoints ⇄ readiness

Legend: **LIVE** = endpoint exists today · **PHASE n** = unblocked by roadmap phase *n*
(see `backend-roadmap-ar.md`) · **404 today**.

### 6.1 Accounts — LIVE

| Screen | Endpoints | Notes |
| --- | --- | --- |
| Splash / session restore | `POST /auth/refresh`, `GET /me` | silent; keep splash until decided |
| Sign-in (Google + Apple) | `POST /auth/google`, `POST /auth/apple` | **first sign-in creates the account** — no register screen and no forgot-password screen, ever |
| Home shell | `GET /me` | greeting, level chip, daily goal (goal value from `/me` today) |
| Profile | `GET/PATCH /me` | cannot edit `currentLevel`; omit a field to leave it untouched |
| Devices | `GET/DELETE /me/devices` | shows last-seen; revoking a device revokes its sessions |
| Close account | `DELETE /me` | destructive: confirm dialog + re-auth; no grace period exists server-side (idempotent `204`) |
| Sign out | `POST /auth/logout` | `allDevices: false` default; clears local tokens regardless |
| Dev sign-in (debug builds only) | `POST /auth/dev-login` | `admin`/`admin`; mounted by default in every env, 10-year tokens — a QA backdoor, never shown to learners |

Advisory fields on sign-in (`displayName`, `nativeLanguage`, `englishVariant`,
`timezone`, `locale`) apply **only when the account is created**; they never overwrite
an existing learner's choices.

### 6.2 Content catalogue — LIVE

| Screen | Endpoint | Design notes |
| --- | --- | --- |
| Curriculum (levels → units → lessons) | `GET /content/tree` | public (no auth gate!) — usable pre-sign-in; nested, with `unitCount`/`lessonCount`/`questionCount`; one response, no pagination |
| Lesson detail | `GET /content/lessons/{id}` | questions **without** `correctIndex`, `acceptedAnswers`, `explanation` — plus optional `imageUrl`/`audioUrl` aids |

- Both are cacheable: `ETag` + `Cache-Control: max-age=60,
  stale-while-revalidate=600`. Cache the body + ETag in drift; revalidate with
  `If-None-Match` and serve `304` from cache while offline.
- The tree returns the **whole** published hierarchy at once, each level carrying its units
  and each unit its lessons — so the app can render the curriculum from a single cached
  response instead of walking three paginated lists.
- Rate limit 240/min per IP — generous, but still surface `429`.
- The catalogue is for **reading**. The lesson screen renders the lesson and its
  questions with their image/audio aids — but it does not grade and never exposes the
  rationale. The moment the learner wants to answer for real, hand off to the **quiz
  player (§6.3)**, which grades on the server and returns the explanation per answer;
  never compute a score the server did not produce.
- `imageUrl`/`audioUrl` are same-origin `/media/...` paths (not under `/api/v1`),
  immutable, and few — cache them for the life of the app. A question that is unpublished
  or unknown is a `404` (`QUESTION_NOT_FOUND`) on the lesson endpoint, indistinguishable
  from a never-existed question on purpose — so the catalogue cannot be used to probe the
  un-published content pipeline.

### 6.3 Quiz — LIVE (the attempt engine)

The core loop is here. Three authenticated endpoints under `/quiz`; every session is
scoped to the caller and **graded on the server** — the answer key never reaches the
client before an answer is committed.

| Screen | Endpoints | Notes |
| --- | --- | --- |
| Lesson → "Start practice" | `POST /quiz/sessions` | body `{ "mode": "lesson", "lessonId": "uuid" }` → `201` with the session and its questions **without** an answer key. Send an `Idempotency-Key` so a lost response replays the same `sessionId` rather than opening a second sitting |
| Quiz player (resume) | `GET /quiz/sessions/{id}` | **resume after app kill** — the session plus the questions, and `answeredQuestionIds` so you skip exactly what is already done |
| Answer / finish / give up | `POST /quiz/sessions/{id}` | one route, three verbs, told apart by `op`. `{"op":"answer","questionId","answer":{"choice"\|"text"}}` → `result`: `isCorrect`, `correctAnswer`, `explanation`, `pointsAwarded`, `answeredCount`, `totalCount`, `correctCount`, `isComplete`; `{"op":"complete"}` → `result`: the session + `scorePercent`; `{"op":"abandon"}` → `result`: the ended session |

Wire shapes (camelCase; field names are exact):

```jsonc
// POST /quiz/sessions        → 201
{
  "session": { "id":"uuid", "mode":"lesson", "lessonId":"uuid", "status":"active",
               "startedAt":"…", "expiresAt":"…", "completedAt":null,
               "totalCount":6, "answeredCount":0, "correctCount":0, "xpAwarded":0,
               "answeredQuestionIds":[] },
  "questions": [ { "id":"uuid", "position":0, "kind":"multiple_choice",
                   "skill":"vocabulary", "prompt":"…", "choices":["a","b","c"],
                   "points":1, "imageUrl":"/media/images/….jpg",
                   "audioUrl":"/media/audio/….mp3", "prompts":[] } ]
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

// 200 (op=answer) — the result mirrors op
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
  "result": { "session": { … , "status":"completed", "xpAwarded":5 }, "scorePercent":100 } }
```

Client rules:

- **Questions arrive without `correctIndex`, `acceptedAnswers` or `explanation`.**
  Correctness only comes back per answer, from the answers call. Never expect a key in
  the session payload.
- **Submit answers one at a time, each as `{"op":"answer", …}` with an `Idempotency-Key`.**
  A network retry must not double-count.
- For a choice question, send the **choice's text** (`{"choice":"b"}` also accepts the
  zero-based index or the letter `a`–`f`); for `fill_blank` and `word_bank`, send
  `{"text":"…"}` (graded case- and whitespace-insensitively — a `word_bank`'s `choices`
  are the tiles to assemble from, not options to pick, so the answer is still its text).
  For `multi_select`, send `{"choices":[…]}` — the
  full set the learner picked, each entry as text, index or letter; grading is exact-set,
  so a partial pick is wrong. For `ordering`, send `{"order":[…]}` — every item exactly once,
  in the learner's chosen sequence; grading is element-wise against the authored order and
  all-or-nothing, so one item out of place is wrong. The items come back in `choices` as a
  shuffled sequence that is **never** the correct one, so never replay the payload order as
  an answer. Sending the wrong shape, an item not in the question, or a partial/full sequence
  is `422`, not a silent "wrong". For `matching`, send `{"matching":[…]}` — every prompt
  exactly once, each entry as `"prompt => option"` with the option's **text** as it was
  served. The pair order never matters: grading is set equality, so all-or-nothing per set.
  A pair with no arrow, an unknown prompt or option, or a prompt left out is `422`; a
  complete set that simply disagrees with the key is a wrong answer.
- **The session survives process death.** Persist `sessionId`; on relaunch call
  `GET /quiz/sessions/{id}` and skip exactly `answeredQuestionIds`.
- **Scoring is server-owned and "first try":** a correct answer on the **first attempt**
  is worth full points; a correct retry is worth **half** (integer division, rounded
  down — a one-point question earns nothing on a retry). A **wrong** answer can be
  re-answered and overwrites the same answer slot, so a question counts once. A question
  already answered **correctly** is closed — re-answering it is `409
  QUESTION_ALREADY_ANSWERED`; reconcile by re-reading the session.
- `complete` refuses a partial sitting with `409 QUIZ_NOT_COMPLETED`; enable **Finish**
  only when the last answer came back `isComplete: true`.
- The session is a **lease**: it expires after `QUIZ_SESSION_TTL_SECONDS` (**7200 s**).
  Once expired, answering or completing returns `409 QUIZ_SESSION_EXPIRED`; start again.
  A completed or abandoned session can still be read back for its result.
- Rate limit **120/min per IP** for the quiz scope — surface `429` with `Retry-After`.
- Session questions still carry `imageUrl`/`audioUrl` aids (same as the catalogue); render
  them — they teach the item, they do not reveal the answer.
- **Finishing a lesson also seeds spaced-repetition cards** for the questions the learner
  answered correctly (§6.5), and feeds XP/streak/achievements (§6.6) — all server-side,
  and all in the same transaction. The client does nothing extra.

Errors: `QUIZ_SESSION_NOT_FOUND` · `QUIZ_SESSION_EXPIRED` · `QUIZ_ALREADY_COMPLETED` ·
`QUIZ_NOT_COMPLETED` · `QUESTION_NOT_FOUND` · `QUESTION_NOT_IN_SESSION` ·
`QUESTION_ALREADY_ANSWERED` · `QUESTION_TYPE_UNSUPPORTED` · `LESSON_NOT_FOUND` ·
`LESSON_NOT_AVAILABLE` (a lesson with no questions cannot be started).

**Not yet in the loop:** hearts. There is no `sessionHearts` column and no penalty on a
wrong answer, so do not draw a heart meter. Streaks, XP and achievements *are* served
(§6.4, §6.6).

### 6.4 Progress — LIVE

Completion **writes** durable per-lesson progress (state `in_progress` at start,
`completed` at the end; `bestScore` keeps the maximum, `attempts` increments, `xpEarned`
accumulates — a retry can only improve it), and two authenticated reads serve it back.
Every figure is **derived** from the rows the attempt engine wrote; nothing is projected
or invented, and anything without a backing store is omitted rather than returned as a
zero.

| Screen | Endpoints |
| --- | --- |
| Home / dashboard | `GET /progress` |
| Lesson map | `GET /progress/unlocks` |
| XP history | `GET /progress?limit=&cursor=` (the `xp` page) |

`GET /progress` → the summary fields plus the first page of XP history in one call, so the
dashboard is a single request. Summary fields: `totalXp`, `lessonsCompleted`,
`lessonsInProgress`, `lessonsStarted`, `correctCount`, `answeredCount`, `accuracyPercent`,
`currentStreakDays`, `longestStreakDays`, `lastActivityAt`. The streak is measured in the
learner's own timezone (profile `timezone`, `Asia/Baghdad` by default), not UTC. The `xp`
page is the paginated XP feed, **newest first**, one row per *completed* session:
`sessionId`, `lessonId`, `xpAwarded`, `correctCount`, `totalCount`, `scorePercent`,
`completedAt`. An abandoned sitting earned nothing and never appears. Cursor is opaque and
ordered by `(completedAt, id)` — echo `meta.nextCursor` back verbatim as `?cursor=`, and
page the feed by re-calling `/progress?limit=&cursor=` and reading `xp`.

`GET /progress/unlocks` → `{ items: [{ lessonId, unitId, levelCode, title, position, state,
locked, bestScore, attempts, xpEarned, completedAt }], total }`: every *published* lesson in
curriculum order (level → unit → lesson position). Whole-map, not paginated — the published
catalogue is the bound. **`locked` is present but always `false` today**: no gating rule is
in force yet, so nothing blocks starting a lesson. A lesson the learner has never touched
still returns a view with a zeroed record (`attempts: 0`, `xpEarned: 0`). The per-lesson
`state` (`available | in_progress | completed`) is read straight off the map, so there is
no separate per-lesson progress call.

Both are authenticated (`CurrentUser`), scoped to the caller, and `Cache-Control:
no-store`. They share their own rate-limit scope (240/min), separate from the write budget.

### 6.5 Spaced repetition — LIVE

Four authenticated endpoints under `/review`. Cards are **seeded automatically**: the first
time a learner answers a question correctly in a lesson, a card is created (state `new`,
due one day later). The schedule is classic **SM-2**; the engine produces only two grades —
a correct first attempt is quality **5**, anything else is **2**.

| Screen | Endpoints | Notes |
| --- | --- | --- |
| Daily review | `GET /review/due?limit=&strict=` | cards due now, soonest first; `dueCount` is the total owed; pass `strict=true` to get `404 REVIEW_NOTHING_DUE` instead of an empty page |
| Review (answer) | `POST /review/cards/{id}/answer` | same body as a quiz answer; returns the verdict, `correctAnswer`, `explanation`, plus the new SM-2 schedule: `quality`, `dueAt`, `intervalDays`, `repetitions`, `lapses`, `state` |
| Review tab badge | `GET /review/preview` | just `{ dueCount }` — cheaper than loading the queue |
| Review stats | `GET /review/stats` | `totalCards`, `dueToday`, `future`, `reviewedTotal`, `retentionPercent` |

Design notes:

- The question served in a due card is the **same answer-free projection** a live session
  uses — the key still only arrives after an answer. Reuse the quiz's question card and
  answer-feedback widgets.
- **"Nothing due today" is a success, not an error.** Render `EmptyView`, not `ErrorView`.
  `REVIEW_NOTHING_DUE` exists only for the `strict=true` caller.
- A card whose question was unpublished is silently skipped, so a card count can exceed a
  page's length — never assume `dueCount == cards.length`.
- Rate limit 240/min. Answering is a write but shares the review scope with the reads.

### 6.6 Gamification — LIVE

Five authenticated endpoints. The hub, the achievement catalogue and the streak screen are
each a **single read**, so each is one round trip.

| Screen | Endpoints | Notes |
| --- | --- | --- |
| Hub (XP, gems, streak, league, badge tally) | `GET /gamification/summary` | `stats` (`xpTotal`, `gems`, `currentStreak`, `longestStreak`, `leagueTier`, `freezesAvailable`, `lastActivityDate`) + `lessonsCompleted`, `achievementsUnlocked`, `achievementsTotal` |
| Achievements | `GET /achievements`, `POST /achievements/{code}/claim` | catalogue includes locked badges with live `progress`/`target`; `claimable` is the one bit a "claim" button keys off. `ALREADY_CLAIMED` (409) means refresh the list, not an error toast; an unknown or not-yet-unlocked code is `ACHIEVEMENT_NOT_FOUND` (404) |
| Streak detail | `GET /streak`, `POST /streak/freeze` | `premium` tells you whether freezing is offered **before** the tap. A non-premium deployment answers `402 PREMIUM_REQUIRED` with a `purchase_url`; no freezes left is `409 NO_FREEZES_AVAILABLE` |

- **League tiers** are read from lifetime XP: `BRONZE` (0), `SILVER` (100), `GOLD` (500),
  `PLATINUM` (1500), `DIAMOND` (4000). The tier travels as its **string** value.
- **Achievements** are a small code-owned catalogue (seeded from code): `first_lesson`,
  `ten_lessons`, `twenty_five_lessons`, `xp_century`, `xp_veteran`, `streak_three`,
  `streak_week`, `streak_month`. Titles/descriptions/icon are **translation keys**
  (`achievement.first_lesson.title`, …) — translate in the app, do not ship English text.
- Streak achievements measure the **longest** run, so they are never revoked by a later
  gap. If there is no stored stats row yet, the server projects one from durable history —
  a returning learner never reads as a blank sheet.
- Rate limit 240/min.

### 6.7 AI tutor — LIVE, but disabled by default

Four authenticated endpoints under `/ai`. **The module is enabled per deployment**
(`ai_enabled`, off by default). When it is off, or over budget, or the provider errors,
`explain` still returns the **authored** explanation and `tutor` still returns a
deterministic reply — so the UI never has an "AI is down" branch to write. Every
AI-flavoured response carries `source` (`"ai"` | `"authored"` | `"unavailable"`): render
provenance honestly rather than mislabelling a fallback as model output.

| Screen | Endpoints | Notes |
| --- | --- | --- |
| "Explain this answer" | `POST /ai/explain` | body `{ questionId, answer }`; returns `isCorrect`, `correctAnswer`, `explanation`, `source`. The verdict is computed on the server, exactly as `/quiz` does |
| Tutor chat | `POST /ai/tutor`, `GET /ai/tutor/{id}` | omit `conversationId` to open a thread, pass it to continue; the thread is persisted so it is still there on return. `lessonId` is an optional anchor |
| Quota display | `GET /ai/quota` | `enabled`, `allowed`, `limitUsd`, `spentUsd`, `remainingUsd`, `reason` — warn before it runs out |

- **AI never grades.** Points, schedules and scores come only from the attempt engine's
  policy. `explain` is a convenience on top of it.
- `AI_QUOTA_EXCEEDED`, `AI_TIMEOUT`, `AI_PROVIDER_ERROR`, `AI_DISABLED` and
  `AI_UNSAFE_INPUT` are normal outcomes — degrade gracefully. An unknown
  `conversationId` is `404 CONVERSATION_NOT_FOUND`.
- Rate limit **30/min** — the tightest scope, because a call can bill upstream. Do not
  poll it.

### 6.8 Subscriptions & billing — PHASE 4 (not built)

| Screen | Endpoints |
| --- | --- |
| Paywall / plans | `GET /plans` |
| Current subscription | `GET /subscription` |
| Checkout | `POST /subscription/checkout` → open the returned Stripe URL (WebView / native) |
| Cancel | `POST /subscription/cancel` |
| Billing portal | `POST /subscription/portal` |

Today the only "premium" that exists is the **streak-freeze gate** in §6.6, which returns
`PREMIUM_REQUIRED` with `premium_purchase_url`. There is no per-account plan and no
client-side "is premium" flag to build: when the endpoints land, read `GET /subscription`
on start and after every purchase flow. The webhook (Stripe → server) is the source of
truth; the app only reflects it.

### 6.9 Notifications — PHASE 5 (not built)

| Screen | Endpoints |
| --- | --- |
| Inbox | `GET /notifications?cursor=`, `POST /notifications/{id}/read`, `POST /notifications/read-all`, `DELETE /notifications/{id}` |
| Badge | `GET /notifications/unread-count` (poll lightly or refresh on foreground) |
| Push token registration | FCM token goes in `device.pushToken` on sign-in / refresh (the `device` object already accepts it today) |

Preference switches already exist today under `PATCH /me`
(`notificationPreferences` — `dailyReminder`, `streakWarning`, `reviewDue`,
`weeklyReport`, `quietHoursStart/End`): build the toggles now, wire them to `/me`;
delivery itself waits for phase 5. These switches are **part of `GET /me`**, not a
separate store.

### 6.10 Media — PHASE 6 (learner uploads)

Two different things share the name. **Catalogue aids already work today:** question
`imageUrl`/`audioUrl` are public, same-origin `/media/...` files (§6.2), and the quiz
session and review card serve them too (§6.3, §6.5) — read them directly and cache them.
**Learner uploads are phase 6:** presigned upload `POST /media/presign` → upload directly
to S3 → `POST /media/confirm` → read via `GET /media/{id}` (signed URL, 15 min). Relevant
for speaking/pronunciation answers later; do not stream audio through the API server.

### 6.11 Vocabulary & grammar — PHASE 9 (not built)

`GET /vocabulary`, `GET /vocabulary/{word}`, `POST /vocabulary/{word}/favorite`,
`GET /grammar/topics`, `GET /grammar/topics/{id}`.

### 6.12 Not for the phone app

- **Admin content authoring** (`/admin/content/*`) — web only, role-gated
  (`content_editor`, `admin`). Do not build it into the learner app.
- `GET /me/export` (roadmap 0.9) — not built yet; when it lands, a "download my data" row
  in Settings that triggers a share sheet is enough.

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
        GoRoute(path: '/review', ...),      // LIVE — the SRS queue of §6.5
        GoRoute(path: '/progress', ...),    // LIVE — the progress reads of §6.4
        GoRoute(path: '/profile', ...),
      ],
    ),
    GoRoute(path: '/lesson/:id', ...),
    GoRoute(path: '/quiz/:sessionId', ...),  // LIVE — the quiz player of §6.3
    GoRoute(path: '/gamification', ...),     // LIVE — hub / achievements / streak (§6.6)
    GoRoute(path: '/tutor', ...),            // LIVE (AI, deployment-dependent) (§6.7)
  ],
);
```

- **Public routes:** `/splash`, `/sign-in`, `/learn` catalogue browsing. The catalogue
  is deliberately unauthenticated — a new learner can see the course before signing in.
- **The Review tab is live now** — badge it from `GET /review/preview`; still **hide**
  (do not grey out) any tab for a feature that is still `404`. (The quiz player is not a
  tab: it is pushed from a lesson.)
- Deep link `natiqo://lesson/{id}` → resolve through the same router; guard with the
  auth redirect so a cold deep link cannot bypass sign-in for protected screens. A
  `natiqo://quiz/{sessionId}` link must be auth-guarded too, and should resume the
  session via `GET /quiz/sessions/{id}`.

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
- **Catalogue:** render levels → units → lessons from the single cached `GET
  /content/tree` response, with counts on each tile. Keep scroll position on back
  (ValueKey per route + `AutomaticKeepAlive`). Only the XP feed paginates cursor-style;
  prefetch its next page when the last visible row is within 5 of the end.
- **Lesson reader:** question card with choice buttons / true-false / text field / tile bank
  per `kind` (`multiple_choice`, `multi_select`, `true_false`, `fill_blank`, `word_bank`),
  showing the `imageUrl`/`audioUrl` aid when present. An `ordering` question has **no
  `choices` in the catalogue** — they are stored in the answer order, so the reader shows
  the prompt and defers to the quiz player, which serves the reshuffled list. A `matching`
  question shows its `prompts` but **no `choices`** — the bank can be paired back by
  position, so it waits for the quiz session too. The catalogue
  is for **reading**; the moment the learner wants to answer for a result, hand off to the
  quiz player — never grade locally or show a score the server did not produce.
- **Quiz player:** one question at a time, with a progress indicator
  (`answeredCount`/`totalCount`). Render `multi_select` as checkboxes that collect a set
  before one submit, and `ordering` as a reorderable list the learner arranges before one
  submit; `matching` as two columns — `prompts` fixed on the left, the `choices` bank on the
  right — where a prompt takes at most one option and the pair list is submitted once every
  prompt has one. Never pre-select a pairing from the two lists' positions: the server
  guarantees no option is served opposite the prompt it answers. The others submit on tap.
  Disable the choices while an answer is in
  flight; show the verdict, the correct answer and the explanation from the answer
  response in place; advance on the learner's tap, not automatically. Persist `sessionId`
  so an app kill resumes via `GET /quiz/sessions/{id}`. Enable **Finish** only when the
  server says `isComplete`. On `QUIZ_SESSION_EXPIRED`, offer "start again"; on
  `QUESTION_ALREADY_ANSWERED`, re-read the session instead of erroring.
- **Result screen:** show `scorePercent` from the `complete` response — never recompute
  it. Do not draw a hearts meter (§6.3 note).
- **Review:** reuse the quiz question card and feedback widgets — the contract is
  identical. Show the returned next-due ("next in 6 days") from the answer response. An
  empty queue is `EmptyView`, and `strict=false` means an empty page is normal.
- **Gamification hub:** one `GET /gamification/summary` fills the whole screen. Draw
  achievement bars from `progress`/`target`; enable a claim button only when `claimable`
  is true; treat `ALREADY_CLAIMED` as "refresh", not an error. On the streak screen, show
  the paywall when `premium` is false **before** the freeze tap, and open `purchase_url`
  on `402`.
- **AI surfaces:** render `source` honestly ("AI" vs. "from the lesson"). Design for
  `AI_QUOTA_EXCEEDED`/`AI_TIMEOUT` as normal, not fatal — the authored explanation is
  always available. The tutor is rate-limited to 30/min: no polling.
- **Profile:** `currentLevel` shown read-only; `PATCH` sends only changed fields —
  never send the whole object back (that is how `422` on `currentLevel` happens).
- **Destructive actions** (revoke device, close account): confirm dialog that names
  the consequence; `DELETE /me` additionally requires re-authentication.

**Design tokens:** one `ThemeExtension` for brand color, spacing scale, and type
scale; both locales (`en`, `ar`) with RTL mirroring from the start (`Directionality`
driven by `locale`), because half the audience reads Arabic.

---

## 9. Delivery milestones

Each milestone is shippable and depends only on endpoints that exist (or on a named
roadmap phase).

| # | Scope | Depends on | Status |
| --- | --- | --- | --- |
| **M0** | Project scaffold: folder structure, theme, router, DI, Dio + interceptors (auth, single-flight refresh, request-id, error mapping), token store, `AuthBloc` state machine, CI lint+test | — | **buildable now** |
| **M1** | Sign-in (Google/Apple), session restore, shell, catalogue: the curriculum tree + lesson detail with ETag cache | live endpoints | **buildable now** |
| **M2** | Lesson reader, profile (view/edit), devices, settings + notification preference toggles, close account | live endpoints | **buildable now** |
| **M3a** | **Quiz player (the core loop):** start / resume / answer / complete / abandon, server-graded feedback with explanation, session resume across app kill | live quiz endpoints | **buildable now** |
| **M3b** | Home dashboard, lesson map, XP history, progress tab | live progress reads (§6.4) | **buildable now** |
| **M4** | Daily review (SRS) queue, review tab + badge, review stats | live review endpoints (§6.5) | **buildable now** |
| **M5** | Gamification hub, achievements (with claim), streak detail + freeze gate | live gamification endpoints (§6.6) | **buildable now** |
| **M6** | AI tutor chat + "explain this answer" + quota, behind a feature flag | live AI endpoints (§6.7) | **buildable now** |
| **M7** | Notification inbox + FCM push + media upload for speaking tasks | **Phases 5–6** | blocked |
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
| Interceptors | pure unit tests | **single-flight refresh**: N concurrent 401s → exactly 1 refresh call; retry-once; no retry after `AUTH_REFRESH_REUSED` |
| Quiz player | `bloc_test` + fake Dio | start → answer → feedback → next → complete; resume rebuilds from `answeredQuestionIds`; `isComplete` gates Finish; `QUIZ_SESSION_EXPIRED` and `QUESTION_ALREADY_ANSWERED` handled |
| Review | `bloc_test` + fake Dio | due → answer → rescheduled; empty queue is a success; `strict=true` maps `REVIEW_NOTHING_DUE` to the empty state, not an error |
| Gamification | `bloc_test` + fake Dio | hub from one read; `ALREADY_CLAIMED` refreshes; `PREMIUM_REQUIRED` opens the paywall; `NO_FREEZES_AVAILABLE` is graceful |
| AI | `bloc_test` + fake Dio | `source` rendered honestly; `AI_QUOTA_EXCEEDED`/`AI_TIMEOUT` degrade, not crash |
| Widgets | `flutter_test` + golden | the five screen states; RTL layout |
| E2E | `integration_test` against staging | sign-in → browse → lesson → **start a quiz, answer every question, see the score** → **answer a due review card** → **open the hub**; token expiry across 15 min (fast-forward via injected clock) |

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
  applies.
- Images: `cached_network_image` with fixed aspect ratios (no layout jumps). Question
  aids are served same-origin from `/media` and are immutable, so cache them
  aggressively. Audio via just_audio with pre-buffering — stream the `/media/...` aid
  directly; user uploads (phase 6) come via signed S3 URLs, never through the API.
- Quiz answers are tiny and one-at-a-time; keep the response handling synchronous and do
  **not** optimistically show a verdict — the score is the server's to give.
- One screen, one read: the dashboard is a single `GET /progress`, the hub a single
  `GET /gamification/summary`, the review badge a single `GET /review/preview`. Do not
  fan these back out into per-item probes.
- Parse large payloads off the main isolate (`compute`) only after profiling —
  lesson and session JSON here is < 1 KB.
- Keep the widget tree shallow under the quiz player; animate with implicit
  animations, not rebuild-heavy controllers.
- Profile on a mid-range Android device, not only an iPhone simulator.

---

## 12. Pre-release checklist

- [ ] Single-flight refresh proven by test; no sign-out storms on app resume
- [ ] `cursor` never sent empty; no `422` on first page
- [ ] `refreshToken` in secure storage; access token only in memory
- [ ] All five screen states implemented on every remote-backed screen
- [ ] `requestId` visible in error details and attached to support reports
- [ ] `429` respects `Retry-After`; auth calls not auto-retried in a loop
- [ ] Catalogue browsable offline from cache; offline banner shown honestly
- [ ] Quiz grades only through the server: the session payload never supplies an answer
      key, and no score is shown that `POST /quiz/sessions/{id}` (`op: answer|complete`)
      did not return
- [ ] A quiz session started twice (retry after a lost response) reuses the same
      `sessionId` via `Idempotency-Key`; answers carry one too
- [ ] Quiz session `sessionId` is persisted and `GET /quiz/sessions/{id}` resumes after
      an app kill; **Finish** enabled only when the server returns `isComplete`
- [ ] `QUIZ_SESSION_EXPIRED` offers "start again"; `QUESTION_ALREADY_ANSWERED` reconciles
      by re-reading the session, not by an error toast
- [ ] Review empty state uses `EmptyView`, not `ErrorView`; next-due rendered from the
      answer response, never computed locally
- [ ] Gamification: claim gated on `claimable`; `ALREADY_CLAIMED` refreshes; paywall
      shown when `premium` is false, and `purchase_url` opened on `402`
- [ ] AI surfaces render `source` honestly and degrade on quota/timeout/disabled
- [ ] No hearts meter anywhere (hearts are not in the loop yet)
- [ ] No screen for a feature that is still `404` (tabs hidden, not disabled)
- [ ] `PATCH /me` sends only changed fields; `currentLevel` never sent
- [ ] Dev sign-in (`/auth/dev-login`) is not reachable from a release build's UI
- [ ] RTL layout verified for Arabic; both locales in the store listing
- [ ] Close-account flow explains it is immediate and permanent
- [ ] `flutter analyze --fatal-infos` + tests green in CI

---

## 13. Keeping up with the backend

- Subscribe to [`backend-roadmap-ar.md`](backend-roadmap-ar.md) — each phase lists the
  exact endpoints it adds, their error codes and the product decisions pending.
- Treat `GET /openapi.json` as the contract: diff it in CI (a breaking change there
  should fail the mobile build before it fails a user).
- Error codes are reserved ahead of the phases that use them. **Live now:** the `AUTH_*`,
  `USER_*`, `CONTENT_*`/`LESSON_*`, `QUIZ_*`/`QUESTION_*`, `REVIEW_*`, `ACHIEVEMENT_*`/
  `ALREADY_CLAIMED`/`NO_FREEZES_AVAILABLE`/`PREMIUM_REQUIRED`, `AI_*`/`CONVERSATION_NOT_FOUND`,
  and `RATE_LIMITED`/`VALIDATION_ERROR`/`IDEMPOTENCY_KEY_REUSED` sets. **Reserved for later
  phases:** `INSUFFICIENT_HEARTS`, `SUBSCRIPTION_NOT_FOUND`, `PLAN_NOT_FOUND`,
  `PAYMENT_*`, `NOTIFICATION_DISABLED`, `DEVICE_*`, `MEDIA_*`, `WORD_NOT_FOUND`,
  `GRAMMAR_TOPIC_NOT_FOUND`, `JOB_*`. Handle unknown codes defensively (map to a generic
  `ServerFailure` with the raw code kept for logs) so a new backend code never crashes
  an old app build.
