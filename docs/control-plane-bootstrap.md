# Control-plane bootstrap

The Vercel application is a redacted coordination surface. It is not the
browser worker and it does not store candidate identity, employer URLs, CV
identifiers or hashes, answers, cover letters, page content, browser state, or
Ollama prompts.

## Current delivery boundary

Bootstrap does not enable employer submission. The five implemented ATS
families are fixture-qualified only: 87 sanitized fixtures, zero real-URL dry
runs, zero live canaries, zero qualified form scopes, and zero final
executors. See the generated
[qualification matrix](qualification/adapter-matrix.md).

Discovery and preparation may run locally. The operator must still review the
exact private form plan and choose **Send application**. At the current
qualification level the final-action gate remains closed.

## 1. Prepare the private Windows runner

Keep these outside the repository and outside OneDrive:

- `user_profile.yaml`, `cv_routing.yaml`, and `cvs/`;
- profile/CV version storage;
- Playwright employer profiles and LinkedIn browser state;
- the runner environment file and signing keys;
- local Ollama state.

Install and verify local `qwen2.5:7b`. Production configuration requires the
loopback Ollama endpoint and has no automatic cloud fallback:

```powershell
ollama pull qwen2.5:7b
ollama serve
```

Do not copy Chrome or Edge password stores. Sign in through the isolated
Playwright profile and complete MFA manually.

## 2. Create fresh device identities

The preferred managed bootstrap requires PowerShell 7.2 or newer, Docker
Desktop, and a clean checkout whose `HEAD` exactly matches `origin/main`. Invoke
the entry point with `pwsh`; stock Windows PowerShell 5.1 is intentionally not
supported:

```powershell
pwsh -NoProfile -File .\scripts\job_agent.ps1 bootstrap `
  -RepositoryPath "C:\absolute\path\to\Job-apply-agent" `
  -ControlPlaneUrl "https://your-control-plane.example" `
  -VercelProjectId "prj_12345678abcdef" `
  -VercelScopeId "team_12345678abcdef"
```

This creates the fail-closed runtime environment and fresh identity bundle
under the current user's local application-data directory, outside the
repository and OneDrive. It installs the exact owned runner task without
starting it. Use the lower-level identity command below only for an explicitly
managed custom provisioning flow.

Bootstrap creates empty private-data roots; it does not copy personal files or
browser sessions from the checkout. Before `start`, place the reviewed private
profile, routing configuration, CVs, and version storage under
`$env:LOCALAPPDATA\JobApplyAgent\profile-data`. Browser sessions created by the
managed runner live under
`$env:LOCALAPPDATA\JobApplyAgent\browser-state`. Prefer signing in again through
that isolated profile; if an existing isolated profile is deliberately
migrated, treat it as a live credential and follow
[Backup and restore](control-plane-backup-restore.md).

Generate identities into an ACL-restricted local directory. The command emits
only public identifiers and paths; it does not print private keys or operator
secrets:

```powershell
python scripts/control_plane_identity.py create `
  --repository-root "C:\absolute\path\to\Job-apply-agent" `
  --control-plane-url "https://your-control-plane.example" `
  --runtime-env-path "C:\private\JobApplyAgent\runtime.env" `
  --vercel-environment production `
  --vercel-project-id "prj_12345678abcdef" `
  --vercel-scope-id "team_12345678abcdef"
```

The schema-v2 identity binds the protected payload and public manifest to one
exact Vercel environment, project ID, and scope ID. A schema-v1 or cross-target
identity is rejected. The default identity root is under the current user's
local application-data directory. A repository path, OneDrive path, UNC path,
relative path, or reparse-point path is rejected.

Every restore or suspected key exposure requires a new device UUID, runner key
pair, control signing key pair, operator token, session secret, and CSRF
secret. Never reactivate an old device row.

### Configure identity-derived Vercel variables

The identity command intentionally never prints private material. On Windows,
perform Vercel linking, identity configuration, and deployment from a clean
main-derived staging checkout on a local NTFS path outside OneDrive or any
other synced/reparse directory. A checkout under `OneDrive - ...` is
intentionally rejected even when all files are pinned locally. Keep the
working/personal checkout untouched; create the staging checkout at a path
such as `C:\JobApplyAgentDeploy`, verify it is clean and at the approved main
commit, and use that exact path consistently for `--repository-root`,
`--vercel-cwd`, link, deploy, verification, and promotion.

