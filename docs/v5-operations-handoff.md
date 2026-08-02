# v5 operations and rollout handoff

This runbook is the release boundary between implemented automation and
qualified authority. It does not claim a real employer dry run, canary, or
submission. The checked-in first-five ATS evidence remains fixture-only, so
qualified autopilot must remain inactive.

## Two dashboards, two trust boundaries

The local dashboard at `http://127.0.0.1:8000/` is the full private
application. Its authenticated operations view includes:

- discovery source health, cadence, next poll, and last successful run;
- discovered, source-occurrence, deduplicated, eligible, prepared,
  quarantined, and employer-confirmed counts;
- the role-to-CV matrix and latest bounded fit/routing evidence;
- signed policy revision, limits, remaining capacity, expiry, and kill switch;
- database-authoritative adapter qualification tiers and qualified form-scope
  counts;
- recent attempt stages/outcomes, exact attachment verification, form
  fingerprints, evidence digests, runtime identity, and failure clusters.

Only `confirmed_submitted` backed by exact employer evidence is green. Queued,
prepared, retry, operator-reconciled, legacy, and unknown states remain neutral
or warning states.

The Vercel dashboard is a redacted control plane, not the full application. A
signed heartbeat may contain only seven counters, bounded policy state,
fixed discovery-source codes, fixed ATS/qualification codes, release/boot
identity, timestamps, and an operations digest. It never contains job or
company text, a URL, identity, CV identifier/hash, answer, cover letter, form
content, browser state, or Ollama input/output.

The protected local snapshot is `GET /api/dashboard/operations`; the public
Prometheus exposition is `GET /metrics`. Keep both on loopback or an internal
network.

## Prometheus contract

All label values pass a finite allowlist. Unknown or corrupted historical
values collapse to `other` or `OTHER`. Do not add company, tenant, source key,
job, URL, title, question, answer, application, CV, user, exception text, or
free-form message labels.

| Operating question | Metric |
|---|---|
| Is the v5 database snapshot available? | `job_agent_v5_operational_snapshot_available` |
| How stale is each enabled feed family? | `job_agent_discovery_source_lag_seconds` |
| Which feed families are healthy/degraded? | `job_agent_discovery_source_instances` |
| Are first-success observations complete? | `job_agent_discovery_source_last_success_available` |
| Which feed runs fail, and why? | `job_agent_discovery_runs_total`, `job_agent_discovery_failures_total` |
| How many postings were inserted, revised, deduplicated, or closed? | `job_agent_discovery_postings_total` |
| What is the latest fit disposition/abstention state? | `job_agent_fit_current_jobs` |
| Did held-out routing/fit qualification pass? | `job_agent_fit_qualification_available`, `job_agent_fit_qualification_qualified`, `job_agent_fit_qualification_ratio` |
| How long does preparation take? | `job_agent_preparation_duration_seconds` |
| What does signed policy allow or deny? | `job_agent_automation_policy_decisions_total`, `job_agent_automation_policy_denials_total` |
| How many immutable attempts have each outcome? | `job_agent_submission_attempts_total` |
| How many latest applications have exact employer proof? | `job_agent_employer_confirmed_applications_total` |
| What are current durable queue depths? | `job_agent_queue_depth` |
| What finite stage/outcome/reason/evidence events occurred? | `job_agent_operational_events_total`, `job_agent_operational_duration_seconds` |

The qualification ratios come only from the schema-valid local qualification
artifact. Missing, malformed, non-finite, or unqualified artifacts expose an
unavailable/unqualified gauge and cannot create submission authority.

## Managed runner and logs

Use the exact main-derived managed runtime:

```powershell
pwsh -NoProfile -File .\scripts\job_agent.ps1 start
pwsh -NoProfile -File .\scripts\job_agent.ps1 status
pwsh -NoProfile -File .\scripts\job_agent.ps1 open
pwsh -NoProfile -File .\scripts\job_agent.ps1 stop
```

The owned scheduled task is single-instance, starts at logon, restarts after
failure, and refuses foreign or drifted task definitions without explicit
adoption/repair. `start` and `open` require the exact release and readiness
snapshot before opening the local dashboard.

