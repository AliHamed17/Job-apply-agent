"""Load an immutable profile snapshot with an exact database version."""

from __future__ import annotations

from dataclasses import dataclass
from profile.models import UserProfile

import yaml

from db.models import UserProfileVersion


class ProfileSnapshotError(ValueError):
    """The authoritative version row cannot be parsed safely."""


@dataclass(frozen=True, slots=True)
class VersionedProfileSnapshot:
    profile: UserProfile
    version: int | None


def _parse_snapshot(row: UserProfileVersion) -> VersionedProfileSnapshot:
    """Parse one exact, uniquely versioned row without consulting process cache."""

    if not isinstance(row.version, int) or row.version < 1:
        raise ProfileSnapshotError("PROFILE_VERSION_INVALID")
    try:
        payload = yaml.safe_load(row.profile_yaml)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError("profile snapshot must be a mapping")
        profile = UserProfile.model_validate(payload)
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise ProfileSnapshotError("PROFILE_SNAPSHOT_INVALID") from exc
    return VersionedProfileSnapshot(profile=profile, version=row.version)


def latest_profile_version(db) -> int | None:
    """Return the deterministic latest immutable profile identity."""

    row = db.query(UserProfileVersion).order_by(UserProfileVersion.version.desc()).first()
    return row.version if row is not None else None


def load_versioned_profile_snapshot(
    db,
    *,
    version: int | None = None,
) -> VersionedProfileSnapshot:
    """Return profile content from the exact latest persisted version.

    A missing version keeps legacy/dev generation available but returns
    ``version=None``; such an application cannot receive a final-submit permit.
    Once versioned content exists, the process-global YAML cache is never used,
    so API and worker processes cannot bind stale content to a newer version.
    """

    query = db.query(UserProfileVersion)
    if version is None:
        row = query.order_by(UserProfileVersion.version.desc()).first()
    else:
        if not isinstance(version, int) or version < 1:
            raise ProfileSnapshotError("PROFILE_VERSION_INVALID")
        row = query.filter(UserProfileVersion.version == version).one_or_none()
    if row is None:
        if version is not None:
            raise ProfileSnapshotError("PROFILE_VERSION_NOT_FOUND")
        from profile.loader import get_profile

        return VersionedProfileSnapshot(profile=get_profile(), version=None)
    return _parse_snapshot(row)