On that staging checkout,
install the official `vercel` npm package at an exact version, link the
`control_plane` directory to the existing project, and inspect
`.vercel/project.json`. It must contain the exact `projectId` and `orgId` you
intend to configure. Never auto-link from this helper. The npm `.cmd` shim is
intentionally rejected. Resolve and separately pin the absolute `node.exe`
and package-internal `vercel\dist\vc.js` entrypoint, then run the no-secret
preview first:

```powershell
$vercelCliVersion = "58.1.0" # example only; use the exact reviewed version
npm install --global "vercel@$vercelCliVersion"
$nodeExe = (Get-Command node.exe -CommandType Application -ErrorAction Stop).Source
$globalNpmRoot = (& npm root --global).Trim()
$vercelJs = (Resolve-Path -LiteralPath (
  Join-Path $globalNpmRoot 'vercel\dist\vc.js'
) -ErrorAction Stop).Path
if (-not [System.IO.Path]::IsPathFullyQualified($vercelJs)) {
  throw 'VERCEL_JS_ENTRYPOINT_NOT_ABSOLUTE'
}
$nodeSha256 = (Get-FileHash -LiteralPath $nodeExe -Algorithm SHA256).Hash.ToLowerInvariant()
$vercelJsSha256 = (Get-FileHash -LiteralPath $vercelJs -Algorithm SHA256).Hash.ToLowerInvariant()
python scripts/control_plane_identity.py configure-vercel `
  --repository-root "C:\absolute\path\to\Job-apply-agent" `
  --vercel-node $nodeExe `
  --vercel-node-sha256 $nodeSha256 `
  --vercel-js-entrypoint $vercelJs `
  --vercel-js-entrypoint-sha256 $vercelJsSha256 `
  --vercel-cli-version $vercelCliVersion `
  --vercel-cwd "C:\absolute\path\to\Job-apply-agent\control_plane" `
  --environment production `
  --project "prj_12345678abcdef" `
  --scope "team_12345678abcdef" `
  --dry-run
```

The dry run validates the selected external schema-v2 identity, exact linked
cwd/project/scope plus both CLI file paths and digests, but does not decrypt
the DPAPI bundle or invoke Vercel. Review the reported `node_js` mode, eight
variable names, project, scope, environment, and expected CLI version, then
repeat without `--dry-run`. Before decrypting, the live command invokes the
exact command shape `[node.exe, absolute\vercel\dist\vc.js, --version]`.
It then requests only non-decrypted environment metadata and matches records
by exact key plus exact built-in target. Before every secret write it rehashes
both files, then creates a missing target record or patches that exact record
ID through `vercel api`. It never uses the key-only `env add --force` upsert,
because that can collapse distinct Preview and Production records. Sensitive
PATCH bodies omit the immutable key. Request bodies travel only through the
pinned process's standard input with a sanitized environment and fixed cwd;
secret values never enter argv, environment variables, stdout, stderr, or
this repository. Metadata with a decrypted value, a combined target, a
branch/custom target, duplicate or aliased record IDs, a hidden Production
record, an incomplete page, or an invalid record ID fails closed before the
DPAPI bundle is decrypted. CLI version output and non-decrypted metadata are
bounded and discarded; secret-bearing command output is suppressed directly
to the null device. After writing the digest, the helper fetches a second
non-decrypted complete inventory and returns success only when all eight exact
target records exist, every pre-existing record ID is unchanged, and the
other environment's identity records are unchanged.

The helper also accepts the official native package. npm exposes package bins
through shims, so do not use `Get-Command vercel.exe`. Resolve the
package-internal PE exactly:

```powershell
npm install --global "@vercel/vc-native@$vercelCliVersion" --force
$vercelNativePackageRoot = (Resolve-Path -LiteralPath (
  Join-Path $globalNpmRoot '@vercel\vc-native'
) -ErrorAction Stop).Path
$vercelCli = (Resolve-Path -LiteralPath (
  Join-Path $vercelNativePackageRoot 'bin\vercel.exe'
) -ErrorAction Stop).Path
$vercelCliSha256 = (Get-FileHash -LiteralPath $vercelCli -Algorithm SHA256).Hash.ToLowerInvariant()
```

Use `--vercel-cli $vercelCli --vercel-cli-sha256
$vercelCliSha256` instead of the four Node/JS arguments. The helper verifies
the package-internal executable's exact SHA-256 and reported version before
decrypting, and rehashes it before every write. The package sources are the
official [`vercel`](https://www.npmjs.com/package/vercel) and
[`@vercel/vc-native`](https://github.com/vercel/vercel/tree/main/packages/vc-native)
distributions.

This command configures only:

- `CONTROL_OPERATOR_TOKEN`, `CONTROL_SESSION_SECRET`, and
  `CONTROL_CSRF_SECRET`;
- `CONTROL_SIGNING_PRIVATE_KEY_B64` and `CONTROL_SIGNING_KEY_ID`;
- `CONTROL_RUNNER_PUBLIC_KEY_B64` and `CONTROL_RUNNER_DEVICE_ID`;
- `CONTROL_IDENTITY_BUNDLE_DIGEST`, written last.

Production and Preview recompute the bundle digest from all seven identity
values plus the bound version/environment/project/scope. A failed partial
update leaves the prior digest in place, so the next startup rejects the mixed
bundle. Rerunning the same exact command is idempotent and writes the digest
only after all seven values succeed.

It does not provision PostgreSQL and does not configure `APP_ENV`,
`CONTROL_DATABASE_URL`, `CONTROL_PUBLIC_ORIGIN`, or any unrelated Vercel
variable. Configure those separately through the protected Vercel interface.
For Preview, create a completely separate identity root and database, pass that
root with `--root`, and use `--environment preview`. Never configure Preview
from the Production selection.

When an operator login is needed, copy the selected token without printing it:

```powershell
python scripts/control_plane_identity.py copy-operator-token `
  --repository-root "C:\absolute\path\to\Job-apply-agent" `
  --ttl-seconds 60
