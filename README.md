# Job Apply Agent

A private, local-first assistant for discovering jobs, routing a CV, preparing
application materials, reviewing observed ATS forms, and recording
evidence-verified outcomes.

## Current verified status

The system is **not a universal or unattended auto-applier**.

- Workday, Greenhouse, Lever, Ashby, and SmartRecruiters are
  `fixture_qualified` only.
- The committed evidence contains exactly 87 sanitized fixtures:
  Workday 9, Greenhouse 22, Lever 28, Ashby 13, and SmartRecruiters 15.
- Real-URL dry runs completed: 0.
- Live canaries completed: 0.
- Qualified form fingerprints/scopes: 0.
- Enabled final executors: 0.
- No NVIDIA, employer-tenant, or real-submission qualification is claimed.

The generated [qualification matrix](docs/qualification/adapter-matrix.md)
binds these numbers to the central registry and five report pairs.

Discovery and preparation can run continuously. Final **Send application**
remains an explicit operator action for one exact reviewed application or
reviewed batch. With the current qualification evidence, the final-action gate
stays closed.

## Submission truth

Preparing, queueing, clicking, redirecting, receiving HTTP 2xx, finding a
generic success phrase, or expecting an email never makes an application green.

Green requires employer-side evidence tied to the exact attempt, reviewed form
fingerprint, and verified CV attachment. A possible external action without
that proof becomes `unknown`, moves to review, and cannot retry automatically.

Attempt stages are:

`queued → inspecting → preparing → ready → committing → verifying → finished`

Terminal outcomes include `confirmed_submitted`, `already_applied`,
`needs_review`, `unknown`, `failed_before_commit`, `draft_only`,
`operator_confirmed`, and `legacy_unverified`. Operator reconciliation is
useful audit information but is not employer-verified green.

## Privacy and safety boundary

The private Windows runner keeps:

- `user_profile.yaml`, `cv_routing.yaml`, and every CV;
- confirmed answers, generated materials, and form plans;
- browser profiles, cookies, and employer sessions;
- Chromium and the browser worker;
- local Ollama prompts and outputs.

An optional Vercel control plane stores only redacted coordination metadata:
opaque application/grant/command references, adapter/outcome codes, timestamps,
and evidence digests.

The project does not:

- extract Chrome or Edge passwords;
- accept credentials through chat;
- solve CAPTCHA or bypass MFA/security challenges;
- use stealth escalation, proxy rotation, or automatic retry after ambiguity;
- send private application content to Vercel;
- use an automatic cloud-LLM fallback in production;
- treat a previous employer application as proof of a new submission.

## Architecture

The five bounded contexts are:

1. **Job Discovery** — ingest and deduplicate public job metadata.
2. **Application Preparation** — score, route a CV, generate bounded material,
   and abstain on unsupported facts.
3. **Submission Execution** — create a durable attempt, one-use permit, and
   database-backed command.
4. **ATS Integration** — inspect through one exact versioned candidate-browser
   adapter; authorized API transports remain separate and disabled.
5. **Evidence/Reconciliation** — verify employer evidence or retain a
   non-retryable unknown outcome.

Redis/Celery may wake work, but PostgreSQL is authoritative. The irreversible
boundary is persisted before the final action. A worker crash after that
boundary can never create an automatic duplicate.

## Local model

Production preparation uses local Ollama `qwen2.5:7b`, one inference at a time,
with schema validation, bounded timeouts, a circuit breaker, and no cloud
fallback:

```powershell
ollama pull qwen2.5:7b
ollama serve
```

Answer resolution is deterministic facts first, then exact user-confirmed
evidence, approved reusable facts, structured CV evidence, and local-LLM
synthesis for non-sensitive questions. Authorization, sponsorship,
citizenship, nationality, clearance, licensing, certifications, demographics,
consent, and attestations require confirmed evidence. No LLM runs during the
final external-action stage.

## Quick start in safe mode

Prerequisites:

- Python 3.11 or 3.13;
- PowerShell 7.2 or newer (`pwsh`) for the managed Windows runtime;
- Docker Desktop for the managed Windows runtime;
- local Ollama with `qwen2.5:7b`;
- Redis for asynchronous workers;
- PostgreSQL for production and concurrency guarantees;
- Chromium/Playwright only for explicit browser inspection.

Install:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,browser]"
playwright install chromium
copy .env.example .env
copy user_profile.yaml.example user_profile.yaml
copy cv_routing.yaml.example cv_routing.yaml
```

Keep the safe values:

```dotenv
APP_ENV=development
DRY_RUN=true
DRAFT_ONLY=true
AUTO_APPLY=false
PORTAL_FINAL_SUBMIT_ENABLED=false
LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5:7b
OLLAMA_NO_CLOUD=true
```

Run the API:

```powershell
python -m uvicorn api.main:app --port 8000
```

Open `http://127.0.0.1:8000/`. The effective-mode banner and readiness checks
must agree before preparation. A green liveness check does not mean submission
is available.

