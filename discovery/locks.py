"""Cross-process discovery locking and stale-run recovery."""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from db.models import DiscoveryRun

_LOCAL_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.Lock] = {}


def _advisory_key(value: str) -> int:
    raw = int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")
    return raw - (1 << 64) if raw >= (1 << 63) else raw


@contextmanager
def try_discovery_lock(db, lock_name: str):
    """Yield whether this session owns a PostgreSQL advisory/process lock."""

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        key = _advisory_key(lock_name)
        acquired = bool(
            db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
        )
        try:
            yield acquired
        finally:
            if acquired:
                db.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                db.commit()
        return

    with _LOCAL_GUARD:
        lock = _LOCAL_LOCKS.setdefault(lock_name, threading.Lock())
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def reconcile_stale_discovery_runs(db, *, stale_after_seconds: int) -> int:
    """Fail closed any run abandoned before its terminal commit."""

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=stale_after_seconds)
    rows = (
        db.query(DiscoveryRun)
        .filter(
            DiscoveryRun.status == "running",
            DiscoveryRun.started_at < cutoff,
        )
        .all()
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    for row in rows:
        row.status = "failed"
        row.reason_code = "STALE_RUN_RECOVERED"
        row.finished_at = now
    db.commit()
    return len(rows)
