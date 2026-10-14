# Client contract

What a Flutter client can rely on. Everything here is pinned by a test, so a change that
breaks one of these promises fails the build rather than surfacing as a support ticket.

The generated source of truth is `GET /openapi.json` (development only). This document
covers the things a schema cannot express: caching, redirects, and which failures are
deliberate.

Every versioned operation publishes the error envelope below for 400, 401, 403, 404, 409,
422, 429 and 500, so a generated client deserialises real failures rather than a stock
validation shape. For the phone app see
[the Arabic mobile integration guide](mobile-integration-ar.md).

## Field naming

**Responses are camelCase. Requests accept both spellings.**

```jsonc
// Response — always camelCase
{ "meta": { "limit": 20, "hasMore": false, "nextCursor": null },
  "items": [ { "id": "…", "unitCount": 3, "estimatedMinutes": 5 } ] }

// Request — either is accepted
{ "displayName": "Ali" }     // documented form
{ "display_name": "Ali" }    // still accepted
```

This is deliberate migration insurance: the server and the app ship independently, so for
at least one release window an old build and a new server must both work. Generate models
from camelCase. Once no client is on the old build, the snake_case path can be dropped —
but dropping it is a decision, not an oversight.

`extra="forbid"` still applies to request bodies, so an unknown field is a `422` rather
than being silently discarded. That is what stops `currentLevel` being smuggled in through
`PATCH /api/v1/me`.

## Errors

Every error has the same shape. Branch on `code`; `message` is for humans and may change.

```json
{
  "error": {
    "code": "LESSON_NOT_FOUND",
    "message": "There is no published lesson with that id.",
    "requestId": "fb11336eeb1745bf82510f84206fdf39"
  }
}
```

`requestId` is echoed in the `X-Request-ID` response header and appears in the server logs.
Send it in a bug report. It is accepted from the client as `X-Request-ID` on the way in,
sanitised first, so you can correlate your own trace id with ours.

### Which failure you get, and why

| Situation | Status | Code |
| --- | --- | --- |
| A published lesson does not exist | `404` | `LESSON_NOT_FOUND` |
| The lesson exists but is not published | `404` | `LESSON_NOT_FOUND` |
| A lesson id is not a valid UUID (`/content/lessons/not-a-uuid`) | `422` | `VALIDATION_ERROR` |
| Rate limited | `429` | `RATE_LIMITED` |

A malformed **UUID** is rejected as validation, because it cannot be a row id at all and
there is nothing to probe. A well-formed UUID that names an unpublished lesson is answered
exactly like one that names nothing at all, so the endpoint cannot be used to probe which
lessons exist but are not published.

So: treat `404` as "ask the user for something else" and `422` as "you built the request
wrong". Do not retry a `404`; do not show it as an error when a page simply has no content.

## Caching

Only the two catalogue routes (`/content/tree` and `/content/lessons/{id}`) are cacheable.
**Everything else is `no-store`** — including
every authenticated response, so a profile is never left in a shared phone's HTTP cache.

A cacheable `200` carries:

```
Cache-Control: public, max-age=60, stale-while-revalidate=600
ETag: "b1d0c659d85275a94051d06b4702d1f1"
Vary: Accept-Encoding, Origin
```

The intended client loop:

1. `GET` the resource.
2. Store the body **and** the `ETag`.
3. On the next launch, re-send it: `If-None-Match: "b1d0…"`.
4. On `304`, use the stored body. The response carries the same `Cache-Control`, so the
   stored copy's freshness is renewed and step 3 happens once a minute, not once a launch.

Do not cache a `404`. It means "not published", not "never will be" — a `404` cached for
the stale window would hide content the moment an editor publishes it.

`ETag` differs between the gzip and identity representations of the same resource, so
**store the tag alongside the encoding you actually received** and never reuse a tag across
a different `Accept-Encoding`.

## Rate limiting

Per client IP, per scope, per one-minute window. Exceeding it returns `429` with
`Retry-After: 60`.

| Scope | Default |
| --- | --- |
| Auth (`/auth/*`) | 10 / min |
| Public catalogue (`/content/*`) | 240 / min |
| Everything else | 120 / min |

`Retry-After` is seconds. Back off for at least that long; do not retry immediately, and do
not treat `429` as a fatal error — it is a pacing signal. A `429` response body carries the
standard error envelope, so existing error handling covers it.

The catalogue budget is separate from the default on purpose: browsing the curriculum should
never spend the allowance of a token refresh happening at the same time.

## Responses worth knowing about

**A trailing slash redirects.** `GET /api/v1/content/tree/` answers `307` to
`/api/v1/content/tree`, **preserving the query string**. Build paths without the trailing
slash; if you do hit the redirect, make sure your HTTP client re-sends the query string, or
`limit` and `cursor` are dropped and you land on page one of a collection you were paging
through.

**Responses are gzipped** above 400 bytes when the client advertises `Accept-Encoding: gzip`.
Handle it transparently — Flutter's `http` package does.

**Request bodies are capped.** `413` with `PAYLOAD_TOO_LARGE`; the error `details` carry both
`max_bytes` and the received size.

**Security headers are always present** — `X-Content-Type-Options`, `Referrer-Policy`,
`Content-Security-Policy`, `Permissions-Policy`, and `Strict-Transport-Security` in
production. `X-Content-Type-Options: nosniff` means never `eval` a response body.

## Pagination

Collections are cursor-paginated, never offset-paginated:

```jsonc
{ "meta": { "limit": 20, "hasMore": true, "nextCursor": "eyJ2Ijox…" } }
```

Pass `nextCursor` back verbatim as `?cursor=`. It is opaque, signed data — do not parse it,
construct it, or persist it across schema changes. Stop when `hasMore` is `false` or
`nextCursor` is `null`.

`limit` defaults to 20 and is capped at 100; beyond that it is a `422`. There is no offset
parameter, because offset pagination skips or repeats rows whenever the catalogue changes
underneath a learner who is scrolling.