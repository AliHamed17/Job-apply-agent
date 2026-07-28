# Control-plane migrations

This schema is intentionally separate from the private runner database. It stores
only opaque application references, bounded protocol codes, timestamps, and
cryptographic digests.

Set `CONTROL_DATABASE_URL` to the dedicated PostgreSQL URL with
`sslmode=require` (or `verify-ca` / `verify-full`).
Common `postgres://` and `postgresql://` URLs are explicitly normalized to the
bundled psycopg v3 driver. A missing URL aborts; this migration configuration
has no local placeholder database.

Then run from this directory:

```powershell
python -m alembic upgrade head
python -m alembic current
```

Rollback of the initial release removes only the dedicated control-plane tables:

```powershell
python -m alembic downgrade base
```

Back up the dedicated database before every migration. Never point this Alembic
configuration at the private Job Apply Agent database.
