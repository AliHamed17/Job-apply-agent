"""Persist a rebuilt UserProfile to YAML with versioning."""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from profile.loader import set_profile
from profile.models import UserProfile

import structlog
import yaml

logger = structlog.get_logger(__name__)


def save_profile(profile: UserProfile, yaml_path: Path, db=None) -> int:
    """Write profile YAML (with backup), record a version row, swap the cache.

    db=None skips the DB version row (used in tests); returns 1 in that case
    or the next version number when a DB session is provided.
    """
    yaml_path = Path(yaml_path)
    if yaml_path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        shutil.copy2(yaml_path, yaml_path.with_suffix(yaml_path.suffix + f".bak-{stamp}"))

    payload = profile.model_dump()
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{yaml_path.name}.", suffix=".tmp", dir=yaml_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, yaml_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    version = 1
    if db is not None:
        from db.models import UserProfileVersion  # noqa: PLC0415
        last = (
            db.query(UserProfileVersion)
            .order_by(UserProfileVersion.version.desc())
            .first()
        )
        version = (last.version + 1) if last else 1
        db.add(UserProfileVersion(
            profile_yaml=yaml_path.read_text(encoding="utf-8"),
            version=version,
        ))
        db.commit()

    set_profile(profile)
    logger.info("profile_saved", path=str(yaml_path), version=version)
    return version
