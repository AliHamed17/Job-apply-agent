# Job Apply Agent control plane

This is a physically isolated FastAPI application for Vercel. It carries only
redacted coordination metadata. Candidate identity, job URLs, CV identifiers or
hashes, answers, cover letters, browser state, and Ollama inputs stay on the
private Windows runner.

This directory is deployable code, not proof of a current production
deployment. It also does not enable employer submission. The first-five ATS
adapters remain fixture-qualified only: 87 sanitized fixtures, zero real-URL
dry runs, zero live canaries, zero qualified form scopes, and zero final
executors.

## Trust model

- `GET /health/live` is the only unauthenticated informational route.
- An operator token can only be exchanged for a short-lived opaque session.
  The session cookie is `HttpOnly` and `SameSite=Strict`; production also uses
  `Secure`. Every operator mutation requires the exact configured Origin and a
  session-bound CSRF token.
- Invalid operator tokens enter one fixed-size, process-local bucket allowing
  eight attempts per five-minute window after constant-time token verification.
  Invalid attempts
  never check out a database connection and never create audit rows; valid
  tokens bypass the denial limiter.
- Session cookies carry a purpose-separated HMAC proof. Missing, malformed, and
  random cookies are rejected before database access. A valid session is loaded
  read-only, the exact migration head is verified, and only then is last-seen
  state updated. Operator audit history is retained for 30 days with a
  serialized 5,000-row hard cap.
- Runner routes accept canonical Ed25519-signed envelopes. Purpose, audience,
  key identity, issued/expiry timestamps, five-minute maximum lifetime, and a
  one-use nonce are checked. Control-plane commands use a separate signing key.
- Expired runner nonces are retained beyond every valid envelope lifetime and
  then pruned transactionally, keeping replay protection bounded in storage.
- A locally signed review grant can produce at most one command. Commands are
  single-use and expire within five minutes. Preview and non-production
  deployments cannot dispatch.
- Superseding a local review produces a signed, replay-safe revocation
  tombstone. Revocations drain before new grant projection or command polling,
  remain authoritative if messages reorder, and cancel any stale command that
  has not been acknowledged by the private runner.
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
- `VERCEL_PROJECT_ID` (provided by Vercel system environment variables)
- `CONTROL_DATABASE_URL` — dedicated PostgreSQL URL with TLS `sslmode`
- `CONTROL_PUBLIC_ORIGIN` — exact HTTPS origin, with no path/query/credentials
- `CONTROL_OPERATOR_TOKEN` — random secret, at least 32 bytes
- `CONTROL_SESSION_SECRET` — distinct random secret, at least 32 bytes
- `CONTROL_CSRF_SECRET` — distinct random secret, at least 32 bytes
- `CONTROL_SIGNING_PRIVATE_KEY_B64` — base64url Ed25519 seed
- `CONTROL_SIGNING_KEY_ID` — UUID for the control signing identity
- `CONTROL_RUNNER_PUBLIC_KEY_B64` — base64url Ed25519 public key
- `CONTROL_RUNNER_DEVICE_ID` — UUID for the private runner identity
- `CONTROL_IDENTITY_BUNDLE_DIGEST` — schema-v2 target/bundle attestation,
  written last and recomputed at startup

Never commit any value for those variables. Preview deployments should receive
separate non-production values; they remain unable to dispatch.

From a private Windows host, `scripts/control_plane_identity.py
configure-vercel` can configure only the seven identity-derived `CONTROL_*`
values plus the final `CONTROL_IDENTITY_BUNDLE_DIGEST`. Its `--dry-run` never
decrypts the DPAPI bundle or invokes Vercel. The live command requires an exact
linked project/scope from a clean local staging checkout outside OneDrive,
synced folders, and reparse points, plus either a SHA-256/version-pinned package-internal
native Vercel executable or separately SHA-256-pinned absolute `node.exe` and
`vercel\dist\vc.js` files. The npm `.cmd` shim is never executed. Both Node/JS
files are rehashed before every write, and values travel only over stdin with
a sanitized process environment. The helper writes the bundle digest last, so
a partial or mixed update makes the next Production/Preview startup fail
closed. It resolves non-decrypted metadata and creates or patches only the
exact environment-scoped record; key-only `--force` upserts are prohibited so
Preview and Production identities cannot collapse into one record. A bounded
second metadata read must prove all eight target records and preserve the
other environment before the helper reports success. It never configures the
database, public origin, or `APP_ENV`. The
separate `validate-selection` command checks DPAPI-protected schema-v2 target
metadata and both private/public signing-key bindings without printing private
material. The separate `copy-operator-token` command uses an owned,
sequence-bound native Windows clipboard lease and clears after a bounded TTL
only if the clipboard is unchanged. See the repository-level bootstrap
runbook for the exact commands.

