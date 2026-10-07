# Natiqo API — Postman collection

Every endpoint the backend currently exposes, with the negative cases that matter for
auth. 20 requests, 56 assertions.

## Run it

```powershell
powershell -ExecutionPolicy Bypass -File postman\run.ps1
```

The script owns the whole lifecycle: it migrates, starts the server, waits for
`/ready`, mints a session, runs the collection, and stops the server. It fails on the
first broken assertion, so it works as a CI gate as-is.

Needs the server port free. It refuses to start if something is already listening on
8099 rather than quietly testing whatever that is — pass `-KeepServer` to reuse it.

## What is in here

| Folder | Covers |
| --- | --- |
| Health | liveness, readiness |
| Auth | sign-in rejection, refresh rotation, refresh replay, logout, revocation |
| Me | read, partial update, rejected fields, devices, revocation, account closure |

Negative coverage worth knowing about:

- A forged ID token is a `401 AUTH_IDENTITY_INVALID`, before anything is read or written.
- A replayed refresh token is a `401 AUTH_REFRESH_REUSED`, and the whole family is revoked.
- Logout really ends the chain: refreshing with the token it just consumed fails.
- `current_level` is refused on `PATCH /me`. Placement decides it, not the client.
- A well-formed but unknown device id returns `422`, the same as a malformed one. `404`
  would confirm that some other user's device id exists.
- Every error assertion checks the `code` field and that a `requestId` came back.

There is no register, login, forgot-password, reset-password or change-password request
in here because there are no such endpoints: accounts are created by the first Google or
Apple sign-in, and the server stores no password.

## Variables

They live in `dev.postman_environment.json`, not on the collection. This matters:
collection-level variables shadow globals in Postman, so a value passed with
`--global-var` is silently ignored. The scripts read and write with `pm.environment`,
which keeps a run in the Postman app and a run via `run.ps1` resolving identically.

| Variable | Set by |
| --- | --- |
| `baseUrl` | the environment file |
| `email`, `accessToken`, `refreshToken`, `refreshToken2` | `run.ps1`, from the minted sessions |
| `usedRefreshToken` | the `Refresh` request, so the replay test has the pre-rotation token |
| `deviceId` | the `List devices` request, so the revoke test has something to revoke |

## Why `run.ps1` mints a session

The API verifies an ID token against Google's or Apple's published keys, so there is no
token this repository could hand a test client that the real server would accept — and
adding an endpoint, or a request header, that makes it accept one would put an
account-takeover path into development to make testing convenient.

So `mint_session.py` calls the real `login_with_identity` use case with a stub verifier,
against the database the server under test is reading, and writes the tokens to
`session.json`. Everything except the signature check is production code: account
creation, the identity row, profile and settings defaults, and the first refresh chain
all travel the same path a real sign-in does. The collection covers the *rejection*
paths for tokens it cannot forge.

It mints two sessions — two subjects, one account — because the `Logout` test needs a
chain it can revoke without disturbing the tokens the rest of the run is using.

## Regenerating or editing

`mint_session.py` is the only helper. The collection itself is plain JSON — edit it in
the Postman app and re-run. If you add a variable, add it to the environment file too,
or `run.ps1` will not be able to override it.
