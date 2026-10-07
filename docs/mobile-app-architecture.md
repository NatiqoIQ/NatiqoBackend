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
| [`mobile-integration-ar.md`](mobile-integration-ar.md) (Arabic) | The wire contract: every live endpoint, error code, caching rule, token lifecycle. **Read §5 (auth) before writing the network layer.** |
| [`client-contract.md`](client-contract.md) (English) | Naming, error envelope, caching and rate-limit conventions. |
| [`backend-roadmap-ar.md`](backend-roadmap-ar.md) (Arabic) | When each missing endpoint lands, and the product decisions behind it. |
| `GET /openapi.json` | Machine-readable schema — generate types from it, do not hand-copy them. |

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
                     PostgreSQL     Redis    S3
```

**What the backend gives you today (15 operations):**

```
POST   /api/v1/auth/google          POST   /api/v1/auth/apple
POST   /api/v1/auth/refresh         POST   /api/v1/auth/logout
GET    /api/v1/me                   PATCH  /api/v1/me
DELETE /api/v1/me                   GET    /api/v1/me/devices
DELETE /api/v1/me/devices/{id}
GET    /api/v1/content/levels
GET    /api/v1/content/levels/{code}/units
GET    /api/v1/content/units/{id}/lessons
GET    /api/v1/content/lessons/{id}
GET    /health                      GET    /ready
```

**What does not exist yet:** quiz, progress, SRS review, hearts/streaks/XP,
subscriptions, push delivery, media upload, AI, vocabulary, admin. Any request to those
prefixes returns `404`. Section 6 maps each of them to the roadmap phase that will
unlock it.

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
      local_database.dart       # drift schema: cached catalogue, settings
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
      data/         # lesson_repository (uses catalogue endpoint + local cache)
      bloc/         # lesson_bloc, question_viewer_cubit
      presentation/ # lesson_screen, question_card.dart
    profile/
      data/         # me_repository
      bloc/         # profile_bloc
      presentation/ # profile_screen, devices_screen, edit_profile_sheet
    settings/
      data/         # settings_repository (PATCH /me)
      bloc/         # settings_bloc
      presentation/ # settings_screen, notification_prefs_sheet
    # --- future features, same shape (see §6) ---
    quiz/ progress/ review/ gamification/ subscription/
    notifications/ media/ ai/ vocabulary/
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
  // AUTH_TOKEN_EXPIRED, AUTH_TOKEN_INVALID, AUTH_REFRESH_REUSED, USER_DELETED …
class ValidationFailure extends Failure { final Map<String, String> fields; } // 422
class RateLimitedFailure extends Failure { final Duration retryAfter; }        // 429
class NotFoundFailure  extends Failure { final String code; }                  // 404
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
`401` + `USER_DELETED` means the same, permanently. Never parse `message` for logic —
it is human text and may be localised later. Always keep `requestId` for the
"report a problem" flow.

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

- Send `Authorization: Bearer <access token>` on every call except
  `/auth/google`, `/auth/apple`, `/health`.
- **Delete `cursor` when it is null** — sending `cursor=` yields `422`. This is the
  single most common integration bug.
- No trailing slash. `/content/levels/` answers `307` and some clients drop the query
  string on redirect.
- Generate an `X-Request-ID` per request; the server echoes it — attach it to any
  error report.
- GZip is transparent; do not decompress manually.
- On `429`, read `Retry-After` and surface it to the UI — do **not** auto-retry in a
  loop. Auth endpoints are limited to 10/min per IP.

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

Access tokens live **15 minutes**. Several requests can fail with `401` at the same
moment (app resume, parallel screen loads). If each fires its own
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
parallel.** (Full sequence: `mobile-integration-ar.md` §5.5.)

### 4.4 Idempotency (future writes)

The backend accepts an `Idempotency-Key` header on unsafe methods: a repeat of the
same key + body replays the stored 2xx response for 24 hours; the same key with a
*different* body returns `409 IDEMPOTENCY_KEY_REUSED`. Generate one UUID per user
action (submit answer, checkout) and reuse it on network retries. This matters from
milestone 3 (quiz submissions) onward.

---

## 5. State management — Bloc / Cubit

**Choice: `flutter_bloc`.** Rule of thumb in this app:

- **Cubit** for single-flow screens: catalogue lists, lesson loading, settings form.
- **Bloc** for multi-event lifecycles: `AuthBloc`, future `QuizBloc` (answer →
  feedback → next), future `SubscriptionBloc`.

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

### 5.2 Server state vs. client state

| Kind | Owner | Example |
| --- | --- | --- |
| Server truth | repository + bloc | profile, catalogue, devices |
| Session | `AuthBloc` | tokens, current user |
| UI/local | plain `Cubit` or `ValueNotifier` | scroll position, selected tab, form drafts |
| Cached server data | drift + `etag` | catalogue pages for offline reading |

Never mirror server data into `shared_preferences`; `GET /me` is the single source of
truth for profile and preferences (the server refuses `currentLevel` on `PATCH /me` on
purpose — the placement test owns it).

### 5.3 Hygiene

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
| Sign-in (Google + Apple) | `POST /auth/google`, `POST /auth/apple` | **first sign-in creates the account** — there is no register screen and no forgot-password screen, ever |
| Home shell | `GET /me` | greeting, level chip, daily goal (goal value from `/me` today) |
| Profile | `GET/PATCH /me` | cannot edit `currentLevel`; omit a field to leave it untouched |
| Devices | `GET/DELETE /me/devices` | shows last-seen; revoking a device revokes its sessions |
| Close account | `DELETE /me` | destructive: confirm dialog + re-auth; no grace period exists server-side |
| Sign out | `POST /auth/logout` | `allDevices: false` default; clears local tokens regardless |

Advisory fields on sign-in (`displayName`, `nativeLanguage`, `englishVariant`,
`timezone`, `locale`) apply **only when the account is created**; they never overwrite
an existing learner's choices.

### 6.2 Content catalogue — LIVE

| Screen | Endpoint | Design notes |
| --- | --- | --- |
| Levels | `GET /content/levels` | public (no auth gate!) — usable pre-sign-in; shows unit counts |
| Units of a level | `GET /content/levels/{code}/units` | cursor pagination; infinite scroll |
| Lessons of a unit | `GET /content/units/{id}/lessons` | cursor pagination |
| Lesson detail | `GET /content/lessons/{id}` | questions **without** `correctIndex`, `acceptedAnswers`, `explanation` |

- All four are cacheable: `ETag` + `Cache-Control: max-age=60,
  stale-while-revalidate=600`. Cache the body + ETag in drift; revalidate with
  `If-None-Match` and serve `304` from cache while offline.
- Rate limit 240/min per IP — generous, but still surface `429`.
- The lesson screen today is a **reader**, not a quiz: questions render, nothing can
  be submitted. Label it honestly in the UI ("practice coming soon") rather than
  showing dead answer buttons.

### 6.3 Quiz + progress — PHASE 1 (unlocks the app's core loop)

| Screen | Endpoints |
| --- | --- |
| Quiz session player | `POST /quiz/sessions` (start), `GET /quiz/sessions/{id}` (**resume after app kill**), `POST .../answers` (one per question, returns `correct` + `explanation`), `POST .../complete` (XP, unlock, streak), `POST .../abandon` (returns hearts) |
| Home / dashboard | `GET /progress/summary` (streak, xp, accuracy, hearts, reviews due) |
| Lesson map | `GET /progress/lessons/{id}` (locked / available / completed), `GET /progress/unlocks` |
| XP history | `GET /progress/xp` |

Client rules: submit answers **one at a time** with an idempotency key; the session
must survive process death (persist `sessionId`, call `GET /quiz/sessions/{id}` on
resume); question objects arrive with **no answer key** — correctness only comes back
per answer.

### 6.4 Spaced repetition — PHASE 2

| Screen | Endpoints |
| --- | --- |
| Daily review | `GET /review/due?limit=20` (`hasMore=false` when empty), `POST /review/cards/{id}/answer` |
| Review stats | `GET /review/stats`, `GET /review/preview` |

Empty state matters: "nothing due today" is a success, not an error
(`REVIEW_NOTHING_DUE` exists for the answer path).

### 6.5 Gamification — PHASE 3

| Screen | Endpoints |
| --- | --- |
| Hub (hearts, streak, daily goal in one view) | `GET /gamification/summary` |
| Achievements | `GET /achievements`, `POST /achievements/{code}/claim` (`ALREADY_CLAIMED` → refresh list, not an error toast) |
| Streak detail | `GET /streak`, `POST /streak/freeze` (premium only → `PREMIUM_REQUIRED` opens paywall) |

### 6.6 Subscriptions — PHASE 4

| Screen | Endpoints |
| --- | --- |
| Paywall / plans | `GET /plans` |
| Current subscription | `GET /subscription` |
| Checkout | `POST /subscription/checkout` → open the returned Stripe URL (WebView / native) |
| Cancel | `POST /subscription/cancel` |
| Billing portal | `POST /subscription/portal` |

Never build a client-side "is premium" flag: read `GET /subscription` (or the
`premium` field on summary) on app start and after every purchase flow. The webhook
(Stripe → server) is the source of truth; the app only reflects it.

### 6.7 Notifications — PHASE 5

| Screen | Endpoints |
| --- | --- |
| Inbox | `GET /notifications?cursor=`, `POST /notifications/{id}/read`, `POST /notifications/read-all`, `DELETE /notifications/{id}` |
| Badge | `GET /notifications/unread-count` (poll lightly or refresh on foreground) |
| Push token registration | FCM token goes in `device.pushToken` on sign-in / refresh |

Preference switches already exist today under `PATCH /me`
(`notificationPreferences` — `dailyReminder`, `streakWarning`, `reviewDue`,
`weeklyReport`, `quietHoursStart/End`): build the toggles now, wire them to `/me`;
delivery itself waits for phase 5.

### 6.8 Media — PHASE 6

Presigned upload: `POST /media/presign` → upload directly to S3 → `POST /media/confirm`
→ read via `GET /media/{id}` (signed URL, 15 min). Relevant for speaking/pronunciation
answers later; do not stream audio through the API server.

### 6.9 AI — PHASE 7

| Screen | Endpoints |
| --- | --- |
| "Explain this answer" | `POST /ai/explain` (synchronous, short) |
| Writing feedback | `POST /ai/drafts` → `GET /jobs/{id}` (poll) |
| Quota display | `GET /ai/quota` |

Design for failure: `AI_QUOTA_EXCEEDED`, `AI_TIMEOUT`, `AI_PROVIDER_ERROR` are normal
outcomes — degrade to the static explanation already stored with the question.

### 6.10 Vocabulary & grammar — PHASE 9

`GET /vocabulary`, `GET /vocabulary/{word}`, `POST /vocabulary/{word}/favorite`,
`GET /grammar/topics`, `GET /grammar/topics/{id}`.

### 6.11 Not for the phone app

- **Admin content authoring** (`/admin/content/*`) — web only, role-gated
  (`content_editor`, `admin`). Do not build it into the learner app.
- `GET /me/export` (phase 0.9, GDPR) — a "download my data" row in Settings that
  triggers a share sheet is enough.

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
        GoRoute(path: '/review', ...),      // PHASE 2 — hide tab until then
        GoRoute(path: '/progress', ...),    // PHASE 1 — hide tab until then
        GoRoute(path: '/profile', ...),
      ],
    ),
    GoRoute(path: '/lesson/:id', ...),
    GoRoute(path: '/quiz/:sessionId', ...), // PHASE 1
  ],
);
```

- **Public routes:** `/splash`, `/sign-in`, `/learn` catalogue browsing. The catalogue
  is deliberately unauthenticated — a new learner can see the course before signing in.
- **Future tabs are hidden, not disabled** — a greyed "Review" tab implies a feature
  that does not exist yet.
- Deep link `natiqo://lesson/{id}` → resolve through the same router; guard with the
  auth redirect so a cold deep link cannot bypass sign-in for protected screens.

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
- **Levels/units/lessons:** list tiles with counts; prefetch the next cursor page when
  the last visible item is within 5 of the end; keep scroll position on back
  (ValueKey per route + `AutomaticKeepAlive`).
- **Lesson reader:** question card with choice buttons / true-false / text field per
  `kind` (`multiple_choice`, `true_false`, `fill_blank`); disable interaction with a
  gentle "answers open after the update" affordance until phase 1 ships.
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
| **M1** | Sign-in (Google/Apple), session restore, shell, catalogue: levels → units → lessons with cursor paging + ETag cache | live endpoints | **buildable now** |
| **M2** | Lesson reader, profile (view/edit), devices, settings + notification preference toggles, close account | live endpoints | **buildable now** |
| **M3** | Quiz player + home dashboard + lesson map + XP history (**the core loop**) | **backend Phase 1** | blocked |
| **M4** | Daily review (SRS) + review tab | **Phase 2** | blocked |
| **M5** | Gamification hub, achievements, streak detail | **Phase 3** | blocked |
| **M6** | Notification inbox + FCM push + media upload for speaking tasks | **Phases 5–6** | blocked |
| **M7** | Paywall, checkout, subscription management | **Phase 4** | blocked |
| **M8** | AI explanations / writing feedback with graceful fallback | **Phase 7** | blocked |
| **M9** | Vocabulary & grammar sections | **Phase 9** | blocked |

**Parallel work while blocked (M3+):** finish M0–M2 to production polish; build the
UI for quiz screens against fixture JSON with the repository interface already
defined — swapping fixtures for HTTP later is a one-file change — but do **not**
ship dead screens.

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
| Widgets | `flutter_test` + golden | the five screen states; RTL layout |
| E2E | `integration_test` against staging | sign-in → browse → lesson; token expiry across 15 min (fast-forward via injected clock) |

Rules:

- Generate models with `freezed` + `json_serializable` **from `openapi.json`** —
  hand-written DTOs drift from the wire and the server does not negotiate.
- Never assert on `message` text; assert on `code`.
- CI (Codemagic or GitHub Actions): analyze → test → coverage; block merges on red.

---

## 11. Performance checklist

- `ListView.builder` (or `ListView.separated`) for every remote list — the catalogue
  grows to 6 levels / 18 units / 37 lessons / 114 questions.
- Cursor pagination + prefetch (§8); never `offset`-style loading.
- ETag/`If-None-Match` revalidation before refetching catalogue pages; serve cached
  body during offline and after `max-age` while `stale-while-revalidate` (600 s)
  applies.
- Images: `cached_network_image` with fixed aspect ratios (no layout jumps); audio
  via just_audio with pre-buffering, streamed from signed S3 URLs — never proxied
  through the API.
- Parse large payloads off the main isolate (`compute`) only after profiling —
  lesson JSON here is < 1 KB.
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
- [ ] No screen for a feature that is still `404` (tabs hidden, not disabled)
- [ ] `PATCH /me` sends only changed fields; `currentLevel` never sent
- [ ] RTL layout verified for Arabic; both locales in the store listing
- [ ] Close-account flow explains it is immediate and permanent
- [ ] `flutter analyze --fatal-infos` + tests green in CI

---

## 13. Keeping up with the backend

- Subscribe to [`backend-roadmap-ar.md`](backend-roadmap-ar.md) — each phase lists the
  exact endpoints it adds, their error codes and the product decisions pending.
- Treat `GET /openapi.json` as the contract: diff it in CI (a breaking change there
  should fail the mobile build before it fails a user).
- Error codes are already reserved for future phases (`QUIZ_*`, `INSUFFICIENT_HEARTS`,
  `PREMIUM_REQUIRED`, `AI_QUOTA_EXCEEDED`, `REVIEW_NOTHING_DUE`, …) — see
  `app/core/errors.py`; handle unknown codes defensively (map to a generic
  `ServerFailure` with the raw code kept for logs) so a new backend code never crashes
  an old app build.