Enable Vercel system environment variables so `VERCEL_URL` and
`VERCEL_PROJECT_ID` are available. `VERCEL_ORG_ID` is not a Vercel runtime
system variable and is neither required nor trusted. The expected scope ID
comes from the schema-v2 identity digest.

In Project Settings > Security, enable
**[Secure Backend Access with OIDC Federation](https://vercel.com/docs/oidc)**
and select **Global issuer mode**. At runtime Vercel places a short-lived token
in the `x-vercel-oidc-token` request header. Every Production or Preview request
except minimal `GET`/`HEAD /health/live` fails closed unless the control plane
verifies the Vercel RS256 signature and exact issuer, audience, subject, time
window, environment, project ID, and owner ID. The signed `owner_id` must equal
the scope retained from the identity digest. Team issuer mode is intentionally
rejected so an unauthenticated token cannot select a team-specific network
path. The only accepted issuer is `https://oidc.vercel.com`, and the only JWKS
request is `https://oidc.vercel.com/.well-known/jwks`. Retrieval does not follow
redirects and is bounded by response, key-count, one-entry cache, TTL, and
refresh limits.
Vercel documents one-hour deployment tokens and twelve-hour development
tokens. Its Python runtime can fall back to the signed environment token when
the internal request token is unavailable. The verifier therefore requires the
exact Preview/Production target and current expiry before applying a hard
twelve-hour lifetime ceiling; development-scoped tokens remain invalid.
Do not create a persistent replacement token or configure a caller-supplied
scope variable. OIDC authenticates the Vercel deployment, not the human
operator; the operator session, Origin, and CSRF controls remain mandatory.

The exact deployment hostname is trusted only for host validation and liveness
checks in production; login, CSRF, and mutations remain restricted to
`CONTROL_PUBLIC_ORIGIN`. In preview, the exact `VERCEL_URL` is the
non-dispatching operator origin, so protected preview tests remain possible
without a wildcard origin.

Set the Vercel Project Root exactly to this `control_plane` directory. Its
`requirements.txt`, `pyproject.toml`, and `vercel.json` are authoritative; do
not build the function from the parent application's dependency manifest. The
`[tool.vercel]` FastAPI entrypoint must remain on Vercel's current Python
framework runtime. Legacy `builds`/`routes` manifests are prohibited because
they bypass current framework request handling, including the request-scoped
OIDC header required above. The repository-root fallback manifest declares the
same `fastapi` framework and its explicit upload allowlist must include this
directory's `pyproject.toml` and `vercel.json`.

Use Preview only with isolated Preview data and identities. For an immutable
production candidate, disable automatic production-domain assignment, run
`vercel deploy --prod --skip-domain`, verify that staged Production deployment,
and then run `vercel promote <deployment-url> --yes`. Promoting a Preview
triggers a Production rebuild; promoting a staged Production deployment only
assigns the domain and preserves the verified artifact.

## Restore safety

A restored database contains stale one-use authority and must never be attached
directly to a deployment. From the repository root, before reconnecting it:

```powershell
python scripts/quarantine_restored_control_plane.py --dry-run
python scripts/quarantine_restored_control_plane.py --apply
```

The quarantine deactivates restored devices, revokes operator sessions, and
rejects undelivered queued/claimed commands. It does not create runner events,
submission evidence, or signed revocation envelopes. Run it a second time and
require zero changed rows, then generate a new device UUID and new Ed25519
identities. Old devices are never reactivated.

See the repository-level
[control-plane bootstrap](../docs/control-plane-bootstrap.md) and
[backup/restore runbook](../docs/control-plane-backup-restore.md).

## Local verification

From this directory:

```powershell
python -m pytest -q tests
python -m ruff check .
python -m ruff format --check .
```

See [MIGRATIONS.md](MIGRATIONS.md) for the dedicated database lifecycle.
