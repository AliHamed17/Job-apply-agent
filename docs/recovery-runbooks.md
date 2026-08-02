# Recovery runbooks

These runbooks preserve the core rule: uncertainty after a possible final
action becomes `unknown`, never an automatic retry.

## API or worker readiness is degraded

1. Leave **Send application** disabled.
2. Check unauthenticated `/health/live`, then `/health/ready`, from the private
   host or internal network.
3. Identify the degraded dependency: PostgreSQL, Redis, migrations, shared
   profile storage, worker heartbeat, Beat heartbeat, Chromium, browser
   session, or local Ollama.
4. Restore only that dependency and wait for a fresh heartbeat.
5. Reinspect the application. Readiness recovery does not revive an expired
   form plan or permit.
6. Inspect the external structured runner log for fixed event/status/reason
   transitions. It intentionally omits exception text and private payloads, so
   use database attempt/evidence state—not log absence—as submission truth.

## Worker crash during submission

1. Do not requeue the task manually.
2. Inspect the attempt stage and `final_action_at`.
3. If the attempt was at `committing`/`verifying`, a permit was consumed, or
   any final request may have left, mark it `unknown` with
   `STALE_INDETERMINATE`.
4. Check the employer candidate portal for an application-specific record.
   Email is optional corroboration, not primary proof.
5. Reconcile as submitted or definitively not submitted. Operator
   reconciliation remains non-green and is not employer evidence.
6. Only a definitively failed/not-submitted application may receive a new
   numbered attempt after a new review.

## Selector or form drift

1. Stop the exact adapter/version/scope; do not fall back to another transport.
2. Capture only a sanitized fixture and redacted trace.
3. Reset the affected qualification scope to dry-run qualification.
4. Update selector/protocol version when the contract changed.
5. Pass fixtures and one explicit real-URL dry run with final action disabled.
6. A live canary remains a separate exact-job approval.

## CAPTCHA, MFA, or expired session

1. Never solve, bypass, rotate proxies, or add stealth behavior.
2. Pause the adapter and surface `CHALLENGE_DETECTED`, `MFA_REQUIRED`, or
   `SESSION_EXPIRED`.
3. Complete sign-in/MFA manually in the isolated browser profile.
4. Reinspect the form; never reuse the old plan or permit.

## CV attachment cannot be verified

1. Stop before the irreversible action with `ATTACHMENT_UNVERIFIED`.
2. Confirm the routed CV identifier and SHA-256 match the selected local file.
3. Confirm the ATS upload receipt binds the same bytes and control.
4. If a reused prior application exposes an unidentified resume, replace it or
   stop for review. A filename alone is not proof.
5. Build a new form plan after a verified upload.

## Ollama outage or malformed output

1. Keep preparation blocked; never fall back to a cloud model.
2. Verify the loopback endpoint, exact `qwen2.5:7b` identity/digest, and one-at-
   a-time lease.
3. Retry only the reversible generation step after the circuit resets.
4. Never call an LLM during the final external-action stage.
5. Sensitive or unsupported facts remain operator review regardless of model
   recovery.

## Vercel control plane or signing identity compromise

1. Stop the private scheduled runner.
2. Deactivate every affected runner device and revoke operator sessions.
3. Reject undelivered queued/claimed commands without inventing runner events.
4. Rotate the runner device UUID, runner Ed25519 key, control signing identity,
   operator token, session secret, and CSRF secret.
5. Revoke unused local review grants and invalidate active form plans.
6. Verify old signatures receive `RUNNER_DISABLED` or `RUNNER_UNKNOWN`.
7. Create a new schema-v2 identity bound to the exact environment, project ID,
   and scope ID. Run `control_plane_identity.py configure-vercel --dry-run`
   with the exact linked `control_plane` cwd plus either the approved
   package-internal native executable pin or the separate absolute
   `node.exe`/`vercel\dist\vc.js` pins and exact version, then repeat without
   `--dry-run`. Run `validate-selection` before installing or starting the
   runner. The seven identity-derived values travel only over the pinned
   process's stdin and the bundle digest is written last; configure the
   database, origin, and application environment separately.
8. Use `copy-operator-token` only when the operator must log in; never print,
   redirect, or paste the token into a log, issue, PR, or chat.
9. Resume only from a protected, tested artifact and a fresh local review.

## Database or host restore

Follow [Backup and restore](control-plane-backup-restore.md). Quarantine both
databases before any worker or public deployment reconnects. A second
quarantine pass must report zero changes.

## Suspected private-data exposure

1. Stop workers, browser sessions, and outbound control-plane delivery.
2. Preserve a minimal encrypted incident copy; do not paste content into chat
   or an issue.
3. Rotate browser sessions and all relevant signing/session credentials.
4. Identify affected CV/profile versions, logs, backups, and cloud metadata.
5. Delete or quarantine under
   [Private-data retention](private-data-retention.md).
6. Confirm metrics, traces, and the control-plane database contain no names,
   emails, phone numbers, URLs, answers, CV text, cookies, or page content.

## Safe restart checklist

- `DRY_RUN=true`
- `DRAFT_ONLY=true`
- `PORTAL_FINAL_SUBMIT_ENABLED=false`
- migrations at current head
- PostgreSQL and Redis responsive
- shared storage readable/writable
- Chromium available
- worker and Beat heartbeats fresh
- Ollama local and qualified
- no unfinished restored attempt
- no queued/claimed restored command
- all restored devices inactive
- new identity configured
- operator has re-reviewed any application to be prepared

Passing this checklist enables preparation only. It does not change adapter
qualification or authorize an employer submission.

For deployment order, bounded metrics, runner-log location, and the staged
discovery/preparation/canary/autopilot ramp, use the
[v5 operations handoff](v5-operations-handoff.md).
