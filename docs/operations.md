# Production operations

This deployment is private and single-user. PostgreSQL, Redis, Prometheus, and
Grafana remain on the private Compose network or loopback by default. Only nginx
is intended to accept external traffic.

## Production safety

Set `APP_ENV=production`. Startup refuses to continue unless:

- `SECRET_KEY` is non-default and at least 32 characters;
- `WHATSAPP_APP_SECRET` is configured for signed webhooks;
- CORS contains explicit origins rather than `*`; and
- any non-dry-run mode has `LIVE_AUTOMATION_ACKNOWLEDGED=true`.

Keep `DRAFT_ONLY=true`, `AUTO_APPLY=false`, and `DRY_RUN=true` unless the
operator has deliberately reviewed and acknowledged the live-mode risk.

## Health and monitoring

- `/health/live` only proves that the API process responds.
- `/health/ready` checks PostgreSQL, the current Alembic revision, Redis,
  shared profile storage, worker and Beat heartbeats, and Chromium.
- `/health` remains a liveness compatibility alias.
- `/metrics` is Prometheus text and is blocked by the public nginx route.

Dashboard operational status is degraded if any readiness dependency is
missing or stale. Heartbeats are considered stale after
`DEPENDENCY_HEARTBEAT_TTL_SECONDS`.

## Backup

Stop application workers before a consistent backup.

1. PostgreSQL: run `pg_dump --format=custom --file=job-agent.dump job_agent`
   from an authenticated PostgreSQL environment.
2. Copy `user_profile.yaml`, `cv_routing.yaml`, `cvs/`, and `profile-data/`
   version metadata into an encrypted backup.
3. Copy `.linkedin_profile/` only into encrypted storage. It contains an active
   browser session and must be treated as a secret.
4. Copy `.portal_profiles/` only into encrypted storage. Each tenant directory
   contains an active employer session.
5. Record the Git revision and Alembic revision alongside the backup.

Never commit these artifacts. Encrypt backups at rest, restrict them to the
operator, and test restoration quarterly.

## Restore

1. Deploy the recorded Git revision and create an empty PostgreSQL database.
2. Restore with `pg_restore --clean --if-exists --dbname=job_agent job-agent.dump`.
3. Restore `user_profile.yaml`, `cv_routing.yaml`, `cvs/`, `profile-data/`,
   `.linkedin_profile/`, and `.portal_profiles/` only when needed, with their
   original access restrictions.
4. Run `alembic upgrade head`.
5. Start Redis, API, worker, and Beat; require `/health/ready` to become ready
   before enabling scheduled work.

## Retention and deletion

- Keep operational metrics for 15 days; metrics must not contain personal data.
- Keep application and submission audit records while needed for duplicate
  prevention and user review.
- Review rejected or abandoned applications every 90 days.
- When deleting personal data, stop workers, delete the selected database
  records and associated profile/CV versions, remove browser state if the
  account is disconnected, and expire every corresponding encrypted backup
  under the backup provider's retention policy.
- Preserve only non-personal aggregate counts after deletion.
