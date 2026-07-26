"""Bounded runtime identity and live-submission capability decisions.

This module deliberately contains no database or network access.  It turns
process/build facts, configuration, and an already-computed readiness report
into the small public contract consumed by the dashboard.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.config import Settings

PROTOCOL_VERSION = "submission-control.v1"
SUBMIT_COMMAND_PROTOCOL_AVAILABLE = True

_BUILD_ENV_KEYS = (
    "APP_BUILD_SHA",
    "VERCEL_GIT_COMMIT_SHA",
    "GIT_COMMIT_SHA",
    "COMMIT_SHA",
)
_SAFE_RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SOURCE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_READINESS_COMPONENTS = (
    "database",
    "migration",
    "redis",
    "worker",
    "beat",
    "shared_storage",
    "browser",
)
_RUNTIME_SOURCE_ROOTS = (
    "api",
    "bridge",
    "core",
    "db",
    "discovery",
    "ingestion",
    "jobs",
    "llm",
    "match",
    "monitoring",
    "notifications",
    "profile",
    "submitters",
    "worker",
)
_RUNTIME_SOURCE_SUFFIXES = frozenset({".py"})
_RUNTIME_CONFIG_FILES = (
    "alembic.ini",
    "docker-compose.yml",
    "Dockerfile",
    "pyproject.toml",
    "requirements.txt",
    "vercel.json",
)
_BOOT_ID = str(uuid.uuid4())
_STARTED_AT = datetime.now(UTC)


class SubmissionBlockReason(StrEnum):
    """Stable, bounded reasons why the runtime cannot accept a final send."""

    DRY_RUN_ENABLED = "DRY_RUN_ENABLED"
    DRAFT_ONLY_ENABLED = "DRAFT_ONLY_ENABLED"
    FINAL_SUBMIT_DISABLED = "FINAL_SUBMIT_DISABLED"
    LIVE_AUTOMATION_NOT_ACKNOWLEDGED = "LIVE_AUTOMATION_NOT_ACKNOWLEDGED"
    UNATTENDED_AUTOMATION_ENABLED = "UNATTENDED_AUTOMATION_ENABLED"
    RUNTIME_NOT_READY = "RUNTIME_NOT_READY"
    BUILD_IDENTITY_UNAVAILABLE = "BUILD_IDENTITY_UNAVAILABLE"
    WORKER_IDENTITY_UNAVAILABLE = "WORKER_IDENTITY_UNAVAILABLE"
    BUILD_MISMATCH = "BUILD_MISMATCH"
    PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
    SUBMIT_COMMAND_UNAVAILABLE = "SUBMIT_COMMAND_UNAVAILABLE"
    DATABASE_SERIALIZATION_REQUIRED = "DATABASE_SERIALIZATION_REQUIRED"
    OPERATOR_AUTH_REQUIRED = "OPERATOR_AUTH_REQUIRED"


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Immutable identity shared by every response from one API process."""

    build_sha: str
    build_source: str
    ui_asset_digest: str
    source_digest: str
    protocol_version: str
    boot_id: str
    started_at: datetime

    @property
    def release_id(self) -> str:
        """Bounded execution identity persisted on every admitted attempt."""

        digest = hashlib.sha256()
        for value in (self.build_sha, self.source_digest, self.protocol_version):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()


def _clean_release(value: object) -> str | None:
    candidate = str(value or "").strip()
    if not _SAFE_RELEASE_RE.fullmatch(candidate):
        return None
    return candidate


def _resolve_git_dir(repo_root: Path) -> Path | None:
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        return dot_git.resolve()
    try:
        marker = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not marker.lower().startswith("gitdir:"):
        return None
    raw_path = marker.split(":", 1)[1].strip()
    if not raw_path:
        return None
    git_dir = Path(raw_path)
    if not git_dir.is_absolute():
        git_dir = repo_root / git_dir
    resolved = git_dir.resolve()
    return resolved if resolved.is_dir() else None


def _read_ref(git_dir: Path, ref: str) -> str | None:
    if not ref.startswith("refs/") or ".." in Path(ref).parts:
        return None

    bases = [git_dir]
    try:
        common_raw = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        common_raw = ""
    if common_raw:
        common_dir = Path(common_raw)
        if not common_dir.is_absolute():
            common_dir = git_dir / common_dir
        bases.append(common_dir.resolve())

    for base in bases:
        base_resolved = base.resolve()
        candidate = (base_resolved / ref).resolve()
        if not candidate.is_relative_to(base_resolved):
            continue
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _GIT_SHA_RE.fullmatch(value):
            return value.lower()

    for base in bases:
        try:
            lines = (base / "packed-refs").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line or line.startswith(("#", "^")):
                continue
            try:
                sha, packed_ref = line.split(" ", 1)
            except ValueError:
                continue
            if packed_ref == ref and _GIT_SHA_RE.fullmatch(sha):
                return sha.lower()
    return None


