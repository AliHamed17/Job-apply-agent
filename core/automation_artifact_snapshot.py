"""Immutable private artifact snapshots for signed autopilot authority.

The operator-owned routing file, qualification report, and configured CVs can
change independently of the database.  A signed policy therefore receives a
content-addressed local snapshot before it becomes active.  Submission
preflight reads the routed CV only from that immutable version so a concurrent
replacement of the live files cannot change an already-authorized action.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from profile.cv_content_cache import (
    get_cv_artifact_by_path,
    load_configured_cv_artifacts,
)
from profile.cv_routing import load_routing_config
from profile.models import SelectedCVArtifact
from typing import Any

from core.automation_policy import AutoSubmitPolicyV1
from core.config import get_settings
from discovery.contracts import stable_digest
from match.job_fit import (
    cv_manifest_digest,
    load_fit_qualification,
    qualification_matches,
    routing_config_digest,
)
from match.job_fit_runtime import configured_fit_qualification_path

_SCHEMA_VERSION = "automation-artifact-snapshot.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COPY_CHUNK_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 128 * 1024


class AutomationArtifactSnapshotError(RuntimeError):
    """The private immutable artifact set is missing or no longer trustworthy."""


@dataclass(frozen=True, slots=True, repr=False)
class AutomationArtifactSnapshot:
    """Verified local-only paths for one exact signed artifact set."""

    snapshot_id: str
    root: Path
    routing_config_path: Path
    fit_qualification_path: Path
    cv_entries: tuple[tuple[str, str, Path], ...]

    def __repr__(self) -> str:
        return f"AutomationArtifactSnapshot(snapshot_id={self.snapshot_id!r}, <private>)"

    def selected_path(self, cv_id: str, expected_sha256: str) -> Path:
        for observed_id, observed_sha256, path in self.cv_entries:
            if observed_id == cv_id:
                if not hmac.compare_digest(expected_sha256, observed_sha256):
                    raise AutomationArtifactSnapshotError("selected CV snapshot hash changed")
                return path
        raise AutomationArtifactSnapshotError("selected CV is absent from policy snapshot")


def policy_artifact_snapshot_id(policy: AutoSubmitPolicyV1) -> str:
    """Return the deterministic identity of the policy's private artifacts."""

    return stable_digest(
        {
            "schema_version": _SCHEMA_VERSION,
            "routing_config_digest": policy.routing_config_digest,
            "cv_manifest_digest": policy.cv_manifest_digest,
            "fit_qualification_digest": policy.fit_qualification_digest,
        }
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_root(settings: Any) -> Path:
    root = (settings.data_dir / ".automation_artifacts").resolve()
    if root.exists() and (not root.is_dir() or root.is_symlink()):
        raise AutomationArtifactSnapshotError("artifact snapshot root is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _private_child(root: Path, relative: str) -> Path:
    candidate = root / relative
    if candidate.is_symlink():
        raise AutomationArtifactSnapshotError("artifact snapshot contains a link")
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise AutomationArtifactSnapshotError("artifact snapshot path escaped its root") from exc
    return candidate


def _read_manifest(snapshot_root: Path) -> dict[str, Any]:
    manifest_path = _private_child(snapshot_root, "manifest.json")
    if not manifest_path.is_file() or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise AutomationArtifactSnapshotError("artifact snapshot manifest is unavailable")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AutomationArtifactSnapshotError("artifact snapshot manifest is invalid") from exc
    if not isinstance(payload, dict):
        raise AutomationArtifactSnapshotError("artifact snapshot manifest is invalid")
    return payload


def _manifest_entries(
    payload: dict[str, Any], snapshot_root: Path
) -> tuple[tuple[str, str, Path], ...]:
    raw_entries = payload.get("cvs")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise AutomationArtifactSnapshotError("artifact snapshot CV manifest is invalid")
    entries: list[tuple[str, str, Path]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict) or set(raw) != {"cv_id", "pdf_sha256", "path"}:
            raise AutomationArtifactSnapshotError("artifact snapshot CV entry is invalid")
        cv_id = str(raw["cv_id"])
        pdf_sha256 = str(raw["pdf_sha256"])
        relative = str(raw["path"])
        if (
            not cv_id
            or len(cv_id) > 200
            or cv_id in seen_ids
            or _SHA256_RE.fullmatch(pdf_sha256) is None
            or relative != f"cvs/{index:03d}.pdf"
        ):
            raise AutomationArtifactSnapshotError("artifact snapshot CV entry is invalid")
        path = _private_child(snapshot_root, relative)
        if not path.is_file():
            raise AutomationArtifactSnapshotError("artifact snapshot CV is unavailable")
        seen_ids.add(cv_id)
        entries.append((cv_id, pdf_sha256, path))
    if tuple(entries) != tuple(sorted(entries, key=lambda item: item[0])):
        raise AutomationArtifactSnapshotError("artifact snapshot CV entries are not canonical")
    return tuple(entries)


def _manifest_cv_digest(entries: tuple[tuple[str, str, Path], ...]) -> str:
    return stable_digest(
        [{"cv_id": cv_id, "pdf_sha256": pdf_sha256} for cv_id, pdf_sha256, _path in entries]
    )


def _load_snapshot(
    snapshot_root: Path,
    policy: AutoSubmitPolicyV1,
    *,
    verify_all_cv_bytes: bool,
    selected_cv_id: str | None = None,
    selected_cv_hash: str | None = None,
) -> AutomationArtifactSnapshot:
    if not snapshot_root.is_dir() or snapshot_root.is_symlink():
        raise AutomationArtifactSnapshotError("artifact snapshot is unavailable")
    expected_id = policy_artifact_snapshot_id(policy)
    payload = _read_manifest(snapshot_root)
    if (
        payload.get("schema_version") != _SCHEMA_VERSION
        or payload.get("snapshot_id") != expected_id
        or payload.get("routing_config_digest") != policy.routing_config_digest
        or payload.get("cv_manifest_digest") != policy.cv_manifest_digest
        or payload.get("fit_qualification_digest") != policy.fit_qualification_digest
        or payload.get("routing_config_path") != "routing.yaml"
        or payload.get("fit_qualification_path") != "fit-qualification.json"
    ):
        raise AutomationArtifactSnapshotError("artifact snapshot binding changed")

    routing_path = _private_child(snapshot_root, "routing.yaml")
    qualification_path = _private_child(snapshot_root, "fit-qualification.json")
    if not routing_path.is_file() or not qualification_path.is_file():
        raise AutomationArtifactSnapshotError("artifact snapshot metadata is unavailable")
    try:
        config = load_routing_config(routing_path)
        qualification = load_fit_qualification(qualification_path)
    except Exception as exc:
        raise AutomationArtifactSnapshotError("artifact snapshot metadata is invalid") from exc
    if routing_config_digest(config) != policy.routing_config_digest:
        raise AutomationArtifactSnapshotError("routing snapshot digest changed")

    entries = _manifest_entries(payload, snapshot_root)
    if _manifest_cv_digest(entries) != policy.cv_manifest_digest:
        raise AutomationArtifactSnapshotError("CV snapshot manifest digest changed")
    if set(item.id for item in config.cvs) != {cv_id for cv_id, _digest, _path in entries}:
        raise AutomationArtifactSnapshotError("routing and CV snapshot identities differ")
    if (
        qualification.qualification_digest != policy.fit_qualification_digest
        or not qualification_matches(
            qualification,
            config_digest=policy.routing_config_digest,
            manifest_digest=policy.cv_manifest_digest,
        )
    ):
        raise AutomationArtifactSnapshotError("fit qualification snapshot changed")

    selected_found = selected_cv_id is None
    for cv_id, pdf_sha256, path in entries:
        must_hash = verify_all_cv_bytes or cv_id == selected_cv_id
        if must_hash and _sha256_file(path) != pdf_sha256:
            raise AutomationArtifactSnapshotError("CV snapshot bytes changed")
        if cv_id == selected_cv_id:
            selected_found = True
            if selected_cv_hash is None or selected_cv_hash != pdf_sha256:
                raise AutomationArtifactSnapshotError("selected CV snapshot binding changed")
    if not selected_found:
        raise AutomationArtifactSnapshotError("selected CV is absent from policy snapshot")

    return AutomationArtifactSnapshot(
        snapshot_id=expected_id,
        root=snapshot_root,
        routing_config_path=routing_path,
        fit_qualification_path=qualification_path,
        cv_entries=entries,
    )


def _copy_exact(source: Path, destination: Path, expected_sha256: str | None = None) -> None:
    digest = hashlib.sha256()
    with source.open("rb") as reader, destination.open("xb") as writer:
        for chunk in iter(lambda: reader.read(_COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        raise AutomationArtifactSnapshotError("source artifact changed while snapshotting")


def _remove_staging(staging: Path, root: Path) -> None:
    try:
        staging.resolve().relative_to(root.resolve())
    except ValueError:
        return
    if staging.name.startswith(".") and staging.is_dir():
        shutil.rmtree(staging)


def materialize_policy_artifact_snapshot(
    policy: AutoSubmitPolicyV1,
    *,
    settings=None,
) -> AutomationArtifactSnapshot:
    """Atomically create and verify the policy's immutable local artifact set."""

    resolved_settings = settings or get_settings()
    root = _snapshot_root(resolved_settings)
    snapshot_id = policy_artifact_snapshot_id(policy)
    # Keep paths below the legacy Windows MAX_PATH boundary even when the
    # application data directory itself is deeply nested. The complete digest
    # remains in the manifest and is verified on every lookup; a truncated-name
    # collision therefore fails closed instead of selecting the other set.
    target = root / snapshot_id[:32]
    if target.exists():
        return _load_snapshot(target, policy, verify_all_cv_bytes=True)

    routing_source = Path(resolved_settings.cv_routing_path).resolve()
    qualification_source = Path(configured_fit_qualification_path()).resolve()
    try:
        config = load_routing_config(routing_source)
        artifacts = dict(load_configured_cv_artifacts(config, resolved_settings.cv_directory))
        qualification = load_fit_qualification(qualification_source)
    except Exception as exc:
        raise AutomationArtifactSnapshotError("policy artifacts are unavailable") from exc
    if (
        routing_config_digest(config) != policy.routing_config_digest
        or set(artifacts) != {item.id for item in config.cvs}
        or cv_manifest_digest(artifacts) != policy.cv_manifest_digest
        or qualification.qualification_digest != policy.fit_qualification_digest
        or not qualification_matches(
            qualification,
            config_digest=policy.routing_config_digest,
            manifest_digest=policy.cv_manifest_digest,
        )
    ):
        raise AutomationArtifactSnapshotError("policy artifacts changed before snapshotting")

    staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=root))
    try:
        (staging / "cvs").mkdir()
        _copy_exact(routing_source, staging / "routing.yaml")
        _copy_exact(qualification_source, staging / "fit-qualification.json")
        cv_entries: list[dict[str, str]] = []
        for index, (cv_id, artifact) in enumerate(sorted(artifacts.items())):
            relative_path = f"cvs/{index:03d}.pdf"
            destination = staging / relative_path
            _copy_exact(
                Path(artifact.resolved_path),
                destination,
                expected_sha256=artifact.pdf_sha256,
            )
            cv_entries.append(
                {
                    "cv_id": cv_id,
                    "pdf_sha256": artifact.pdf_sha256,
                    "path": relative_path,
                }
            )
        manifest = {
            "schema_version": _SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "routing_config_digest": policy.routing_config_digest,
            "cv_manifest_digest": policy.cv_manifest_digest,
            "fit_qualification_digest": policy.fit_qualification_digest,
            "routing_config_path": "routing.yaml",
            "fit_qualification_path": "fit-qualification.json",
            "cvs": cv_entries,
        }
        with (staging / "manifest.json").open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _load_snapshot(staging, policy, verify_all_cv_bytes=True)
        try:
            os.rename(staging, target)
        except OSError:
            if not target.is_dir():
                raise
            _remove_staging(staging, root)
        return _load_snapshot(target, policy, verify_all_cv_bytes=True)
    except Exception:
        _remove_staging(staging, root)
        raise


def require_policy_artifact_snapshot(
    policy: AutoSubmitPolicyV1,
    *,
    settings=None,
    selected_cv_id: str | None = None,
    selected_cv_hash: str | None = None,
) -> AutomationArtifactSnapshot:
    """Verify an existing snapshot without consulting mutable source paths."""

    resolved_settings = settings or get_settings()
    root = _snapshot_root(resolved_settings)
    target = root / policy_artifact_snapshot_id(policy)[:32]
    return _load_snapshot(
        target,
        policy,
        verify_all_cv_bytes=False,
        selected_cv_id=selected_cv_id,
        selected_cv_hash=selected_cv_hash,
    )


def resolve_selected_cv_artifact_snapshot(
    policy: AutoSubmitPolicyV1,
    *,
    cv_id: str,
    expected_sha256: str,
    settings=None,
) -> tuple[SelectedCVArtifact, str]:
    """Resolve one selected CV from the immutable policy version."""

    snapshot = require_policy_artifact_snapshot(
        policy,
        settings=settings,
        selected_cv_id=cv_id,
        selected_cv_hash=expected_sha256,
    )
    path = snapshot.selected_path(cv_id, expected_sha256)
    artifact = get_cv_artifact_by_path(path)
    if artifact is None or artifact.pdf_sha256 != expected_sha256:
        raise AutomationArtifactSnapshotError("selected CV snapshot is unavailable")
    return (
        SelectedCVArtifact(
            cv_id=cv_id,
            resolved_path=str(path.resolve()),
            artifact=artifact,
        ),
        snapshot.snapshot_id,
    )


__all__ = [
    "AutomationArtifactSnapshot",
    "AutomationArtifactSnapshotError",
    "materialize_policy_artifact_snapshot",
    "policy_artifact_snapshot_id",
    "require_policy_artifact_snapshot",
    "resolve_selected_cv_artifact_snapshot",
]