Complete the local **Candidate Profile → Complete private onboarding** form
before preparation or submission. Placeholder identity does not block
discovery, but confirmed identity and legal facts remain mandatory for later
stages. `AUTO_APPLY` is a deprecated auto-preparation alias and never enables
qualified autopilot.

For asynchronous preparation, start a worker and Beat only after Redis and the
database are ready:

```powershell
python -m celery -A worker.celery_app worker --loglevel=info
python -m celery -A worker.celery_app beat --loglevel=info
```

## Private profile and CV routing

Edit the ignored `user_profile.yaml`, `cv_routing.yaml`, and `cvs/` directory.
Only sanitized `.example` templates belong in Git.

Routing uses title, description, seniority, required skills, and ordered
overrides. It returns a selected CV, confidence, evidence, and fallback reason.
Low confidence, malformed output, unreadable CVs, and non-finite confidence
fail closed for review. The selected CV identifier/hash and profile version are
bound through preparation, form planning, attachment, attempt, and evidence.

## Browser sessions

Use isolated persistent profiles; never point the project at an active
Chrome/Edge profile:

```powershell
python -m scripts.portal_session_bootstrap "https://employer.example/job/..."
```

Complete sign-in and MFA manually. Session readiness does not qualify the form.
The example environment derives LinkedIn and employer profile paths from
`JOB_AGENT_BROWSER_STATE_DIR`, so host sign-in commands and Compose workers use
the same isolated browser state.
Every inspection creates an expiring immutable plan. Any form, selector,
application, CV, profile, session, or build change invalidates its authority.

## Main operator interfaces

The local Windows API leaves `GET /health/live`, `GET /health/ready`, and
`GET /metrics` unauthenticated so local process and container probes can use
them. Bind them to loopback or an internal network; do not publish them through
Vercel or an internet-facing proxy. Detailed application and operational API
routes require bearer authentication outside the explicit development-only,
prepare-only placeholder-auth bypass.

| Method | Path | Meaning |
|---|---|---|
| GET | `/health/live` | Process liveness only |
| GET | `/health/ready` | Dependency and runtime readiness |
| GET | `/metrics` | Bounded Prometheus exposition |
| GET | `/api/runtime/capabilities` | Build, mode, runner, LLM, and send guards |
| GET | `/api/ats/adapters` | Version and qualification inventory |
| POST | `/api/applications/{id}/prepare` | Review/prepare; queues no external action |
| POST | `/api/applications/{id}/inspect` | Build a private form plan |
| GET | `/api/applications/{id}/form-plan` | Read latest plan and blockers |
| POST | `/api/applications/{id}/answers/{field_id}/confirm` | Confirm one exact answer |
| POST | `/api/applications/{id}/submit` | Request exact explicit send; normally disabled now |
| GET | `/api/submission-attempts/{id}` | Poll the authoritative attempt |
| POST | `/api/submission-attempts/{id}/reconcile` | Reconcile an unknown attempt |
| POST | `/api/applications/{id}/retry` | Re-prepare only definitive pre-commit/draft outcomes |

The deprecated `/approve` route is a preparation alias and never claims
submission.

## Vercel control plane

Vercel cannot run Chromium, Ollama, or the authoritative private application
workflow. It may host a protected redacted control plane whose one-use commands
are signed and accepted only by the private runner.

This is a separate HTTP boundary: the isolated Vercel control plane exposes
only `/health/live` without an operator session. Its readiness, grant, command,
dashboard, and runner-management routes remain protected.

Start with the [control-plane bootstrap](docs/control-plane-bootstrap.md).
Preview deployments cannot dispatch. A restored control plane deactivates all
old devices and requires new identities before reconnecting.

## Operations and recovery

- [Production operations](docs/operations.md)
- [Always-on discovery mesh](docs/discovery-mesh.md)
- [Qualification evidence](docs/qualification/README.md)
- [Employer automation boundary](docs/employer-automation.md)
- [Local Ollama form planning](docs/ollama-form-plan-v1.md)
- [Backup and restore](docs/control-plane-backup-restore.md)
- [Recovery runbooks](docs/recovery-runbooks.md)
- [Private-data retention and deletion](docs/private-data-retention.md)

Validate the deterministic qualification aggregate:

```powershell
python scripts/build_adapter_qualification_matrix.py --check
```

Run focused tests and style checks:

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

## License

MIT
