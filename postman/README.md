# Natiqo API — Postman collection

Every endpoint the backend currently exposes, with the negative cases that matter for
auth. 26 requests, 65 assertions.

## Run it

```powershell
powershell -ExecutionPolicy Bypass -File postman\run.ps1
```

The script owns the whole lifecycle: it migrates, starts the server, waits for
`/ready`, runs the collection, and stops the server. It fails on the first broken
assertion, so it works as a CI gate as-is.

Needs the server port free. It refuses to start if something is already listening on
8099 rather than quietly testing whatever that is — pass `-KeepServer` to reuse it.

## What is in here

| Folder | Covers |
| --- | --- |
| Health | liveness, readiness |
| Auth | register, login, refresh rotation, refresh replay, logout, forgot password |
| Me | read, partial update, rejected fields, devices, revocation |
| Password reset (needs a real token) | the two reset-password calls |

Negative coverage worth knowing about:

- `current_level` is rejected on both register and update. Placement decides it, not
  the client.
- A reused refresh token is a 401, and the whole family is revoked.
- `forgot-password` gives the same body for a known and an unknown address.
- A wrong password and an unknown account give the same 401 message, so neither
  reveals which field was wrong.
- A well-formed but unknown device id returns 422, the same as a malformed one. 404
  would confirm that some other user's device id exists.
- Every error assertion checks the `code` field and that a `request_id` came back.

## Variables

They live in `dev.postman_environment.json`, not on the collection. This matters:
collection-level variables shadow globals in Postman, so a value passed with
`--global-var` is silently ignored. The scripts read and write with `pm.environment`,
which keeps a run in the Postman app and a run via `run.ps1` resolving identically.

## Why password reset is a second pass

The reset token only leaves the system through the mail provider. There is no endpoint
that hands it back, and adding a debug one to make testing convenient would put a
usable account-takeover path in development.

So `mint_reset_token.py` calls the real `forgot_password` use case against a
throwaway SQLite database with an in-memory delivery adapter, and writes the token to
`reset_token.json`. `run.ps1` reads that and injects it as `resetToken`. The API under
test is the real one; only the mail delivery is stood in for.

## Regenerating or editing

`mint_reset_token.py` is the only helper. The collection itself is plain JSON — edit
it in the Postman app and re-run. If you add a variable, add it to the environment
file too, or `run.ps1` will not be able to override it.