The outbound control-plane runner writes structured JSONL to:

`%LOCALAPPDATA%\JobApplyAgent\logs\control-plane-runner.jsonl`

Only `timestamp`, fixed `event`, bounded `status`, stable `reason_code`, and
exception class name may be written. Exception messages and protocol payloads
are never logged. Rotation is 5 MiB with five backups (six files and about
30 MiB maximum). Treat any older unstructured logs as potentially sensitive
and expire them under the private-data retention procedure.

## Deployment order

The new server accepts both legacy and summary-bearing heartbeats. The older
server does not accept the new summary fields. Therefore deploy server first:

1. Keep the private runner stopped; back up the dedicated control-plane
   database.
2. Apply control-plane migration `0006_runner_operations_summary` to the exact
   target database and verify the current Alembic head.
3. After main CI passes, create a staged Production deployment without domain
   assignment:

   ```powershell
   vercel deploy --prod --skip-domain
   ```

4. Verify the exact deployment URL, commit/build identity, liveness, protected
   authentication, readiness, migration head, redacted dashboard, dispatch
   policy, and runtime logs.
5. Promote the exact tested artifact without rebuilding:

   ```powershell
   vercel promote <staged-production-deployment-url> --yes
   ```

6. Verify the canonical production origin, then update/start the exact private
   runner. Require a fresh signed heartbeat within 30 seconds and matching
   release identity.
7. Start/open the full local application only after `/health/ready` is 200.

If a staged or promoted check fails, keep the runner stopped, keep submission
authority revoked, and roll back the alias or database under the documented
rollback boundary. Never repair by pushing directly to `main` or rebuilding a
different production artifact.

## Qualification and staged rollout

Every stage needs a written start/end time, counts, failures, unknown attempts,
and operator sign-off. Time passing alone does not advance the rollout.

1. **Discovery-only, seven complete days.** Keep preparation and submission
   authority off. Verify p95 feed lag, cursor recovery, alert latency,
   deduplication, source errors, and zero private cloud/metric content.
2. **Automatic preparation, seven complete days.** Keep final action disabled.
   Verify deterministic CV identity, held-out precision, abstention, unsupported
   fields, material evidence, attachment plans, and quarantine reasons.
3. **One explicit canary per semantic ATS contract class.** Fixture and exact
   real-URL dry run must pass first. The operator selects and approves the exact
   job. CAPTCHA/MFA pauses manually. An ambiguous action becomes `unknown` and
   does not qualify the scope.
4. **Qualified autopilot, five applications/day for three clean days.** Require
   an active locally signed policy and only live-canary-qualified exact scopes.
5. **Ten/day for three clean days.** Advance only with zero duplicate external
   actions, unsupported/sensitive automatic answers, and unresolved unknowns.
6. **Up to 25/day.** This is a policy ceiling, not a target. Maintain five/hour,
   two active applications/company/14 days, active hours, 85 minimum fit, and
   the local emergency stop.

A selector, protocol, form-contract, consent, upload, attachment, evidence, CV,
profile, qualification, or release change revokes the affected authority and
returns it to qualification. LinkedIn Easy Apply and unsupported/proprietary
portals remain prepare-only.

## Recovery and backup acceptance

Follow [backup and restore](control-plane-backup-restore.md), [recovery
runbooks](recovery-runbooks.md), and [private-data retention](private-data-retention.md).
A restored database is quarantined before any API, worker, Beat, browser, or
runner reconnects. Both quarantine tools must run first with `--dry-run`, then
with explicit `--apply`, and a second dry run must report zero changes.

An operations handoff is acceptable only when:

- main and control-plane migrations upgrade and downgrade cleanly;
- local and control-plane tests, Ruff, typing, security/dependency checks,
  Docker/Compose readiness, PR CI, and post-merge main CI are green;
- backup/restore quarantine and identity rotation procedures are current;
- the local dashboard is distinguished from the Vercel control plane;
- current readiness and qualification are reported honestly; and
- no real application, dry run, canary, or production promotion is claimed
  without its exact evidence.
