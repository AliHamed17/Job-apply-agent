# Backup and restore

The private application runtime and the redacted Vercel control plane use
separate PostgreSQL databases and separate trust boundaries. Back up and
restore them independently.

## Backup contents

### Private runtime

While the API, workers, Beat, and browser runner are stopped:

For the managed Windows runtime, the authoritative private-data roots are:

- `$env:LOCALAPPDATA\JobApplyAgent\profile-data` for `user_profile.yaml`,
  `cv_routing.yaml`, `cvs/`, and profile/CV version storage;
- `$env:LOCALAPPDATA\JobApplyAgent\browser-state` for the `linkedin/` and
  `portals/` browser profiles.

These are the paths written to `JOB_AGENT_PROFILE_DATA_DIR` and
`JOB_AGENT_BROWSER_STATE_DIR` in the managed `runtime.env`. If a reviewed custom
deployment overrides those settings, back up their resolved absolute paths
instead. Do not assume the legacy repo-relative `.portal_profiles/` and
`.linkedin_profile/` defaults are the managed runtime's active data.

1. Create a custom-format PostgreSQL dump:

   ```powershell
   pg_dump --format=custom --file=job-agent.dump $env:DATABASE_URL
   ```

2. Archive the entire resolved `JOB_AGENT_PROFILE_DATA_DIR`, including
   `user_profile.yaml`, `cv_routing.yaml`, `cvs/`, and shared profile/CV version
   storage.
3. If continued authenticated sessions are required, separately archive the
   entire resolved `JOB_AGENT_BROWSER_STATE_DIR`. Treat it as live credentials.
4. Record the Git commit, main Alembic revision, backup time, database server
   version, and SHA-256 digest of every archive.
5. Do not back up Redis as authoritative state. It is a wake-up/cache layer and
   must be recreated empty.

### Redacted control plane

Create a provider snapshot or `pg_dump` of the dedicated control-plane
PostgreSQL database. Record the exact control-plane Alembic revision and tested
deployment identity. Vercel environment secrets, signing keys, and private
runner keys are not database content and must not be placed in the dump.

Private identity bundles are not restore authority. A restore always creates
new identities; old keys and device UUIDs remain revoked.

## Storage rules

- Encrypt every archive at rest and in transit.
- Restrict access to the single operator and a documented recovery account.
- Keep the decryption key outside the backup location.
- Never store backups in Git, Vercel build artifacts, CI artifacts, issue
  attachments, or ordinary cloud-sync folders.
- Test restore into an isolated network at least quarterly.

## Private-runtime restore

Never restore over a running instance.

1. Stop the API, Celery worker, Beat, browser runner, and scheduled tasks.
2. Restore into a new isolated PostgreSQL database:

   ```powershell
   pg_restore --clean --if-exists --dbname $env:DATABASE_URL job-agent.dump
   ```

3. Check the recorded Git revision, then run `python -m alembic upgrade head`.
4. With networking and workers still stopped, preview the quarantine:

   ```powershell
   python scripts/quarantine_restored_runtime.py --dry-run
   ```

5. Apply it exactly once:

   ```powershell
   python scripts/quarantine_restored_runtime.py --apply
   ```

The quarantine:

- revokes every active signed autopilot policy before any worker is restarted;
- terminalizes queued and running autopilot inspections so they cannot be reclaimed;
- invalidates every restored form plan;
- expires every unused final-submit permit;
- revokes unconsumed local control-plane review grants;
- cancels pending and claimed submission commands;
- finishes definite pre-commit attempts as `failed_before_commit`;
- classifies any possible post-commit action as `unknown`;
- never requeues an attempt;
- preserves exact employer-verified attempts, `submitted_at`, and evidence;
- moves only affected non-confirmed applications to `NEEDS_REVIEW`.

Run the command again and require all mutation counts to be zero. That proves
idempotency.

6. Restore profile/CV and browser archives only after malware scanning and
   access-control verification. Restore them to the resolved
   `JOB_AGENT_PROFILE_DATA_DIR` and `JOB_AGENT_BROWSER_STATE_DIR` recorded for
   the new managed runtime, not to legacy repo-relative directories.
7. Start PostgreSQL and Redis, then API readiness in `DRY_RUN=true`,
   `DRAFT_ONLY=true`, and `PORTAL_FINAL_SUBMIT_ENABLED=false`.
8. Do not start workers or reuse a prepared application until the operator
   re-reviews the current private data and creates a new form plan.

An `unknown` attempt requires reconciliation against the employer portal. It
must never retry automatically.

## Control-plane restore

Restore into a new database that is not yet connected to a public deployment.

1. Apply the dedicated control-plane migrations from `control_plane/`.
2. Point the local maintenance environment at the restored database and retain
   the old configuration only long enough to run quarantine:

   ```powershell
   python scripts/quarantine_restored_control_plane.py --dry-run
   python scripts/quarantine_restored_control_plane.py --apply
   ```

The cloud quarantine deactivates every restored runner device, revokes every
operator session, and rejects only undelivered `queued`/`claimed` commands.
Acknowledged, running, rejected, and finished history remains unchanged. It
does not add runner events, evidence, or revocation envelopes.

3. Run it again and require zero changed rows.
4. Generate a completely new schema-v2 identity bundle with
   `scripts/control_plane_identity.py create`, bound to the exact Vercel
   environment, project ID, and scope ID.
5. Replace Vercel and private-runner configuration with the new public/private
   halves through the separately pinned Node/JS or package-internal native CLI
   workflow. Run `validate-selection`, then require the digest-last startup
   attestation to pass. Do not reactivate or reuse any restored device.
6. Verify that all old devices remain inactive, all old sessions are revoked,
   and no queued/claimed command remains.
7. Connect a protected Preview to an isolated, quarantined Preview database and
   run the protected tests. Do not promote that Preview as the immutable
   artifact because Vercel rebuilds Preview deployments with Production
   environment variables during promotion.
8. Back up and quarantine the restored Production database, require the second
   quarantine pass to change zero rows, and migrate it to the expected head.
   Create a staged Production deployment with
   `vercel deploy --prod --skip-domain`, verify that exact deployment, then
   assign the production domain with `vercel promote <deployment-url> --yes`.
   A staged Production promotion does not rebuild the verified artifact.

The restored database alone never proves that a command reached a runner or
that an employer accepted an application. Do not fabricate missing events,
evidence digests, or terminal outcomes.

## Rollback boundary

Keep the pre-restore database read-only and encrypted until acceptance is
complete. Never switch the application back to it without running the same
quarantine and identity-rotation process; otherwise old one-use authority may
become usable again.
