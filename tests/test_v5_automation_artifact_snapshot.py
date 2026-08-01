"""Immutable private artifact bindings for Release-4 final admission."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from profile.cv_content_cache import load_configured_cv_artifacts
from profile.cv_routing import load_routing_config
from uuid import uuid4

import pytest
import yaml
from pypdf import PdfWriter

from core.automation_artifact_snapshot import (
    AutomationArtifactSnapshotError,
    materialize_policy_artifact_snapshot,
    policy_artifact_snapshot_id,
    require_policy_artifact_snapshot,
    resolve_selected_cv_artifact_snapshot,
)
from core.automation_policy import (
    AutomationGeography,
    AutoSubmitPolicyV1,
    QualifiedFormContractV1,
)
from core.config import Settings
from match.job_fit import (
    FitQualificationV1,
    FitThresholdsV1,
    cv_manifest_digest,
    routing_config_digest,
)

_NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)


def _write_pdf(path: Path, *, width: float) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=792)
    with path.open("wb") as handle:
        writer.write(handle)
    return path.read_bytes()


def _policy_fixture(tmp_path: Path, monkeypatch) -> tuple[Settings, AutoSubmitPolicyV1, Path]:
    cv_root = tmp_path / "cvs"
    cv_root.mkdir()
    cv_path = cv_root / "ai.pdf"
    _write_pdf(cv_path, width=612)
    routing_path = tmp_path / "cv_routing.yaml"
    routing_path.write_text(
        yaml.safe_dump(
            {
                "minimum_confidence": 0.35,
                "fallback_cv_id": None,
                "cvs": [
                    {
                        "id": "ai-ml",
                        "file": "ai.pdf",
                        "title_terms": ["machine learning"],
                        "skills": ["python"],
                        "seniority": ["senior"],
                    }
                ],
                "overrides": [],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = load_routing_config(routing_path)
    artifacts = load_configured_cv_artifacts(config, cv_root)
    qualification = FitQualificationV1(
        routing_config_digest=routing_config_digest(config),
        cv_manifest_digest=cv_manifest_digest(artifacts),
        dataset_digest="7" * 64,
        thresholds=FitThresholdsV1(),
        labeled_cases=240,
        holdout_cases=48,
        holdout_precision=0.97,
        holdout_coverage=0.5,
        qualified=True,
        created_at=_NOW,
    )
    qualification_path = tmp_path / "fit-qualification.json"
    qualification_path.write_text(
        qualification.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FIT_ROUTING_QUALIFICATION_PATH", str(qualification_path))
    settings = Settings(
        _env_file=None,
        app_env="test",
        application_data_dir=str(tmp_path),
        cv_routing_path=str(routing_path),
        cv_directory=str(cv_root),
    )
    policy = AutoSubmitPolicyV1(
        policy_id=uuid4(),
        revision=1,
        role_families=("ai-ml",),
        geographies=(AutomationGeography.ISRAEL,),
        permitted_adapters=("greenhouse",),
        qualified_form_contracts=(
            QualifiedFormContractV1(
                adapter_name="greenhouse",
                adapter_version="1.0.0",
                selector_version="greenhouse-v1",
                form_contract_digest="f" * 64,
            ),
        ),
        profile_version=1,
        routing_config_digest=qualification.routing_config_digest,
        cv_manifest_digest=qualification.cv_manifest_digest,
        fit_qualification_digest=qualification.qualification_digest,
        confirmed_answer_revision="e" * 64,
        activated_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
    )
    return settings, policy, cv_path


def test_policy_snapshot_keeps_inflight_cv_immutable_after_live_replacement(
    tmp_path,
    monkeypatch,
) -> None:
    settings, policy, live_cv_path = _policy_fixture(tmp_path, monkeypatch)
    original_bytes = live_cv_path.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()

    created = materialize_policy_artifact_snapshot(policy, settings=settings)
    assert created.snapshot_id == policy_artifact_snapshot_id(policy)
    assert created.root.parent == (tmp_path / ".automation_artifacts").resolve()

    _write_pdf(live_cv_path, width=500)
    Path(settings.cv_routing_path).write_text("cvs: []\n", encoding="utf-8")
    Path(os.environ["FIT_ROUTING_QUALIFICATION_PATH"]).write_text(
        "{}\n",
        encoding="utf-8",
    )

    selected, snapshot_id = resolve_selected_cv_artifact_snapshot(
        policy,
        cv_id="ai-ml",
        expected_sha256=original_hash,
        settings=settings,
    )

    assert snapshot_id == created.snapshot_id
    assert Path(selected.resolved_path).read_bytes() == original_bytes
    assert Path(selected.resolved_path) != live_cv_path.resolve()
    assert selected.pdf_sha256 == original_hash


def test_policy_snapshot_rejects_tampered_versioned_cv(tmp_path, monkeypatch) -> None:
    settings, policy, _live_cv_path = _policy_fixture(tmp_path, monkeypatch)
    snapshot = materialize_policy_artifact_snapshot(policy, settings=settings)
    cv_id, cv_hash, cv_path = snapshot.cv_entries[0]
    assert cv_id == "ai-ml"
    cv_path.write_bytes(cv_path.read_bytes() + b"tampered")

    with pytest.raises(AutomationArtifactSnapshotError, match="CV snapshot bytes changed"):
        require_policy_artifact_snapshot(
            policy,
            settings=settings,
            selected_cv_id=cv_id,
            selected_cv_hash=cv_hash,
        )