```

Paste it directly into the protected control-plane login. The command remains
open for the bounded TTL and clears the clipboard only if both its native
sequence number and text are unchanged. It retries transient clipboard locks
for a small bounded grace period; if safe cleanup still fails, it exits nonzero
and warns the operator to clear the clipboard manually. If clipboard content
changed—even away and back to the same text—it leaves the replacement
untouched. Run both subcommands as the same Windows user that created the DPAPI
bundle.

## 3. Provision the dedicated database

Create a dedicated PostgreSQL database for the control plane. It must not be
the private application database. Production startup requires a network
PostgreSQL URL with TLS `sslmode=require`, `verify-ca`, or `verify-full`.

From `control_plane/`, apply the dedicated migrations:

```powershell
python -m alembic upgrade head
```

Confirm that the recorded revision equals
`job_control_plane.db.EXPECTED_SCHEMA_REVISION`.

## 4. Configure Vercel

Set the Vercel project root to `control_plane`. Add production secrets through
Vercel's protected environment-variable interface; never commit them or paste
them into an issue, PR, chat, or build log.

Required variables are listed in
[`control_plane/README.md`](../control_plane/README.md). Preview deployments
use separate non-production values and cannot dispatch commands. Production
and preview must never share a database, operator token, signing key, or runner
identity. Enable Vercel system environment variables; startup requires the
platform-supplied `VERCEL_PROJECT_ID` and `VERCEL_ENV` to match the schema-v2
bundle target. `VERCEL_ORG_ID` is not a runtime system variable and must not be
used as scope evidence. The expected scope remains bound inside the schema-v2
digest.

In the Vercel project's **Settings > Security**, enable
**[Secure Backend Access with OIDC Federation](https://vercel.com/docs/oidc)**
and select **Global issuer mode** before deploying either environment. Team
issuer mode is not accepted. The fixed global mode prevents an unauthenticated
token from selecting a team-specific JWKS path: the verifier accepts only
`https://oidc.vercel.com` and fetches only
`https://oidc.vercel.com/.well-known/jwks`. Vercel then places its signed token
in `x-vercel-oidc-token` on each function request. The verifier honors the
signed expiry and rejects declared lifetimes beyond a twelve-hour absolute
ceiling. This matches deployed Preview behavior, where Vercel can reuse a
still-valid signed runtime token after its first hour, without accepting an
expired or overlong token. Bounded server-only codes distinguish an overlong
environment fallback from an overlong request token without logging token or
claim material. OIDC is deployment attestation only; it never replaces the
operator session or one-use command authority.
The isolated project uses the `[tool.vercel]` FastAPI entrypoint and Vercel's
current Python framework runtime. Do not reintroduce legacy `builds` or
`routes` entries: those bypass current framework request handling and can
prevent the request-scoped OIDC header from reaching the ASGI application.
The repository-root CLI fallback must declare the same `fastapi` framework and
allowlist the isolated `pyproject.toml` and `vercel.json`; otherwise Vercel
silently falls back to the legacy root configuration during a CLI deployment.
The control plane permits only minimal `GET`/`HEAD /health/live` without that
attestation. Every login, dashboard, readiness, operator API, and runner API
request verifies the RS256 signature against Vercel's bounded JWKS cache and
requires the signed `owner_id`, `project_id`, and `environment` to equal the
schema-v2 target. It also validates issuer, audience, subject, and token times.
If OIDC is disabled, unavailable, malformed, cross-project, cross-scope, or
cross-environment, protected requests return a generic denial before database
or command processing. Do not add a static `VERCEL_ORG_ID` or copy a build OIDC
token into project secrets.

