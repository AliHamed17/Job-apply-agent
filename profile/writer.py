"""Persist a rebuilt UserProfile to YAML with versioning."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from profile.loader import set_profile
from profile.models import UserProfile

import structlog
import yaml
from sqlalchemy import func
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

logger = structlog.get_logger(__name__)

_PROFILE_WRITE_LOCK = threading.RLock()
_PROFILE_VERSION_LOCK_ID = 0x4A4F4250524F4649
_VERSION_WRITE_RETRIES = 3


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _acquire_database_write_lock(db) -> None:
    """Serialize the profile-version sequence across PostgreSQL processes."""

    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.execute(
            sql_text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _PROFILE_VERSION_LOCK_ID},
        )


def _persist_version(db, profile_yaml: str, yaml_path: Path) -> int:
    """Atomically order the file swap and immutable DB version identity."""

    from db.models import UserProfileVersion  # noqa: PLC0415

    for attempt in range(_VERSION_WRITE_RETRIES):
        try:
            _acquire_database_write_lock(db)
            # Keep the authoritative file swap inside the same serialized
            # section as sequence allocation. A uniqueness rollback releases
            # PostgreSQL's transaction lock, so every retry reacquires the
            # lock and rewrites its own content before it can become latest.
            _atomic_write(yaml_path, profile_yaml)
            latest = db.query(func.max(UserProfileVersion.version)).scalar()
            version = int(latest or 0) + 1
            db.add(
                UserProfileVersion(
                    profile_yaml=profile_yaml,
                    version=version,
                )
            )
            db.commit()
            return version
        except IntegrityError:
            db.rollback()
            if attempt + 1 >= _VERSION_WRITE_RETRIES:
                raise
    raise RuntimeError("profile version retry loop exhausted")


def _restore_authoritative_file(
    db,
    yaml_path: Path,
    previous_text: str | None,
) -> None:
    """Restore the latest committed snapshot after a failed version write."""

    from db.models import UserProfileVersion  # noqa: PLC0415

    db.rollback()
    try:
        _acquire_database_write_lock(db)
        latest = db.query(UserProfileVersion).order_by(UserProfileVersion.version.desc()).first()
        if latest is not None:
            _atomic_write(yaml_path, latest.profile_yaml)
        elif previous_text is not None:
            _atomic_write(yaml_path, previous_text)
        else:
            yaml_path.unlink(missing_ok=True)
    finally:
        # Release a PostgreSQL transaction-scoped advisory lock without
        # committing any caller-owned state.
        db.rollback()


def save_profile(profile: UserProfile, yaml_path: Path, db=None) -> int:
    """Write profile YAML (with backup), record a version row, swap the cache.

    db=None skips the DB version row (used in tests); returns 1 in that case
    or the next version number when a DB session is provided.
    """
    payload = profile.model_dump()
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    yaml_path = Path(yaml_path)

    with _PROFILE_WRITE_LOCK:
        previous_text = yaml_path.read_text(encoding="utf-8") if yaml_path.exists() else None
        if yaml_path.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            shutil.copy2(
                yaml_path,
                yaml_path.with_suffix(yaml_path.suffix + f".bak-{stamp}"),
            )
        version = 1
        if db is None:
            _atomic_write(yaml_path, text)
        else:
            try:
                version = _persist_version(db, text, yaml_path)
            except Exception:
                _restore_authoritative_file(db, yaml_path, previous_text)
                raise

        set_profile(profile)
    logger.info("profile_saved", path=str(yaml_path), version=version)
    return version
