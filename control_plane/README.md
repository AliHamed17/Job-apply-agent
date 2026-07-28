# Job Apply Agent control plane

This is a physically isolated FastAPI application for Vercel. It carries only
redacted coordination metadata. Candidate identity, job URLs, CV identifiers or
hashes, answers, cover letters, browser state, and Ollama inputs stay on the
private Windows runner.

## Trust model

- `GET /health/live` is the only unauthenticated informational route.
- An operator token can only be exchanged for a short-lived opaque session.
  The session cookie is `HttpOnly` and `SameSite=Strict`; production also uses
  `Secure`. Every operator mutation requires the exact configured Origin and a
  session-bound CSRF token.
- Runner routes accept canonical Ed25519-signed envelopes. Purpose, audience,
  key identity, issued/expiry timestamps, five-minute maximum lifetime, and a
  one-use nonce are checked. Control-plane commands use a separate signing key.
- Expired runner nonces are retained beyond every valid envelope lifetime and
  then pruned transactionally, keeping replay protection bounded in storage.
- A locally signed review grant can produce at most one command. Commands are
  single-use and expire within five minutes. Preview and non-production
  deployments cannot dispatch.
- A lost poll response or acknowledgement receives the exact same signed
  command after a short claim lease. The private runner's durable receipt makes
  that redelivery idempotent; conflicting command content is rejected.
- Runner events have a per-command sequence. Only a pre-commit
  `inspecting`/`preparing`/`ready` transition may reset to `queued`. A terminal
  `unknown` or `legacy_unverified` outcome may receive exactly one later
  non-green operator reconciliation event; it can never become employer proof.
- OpenAPI and interactive documentation are disabled.

## Required environment

Production requires:

- `APP_ENV=production`
- `VERCEL_ENV=production` (provided by Vercel)
- `CONTROL_DATABASE_URL` — dedicated PostgreSQL URL with TLS `sslmode`
- `CONTROL_PUBLIC_ORIGIN` — exact HTTPS origin, with no path/query/credentials
- `CONTROL_OPERATOR_TOKEN` — random secret, at least 32 bytes
- `CONTROL_SESSION_SECRET` — distinct random secret, at least 32 bytes
- `CONTROL_CSRF_SECRET` — distinct random secret, at least 32 bytes
- `CONTROL_SIGNING_PRIVATE_KEY_B64` — base64url Ed25519 seed
- `CONTROL_SIGNING_KEY_ID` — UUID for the control signing identity
- `CONTROL_RUNNER_PUBLIC_KEY_B64` — base64url Ed25519 public key
- `CONTROL_RUNNER_DEVICE_ID` — UUID for the private runner identity

Never commit any value for those variables. Preview deployments should receive
separate non-production values; they remain unable to dispatch.

Enable Vercel system environment variables so `VERCEL_URL` is available. The
exact deployment hostname is trusted only for host validation and liveness
checks in production; login, CSRF, and mutations remain restricted to
`CONTROL_PUBLIC_ORIGIN`. In preview, the exact `VERCEL_URL` is the
non-dispatching operator origin, so protected preview tests remain possible
without a wildcard origin.

Set the Vercel Project Root exactly to this `control_plane` directory. Its
`requirements.txt` and `vercel.json` are authoritative; do not build the
function from the parent application's dependency manifest.

## Local verification

From this directory:

```powershell
python -m pytest -q tests
python -m ruff check .
python -m ruff format --check .
```

See [MIGRATIONS.md](MIGRATIONS.md) for the dedicated database lifecycle.