## 5. Verify before promotion

Before merging or deploying, disable automatic assignment of the production
domain. A push to `main` must not replace the current production deployment
before the staged-production checks below pass.

First deploy a Preview build with its isolated Preview database, operator
secrets, signing identity, and runner identity. Apply the dedicated migrations
to that Preview database before deployment. Verify the Preview without enabling
the private runner:

1. `GET /health/live` returns only liveness.
2. Interactive API documentation remains disabled in production mode.
3. Unauthenticated `/` renders only the login shell and no dashboard data;
   grant, command, readiness, and runner actions remain denied.
4. Preview reports dispatch disabled.
5. No active runner device exists in the production database before the exact
   new identity is configured.

Do not promote the Preview deployment as the immutable production artifact.
Vercel rebuilds a promoted Preview with Production environment variables.
After main CI passes, back up and migrate the dedicated Production database,
then create a staged Production deployment without assigning its domains:

```powershell
vercel deploy --prod --skip-domain
```

Inspect that deployment, verify its Git commit and build result, run the
production-safe checks against its unique deployment URL, and inspect its
runtime error logs. Then promote that exact staged Production deployment:

```powershell
vercel promote <staged-production-deployment-url> --yes
```

Promotion of this staged Production deployment assigns the production domain
without rebuilding. Verify the canonical production origin immediately after
promotion. If any check fails, leave the staged deployment unpromoted or roll
back the production alias; do not start the private runner.

## 6. Start the private runner

Keep PostgreSQL, Redis, Chromium, browser profiles, CVs, and Ollama private.
The runner makes outbound TLS requests to the control plane; no inbound browser
worker port is exposed.

Deploy and migrate the control-plane server before updating this runner. The
new server accepts the legacy heartbeat during a rolling upgrade; the previous
server rejects the new operations-summary fields. After the server is promoted,
require a fresh signed summary heartbeat and matching operations digest before
considering the runner online.

Start with:

```dotenv
DRY_RUN=true
DRAFT_ONLY=true
PORTAL_FINAL_SUBMIT_ENABLED=false
```

Use the managed commands from the clean main-derived checkout:

```powershell
pwsh -NoProfile -File .\scripts\job_agent.ps1 start
pwsh -NoProfile -File .\scripts\job_agent.ps1 status
pwsh -NoProfile -File .\scripts\job_agent.ps1 open
pwsh -NoProfile -File .\scripts\job_agent.ps1 stop
```

`start` and `open` refuse to open the dashboard unless the loopback listener,
running Compose `web-api` publisher, authenticated runtime identity, build SHA,
worker release, and readiness snapshot agree. `stop` acts only on the exact
owned task and Compose project and preserves data volumes.

Require local readiness and a healthy signed heartbeat before allowing
preparation. A heartbeat only proves runner availability; it does not qualify
an adapter or authorize a final action.

The runner log is external to Git and the release checkout at
`%LOCALAPPDATA%\JobApplyAgent\logs\control-plane-runner.jsonl`. It contains only
structured fixed events/statuses, stable reason codes, and exception class
names. It rotates at 5 MiB with five backups and never stores exception text or
protocol payloads.

## 7. Operator acceptance

Before normal use, prove:

- private content is absent from the control-plane database and responses;
- an expired, replayed, changed, or unreviewed grant is rejected;
- an offline or deactivated runner cannot receive a command;
- only a newly minted local review grant can expose **Send application**;
- queue acceptance and button clicks remain neutral, never green;
- green is reserved for exact employer evidence tied to the attempt and CV.

If any proof fails, leave the runner stopped and follow the
[recovery runbooks](recovery-runbooks.md).
