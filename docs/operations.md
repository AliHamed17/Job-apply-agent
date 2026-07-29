# Production operations

This is a private, single-user deployment. PostgreSQL, Redis, Prometheus,
Grafana, private profile storage, Chromium, CVs, and Ollama stay on the private
host or private network. Vercel may host only the authenticated redacted
control plane.

## Current qualification truth

The first-five ATS adapters are fixture-qualified only. The aggregate contains
87 sanitized fixtures and no real-URL dry run, live canary, qualified form
scope, or final executor. Production infrastructure does not elevate that
qualification.

Discovery and preparation may run automatically. Final **Send application**
remains an explicit operator action and is unavailable while the adapter/form
scope is not live-canary-qualified.

## Production safety

Set `APP_ENV=production`. Startup refuses unsafe secrets, unsigned webhook
configuration, wildcard CORS, unacknowledged non-dry-run settings, non-local
Ollama, an unqualified local model identity, or a mismatched runtime release.

Keep these defaults until a separate qualification record authorizes an exact
scope:

```dotenv
DRY_RUN=true
DRAFT_ONLY=true
PORTAL_FINAL_SUBMIT_ENABLED=false
AUTO_APPLY=false
LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5:7b
OLLAMA_NO_CLOUD=true
```

`AUTO_APPLY` means preparation eligibility only. It never approves or sends an
employment application.

## Private Windows API health and monitoring

- `/health/live` is unauthenticated and proves only that the API process
  responds.
- `/health/ready` is unauthenticated and checks PostgreSQL, current Alembic
  revision, Redis, shared profile storage, worker and Beat heartbeats,
  Chromium, and required runtime identity.
- `/health` remains an unauthenticated liveness compatibility alias.
- `/metrics` is unauthenticated bounded Prometheus text.

Operational counter updates are written with the same database transaction as
the domain outcome. PostgreSQL retains bounded labeled detail for at most 90
days and 100,000 rows, permanent content-free replay receipts containing only
a SHA-256 event key and timestamp, and aggregate rollups. Prometheus labels are
normalized to fixed vocabularies before storage and collection.

These probe endpoints must stay on loopback or an internal network and must not
be exposed through an internet-facing proxy. Detailed application and
operational routes require bearer authentication outside the explicit
development-only, prepare-only placeholder-auth bypass.

The isolated Vercel control plane is a separate boundary: only its
`/health/live` route is unauthenticated. Its readiness, dashboard, grant,
command, and runner-management routes require the protected control-plane
session or a valid signed runner envelope.

A degraded dependency, stale heartbeat, release mismatch, expired session,
expired form plan, or disabled adapter keeps **Send application** unavailable.
Queue acceptance and preparation are neutral states. Only exact employer
evidence can be green.

## Private inference

Production uses local `qwen2.5:7b` through a loopback Ollama endpoint, with
bounded timeouts, one inference lease, schema validation, and no cloud fallback.
An Ollama outage blocks reversible preparation and never triggers submission.
No LLM runs during the final external-action stage.

## Backup, restore, and recovery

- [Control-plane bootstrap](control-plane-bootstrap.md)
- [Backup and restore](control-plane-backup-restore.md)
- [Recovery runbooks](recovery-runbooks.md)
- [Private-data retention and deletion](private-data-retention.md)

A restore is not ready when PostgreSQL starts. Before any worker reconnects,
run private and cloud quarantine, require an idempotent zero-change second
pass, rotate every device/signing/session identity, and re-review private
applications.

## Qualification operations

The evidence source and deterministic first-five matrix live under
[`docs/qualification`](qualification/README.md). Validate the aggregate without
changing files:

```powershell
python scripts/build_adapter_qualification_matrix.py --check
```

Any selector, protocol, form, attachment, request, or evidence drift resets the
affected scope to dry-run qualification. CAPTCHA, MFA, session expiry, unknown
required facts, and unverified attachments stop for manual review.
