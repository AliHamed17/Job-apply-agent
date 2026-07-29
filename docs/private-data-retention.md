# Private-data retention and deletion

This is a private, single-user system. The operator is the data owner. The
application does not currently run an automatic deletion job, so the schedule
below is an operational policy that must be reviewed and executed deliberately.

## Data classes

| Data | Location | Sensitivity | Default review |
|---|---|---|---|
| Candidate profile and confirmed facts | Managed `JOB_AGENT_PROFILE_DATA_DIR` (`$env:LOCALAPPDATA\JobApplyAgent\profile-data`) | High | On every factual change; delete on account closure |
| CV files and versions | Managed `JOB_AGENT_PROFILE_DATA_DIR` (`profile-data\cvs` and version storage) | High | Remove superseded versions after 90 days unless needed for audit |
| Browser sessions | Managed `JOB_AGENT_BROWSER_STATE_DIR` (`$env:LOCALAPPDATA\JobApplyAgent\browser-state`) | Credential-equivalent | Delete on logout, compromise, employer disconnect, or 90 days inactive |
| Form plans and generated materials | Private PostgreSQL | High | Delete abandoned drafts after 90 days |
| Submission attempts and evidence digests | Private PostgreSQL | Medium/high | Retain while needed for duplicate prevention and reconciliation |
| Unknown attempts | Private PostgreSQL | High operational importance | Retain until explicitly reconciled |
| Application audit events | Private PostgreSQL | Medium | Review annually; preserve bounded evidence needed for truth/audit |
| Operational metric details | Private PostgreSQL | Redacted bounded labels only | Keep at most 90 days and 100,000 rows |
| Operational metric receipts | Private PostgreSQL | SHA-256 event key and timestamp only | Retain for replay prevention; contains no labels or private content |
| Operational metric rollups | Private PostgreSQL | Redacted aggregate | Retain for historical counters; review annually |
| Logs | Private host | Potentially sensitive | 30 days maximum unless an incident hold exists |
| Prometheus metrics | Private host | Redacted aggregate | 15 days |
| Control-plane sessions/commands/events | Dedicated cloud PostgreSQL | Redacted metadata | Sessions expire quickly; review command/event retention annually |
| Encrypted backups | Approved backup storage | Same sensitivity as source | Rotate under the backup schedule and expire after verified replacement |

The managed runtime records the resolved roots in `runtime.env`. A reviewed
custom deployment may override them. Repo-relative `.portal_profiles/` and
`.linkedin_profile/` are legacy direct-run defaults only; do not use them as the
managed runtime's backup or deletion target.

Legal, tax, employment, or contractual duties may require a different period.
Document any exception and its expiry. Do not retain private data merely
because storage is available.

## Collection minimization

- Keep CV text, answers, cover letters, employer URLs, and browser state on the
  private PC.
- Send only opaque references, bounded adapter/outcome codes, timestamps, and
  evidence digests to the control plane.
- Never store passwords from Chrome or Edge.
- Never store CAPTCHA content, cookies, raw page HTML, form answers, or CV text
  in traces or metrics.
- Metric detail pruning deletes only the bounded labeled observation. Its
  content-free SHA-256 receipt remains so delayed task replay cannot count the
  same domain event twice.
- Sensitive factual answers require exact user-confirmed evidence.
- Ollama inputs remain local and have no cloud fallback in production.

## Deleting one application

1. Stop the worker and browser runner.
2. Confirm whether any attempt is `unknown`. Reconcile it before deletion so a
   later rediscovery cannot cause an accidental duplicate.
3. Export the minimal redacted duplicate-prevention record if it must be
   retained.
4. Delete the application, its generated materials, form plans, unused grants,
   attempts, and private audit rows through a reviewed maintenance procedure.
5. Remove associated temporary CV copies and browser downloads.
6. Remove matching redacted control-plane metadata only after the private
   command history is settled.
7. Record the deletion date and backup-expiry date without recording the
   deleted content.

Do not use broad filesystem or database deletion commands. Resolve exact IDs
and paths first, take a current encrypted backup when policy requires it, and
verify that confirmed or unknown history outside the requested scope remains.

## Deleting a CV or profile version

Before deletion, identify every application and attempt that references its
identifier or hash. Historical attempt bindings may be needed to prove which
document was attached. If that evidence must remain, keep only the minimum
encrypted artifact needed for the retention period and block it from reuse.

After deletion:

- invalidate affected active form plans;
- revoke unused review grants and permits;
- move affected prepared applications to review;
- rebuild routing evaluation without the deleted CV;
- verify the file is absent from active storage and future backups.

## Disconnecting an employer account

Resolve `JOB_AGENT_BROWSER_STATE_DIR` from the active runtime, then delete the
exact isolated directory below its `portals/` child for that employer. Do not
delete the shared browser-state or `portals/` parent directory. Revoke the
employer session from its candidate portal when available, invalidate related
form plans, and leave any ambiguous attempt in `unknown` until reconciled.

## Backup expiry

Source deletion is incomplete until every encrypted backup containing that
data reaches its documented expiry. Record:

- backup identifier and creation date;
- data categories contained;
- scheduled expiry;
- actual deletion confirmation;
- any legal hold.

After expiry, verify restore tests use a newer sanitized backup and cannot
reintroduce deleted authority. Every restored backup must still pass the
quarantine procedure in
[Backup and restore](control-plane-backup-restore.md).