def resolve_build_sha(
    *,
    environ: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> tuple[str, str]:
    """Resolve a bounded release identifier, preferring deployment metadata."""

    env = os.environ if environ is None else environ
    for key in _BUILD_ENV_KEYS:
        release = _clean_release(env.get(key))
        if release is not None:
            return release, key.lower()

    root = repo_root or Path(__file__).resolve().parents[1]
    git_dir = _resolve_git_dir(root)
    if git_dir is not None:
        try:
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            head = ""
        if _GIT_SHA_RE.fullmatch(head):
            return head.lower(), "git_head"
        if head.startswith("ref:"):
            resolved = _read_ref(git_dir, head.split(":", 1)[1].strip())
            if resolved is not None:
                return resolved, "git_ref"

    return "unknown", "unavailable"


def compute_ui_asset_digest(project_root: Path | None = None) -> str:
    """Hash public UI files deterministically without exposing their contents."""

    root = project_root or Path(__file__).resolve().parents[1]
    asset_roots = (root / "api" / "static", root / "api" / "templates")
    files = sorted(
        (
            path
            for asset_root in asset_roots
            if asset_root.exists()
            for path in asset_root.rglob("*")
            if path.is_file()
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    digest = hashlib.sha256()
    try:
        for path in files:
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return "unavailable"
    return f"sha256:{digest.hexdigest()}"


def compute_source_digest(project_root: Path | None = None) -> str:
    """Hash only allowlisted runtime source/config files deterministically.

    Personal profiles, CVs, browser state, environment files, databases, and
    other operator-owned content are intentionally outside this allowlist.
    """

    root = project_root or Path(__file__).resolve().parents[1]
    files: list[Path] = []
    for source_name in _RUNTIME_SOURCE_ROOTS:
        source_root = root / source_name
        if not source_root.exists():
            continue
        files.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file() and path.suffix.lower() in _RUNTIME_SOURCE_SUFFIXES
        )
    migrations_root = root / "migrations"
    if migrations_root.exists():
        files.extend(path for path in migrations_root.rglob("*.py") if path.is_file())
    files.extend(path for name in _RUNTIME_CONFIG_FILES if (path := root / name).is_file())
    files = sorted(set(files), key=lambda path: path.relative_to(root).as_posix())
    if not files:
        return "unavailable"

    digest = hashlib.sha256()
    try:
        for path in files:
            relative = path.relative_to(root).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return "unavailable"
    return f"sha256:{digest.hexdigest()}"


def build_runtime_identity(
    *,
    environ: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    boot_id: str | None = None,
    started_at: datetime | None = None,
) -> RuntimeIdentity:
    """Create one immutable API/worker identity."""

    build_sha, build_source = resolve_build_sha(
        environ=environ,
        repo_root=project_root,
    )
    return RuntimeIdentity(
        build_sha=build_sha,
        build_source=build_source,
        ui_asset_digest=compute_ui_asset_digest(project_root),
        source_digest=compute_source_digest(project_root),
        protocol_version=PROTOCOL_VERSION,
        boot_id=boot_id or _BOOT_ID,
        started_at=started_at or _STARTED_AT,
    )


def runtime_source_is_current(
    identity: RuntimeIdentity | None = None,
    *,
    project_root: Path | None = None,
) -> bool:
    """Return false when allowlisted source changed after process startup."""

    startup_identity = identity or get_runtime_identity()
    return compute_source_digest(project_root) == startup_identity.source_digest


@lru_cache(maxsize=1)
def get_runtime_identity() -> RuntimeIdentity:
    """Return the process-wide runtime identity."""

    return build_runtime_identity()


def _effective_mode(settings: Settings) -> tuple[str, bool]:
    live_submit_enabled = (
        not settings.dry_run
        and not settings.draft_only
        and settings.portal_final_submit_enabled
        and settings.live_automation_acknowledged
        and not settings.auto_apply
        and settings.db_is_postgres
        and settings.operator_auth_configured
    )
    if settings.dry_run:
        return "dry_run", live_submit_enabled
    if settings.draft_only:
        return "draft_only", live_submit_enabled
    if not settings.portal_final_submit_enabled:
        return "prepare_only", live_submit_enabled
    if settings.auto_apply:
        return "blocked_unattended", live_submit_enabled
    if not settings.live_automation_acknowledged:
        return "blocked_unacknowledged", live_submit_enabled
    if not settings.operator_auth_configured:
        return "blocked_auth", live_submit_enabled
    return "explicit_live", live_submit_enabled


def build_runtime_capabilities(
    settings: Settings,
    readiness: Mapping[str, Any],
    identity: RuntimeIdentity | None = None,
    *,
    current_source_digest: str | None = None,
) -> dict[str, Any]:
    """Build the redacted capability response and fail-closed send decision."""

    release = identity or get_runtime_identity()
    observed_source_digest = (
        current_source_digest
        if current_source_digest is not None
        else compute_source_digest()
        if identity is None
        else release.source_digest
    )
    raw_checks = readiness.get("checks")
    checks_source = raw_checks if isinstance(raw_checks, Mapping) else {}
    checks: dict[str, bool] = {}
    for component in _READINESS_COMPONENTS:
        detail = checks_source.get(component)
        checks[component] = bool(detail.get("ok")) if isinstance(detail, Mapping) else False
    readiness_status = "ready" if all(checks.values()) else "degraded"

    worker_detail = checks_source.get("worker")
    if not isinstance(worker_detail, Mapping):
        worker_detail = {}
    worker_build = _clean_release(worker_detail.get("build_sha"))
    worker_protocol = _clean_release(worker_detail.get("protocol_version"))
    worker_source_digest = str(worker_detail.get("source_digest") or "")
    worker_release_id = _clean_release(worker_detail.get("release_id"))
    release_known = release.build_sha not in {"unknown", "unavailable"}
    source_known = _SOURCE_DIGEST_RE.fullmatch(release.source_digest) is not None
    source_current = (
        _SOURCE_DIGEST_RE.fullmatch(observed_source_digest) is not None
        and observed_source_digest == release.source_digest
    )
    worker_known = (
        worker_build not in {None, "unknown", "unavailable"}
        and worker_protocol is not None
        and worker_source_digest.startswith("sha256:")
        and len(worker_source_digest) == 71
        and worker_release_id not in {None, "unknown", "unavailable"}
    )
    worker_compatible = (
        release_known
        and source_known
        and source_current
        and worker_known
        and worker_build == release.build_sha
        and worker_protocol == release.protocol_version
        and worker_source_digest == release.source_digest
        and worker_release_id == release.release_id
    )

    mode_name, live_submit_enabled = _effective_mode(settings)
    reasons: list[SubmissionBlockReason] = []
    if settings.dry_run:
        reasons.append(SubmissionBlockReason.DRY_RUN_ENABLED)
    if settings.draft_only:
        reasons.append(SubmissionBlockReason.DRAFT_ONLY_ENABLED)
    if not settings.portal_final_submit_enabled:
        reasons.append(SubmissionBlockReason.FINAL_SUBMIT_DISABLED)
    if not settings.live_automation_acknowledged:
        reasons.append(SubmissionBlockReason.LIVE_AUTOMATION_NOT_ACKNOWLEDGED)
    if settings.auto_apply:
        reasons.append(SubmissionBlockReason.UNATTENDED_AUTOMATION_ENABLED)
    if not settings.db_is_postgres:
        reasons.append(SubmissionBlockReason.DATABASE_SERIALIZATION_REQUIRED)
    if not settings.operator_auth_configured:
        reasons.append(SubmissionBlockReason.OPERATOR_AUTH_REQUIRED)
    if readiness_status != "ready":
        reasons.append(SubmissionBlockReason.RUNTIME_NOT_READY)
    if not release_known or not source_known:
        reasons.append(SubmissionBlockReason.BUILD_IDENTITY_UNAVAILABLE)
    elif not source_current:
        reasons.append(SubmissionBlockReason.BUILD_MISMATCH)
    if not worker_known:
        reasons.append(SubmissionBlockReason.WORKER_IDENTITY_UNAVAILABLE)
    else:
        if (
            worker_build != release.build_sha
            or worker_source_digest != release.source_digest
            or worker_release_id != release.release_id
        ):
            reasons.append(SubmissionBlockReason.BUILD_MISMATCH)
        if worker_protocol != release.protocol_version:
            reasons.append(SubmissionBlockReason.PROTOCOL_MISMATCH)
    if not SUBMIT_COMMAND_PROTOCOL_AVAILABLE:
        reasons.append(SubmissionBlockReason.SUBMIT_COMMAND_UNAVAILABLE)

    return {
        "release": {
            "build_sha": release.build_sha,
            "ui_asset_digest": release.ui_asset_digest,
            "source_digest": release.source_digest,
            "release_id": release.release_id,
            "protocol_version": release.protocol_version,
            "boot_id": release.boot_id,
            "started_at": release.started_at.isoformat().replace("+00:00", "Z"),
        },
        "mode": {
            "name": mode_name,
            "dry_run": settings.dry_run,
            "draft_only": settings.draft_only,
            "live_submit_enabled": live_submit_enabled,
        },
        "readiness": {
            "status": readiness_status,
            "checks": checks,
        },
        "submission": {
            "allowed": (
                live_submit_enabled
                and readiness_status == "ready"
                and worker_compatible
                and SUBMIT_COMMAND_PROTOCOL_AVAILABLE
                and settings.db_is_postgres
            ),
            "reasons": [reason.value for reason in reasons],
        },
        "worker": {
            "build_sha": worker_build,
            "source_digest": worker_source_digest or None,
            "release_id": worker_release_id,
            "protocol_version": worker_protocol,
            "compatible": worker_compatible,
        },
    }
