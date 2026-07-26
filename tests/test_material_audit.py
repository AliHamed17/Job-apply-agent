from __future__ import annotations

from profile.models import CVArtifact, SelectedCVArtifact
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.routes.applications import _validate_material_quality
from core.material_audit import (
    invalidate_material_audit,
    material_review_reason,
    persist_material_audit,
)
from llm.claim_evidence import ClaimEvidenceRefV1
from llm.contracts import ModelIdentity
from llm.generation import GeneratedApplication, MaterialPackageV1, QAAnswerV1
from llm.qualification_registry import load_qualified_local_model

_CV_HASH = "c" * 64
_QUALIFIED_MODEL_DIGEST = load_qualified_local_model().digest


@pytest.fixture(autouse=True)
def _current_qualification_report(monkeypatch):
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: True,
    )


def _selected_cv() -> SelectedCVArtifact:
    return SelectedCVArtifact(
        cv_id="synthetic-cv",
        resolved_path="C:/private/synthetic.pdf",
        artifact=CVArtifact(
            pdf_sha256=_CV_HASH,
            byte_size=12,
            extracted_text="Python backend engineering",
        ),
    )


def _application():
    return SimpleNamespace(
        selected_cv_hash=None,
        material_eligible=None,
        material_blockers_json=None,
        material_claims_json=None,
        material_model_provider=None,
        material_model_name=None,
        material_model_digest=None,
        material_prompt_version=None,
    )


def _eligible_generated() -> GeneratedApplication:
    claim_digest = "d" * 64
    package = MaterialPackageV1(
        cv_sha256=_CV_HASH,
        profile_version=7,
        model_identity=ModelIdentity(
            provider="ollama",
            model="qwen2.5:7b",
            local=True,
            digest=_QUALIFIED_MODEL_DIGEST,
        ),
        cover_letter="I would welcome the opportunity to contribute.",
        recruiter_message="I am interested in the role.",
        qa_answers=(
            QAAnswerV1(
                field_id="relevant_experience",
                answer="I developed Python services.",
            ),
        ),
        claim_evidence=(
            ClaimEvidenceRefV1(
                claim_id="claim_" + "c" * 24,
                claim_digest=claim_digest,
                evidence_ids=("ev_" + "e" * 24,),
                evidence_quote_digests=("f" * 64,),
                supported=True,
            ),
        ),
        relevant_experience_claim_digests=(claim_digest,),
    )
    return GeneratedApplication(
        cover_letter=package.cover_letter,
        recruiter_message=package.recruiter_message,
        qa_answers=package.qa_answer_dict(),
        cv_sha256=_CV_HASH,
        profile_version=7,
        claim_evidence=list(package.claim_evidence),
        material_package=package,
    )


def test_material_audit_binds_eligibility_to_exact_cv_and_model() -> None:
    application = _application()

    blockers = persist_material_audit(application, _eligible_generated(), _selected_cv())

    assert blockers == []
    assert application.material_eligible is True
    assert application.selected_cv_hash == _CV_HASH
    assert application.material_model_provider == "ollama"
    assert application.material_model_name == "qwen2.5:7b"
    assert application.material_model_digest == _QUALIFIED_MODEL_DIGEST


def test_material_audit_blocks_hash_mismatch_and_untyped_clean_text() -> None:
    mismatched = _eligible_generated()
    mismatched.cv_sha256 = "d" * 64
    mismatched.material_package = mismatched.material_package.model_copy(
        update={"cv_sha256": "d" * 64}
    )
    application = _application()

    blockers = persist_material_audit(application, mismatched, _selected_cv())

    assert application.material_eligible is False
    assert "MATERIAL_CV_MISMATCH" in blockers

    legacy_clean = _application()
    blockers = persist_material_audit(
        legacy_clean,
        GeneratedApplication(cover_letter="Clean-looking but unaudited."),
        _selected_cv(),
    )
    assert legacy_clean.material_eligible is False
    assert blockers == ["MATERIAL_NOT_ELIGIBLE"]


@pytest.mark.parametrize(
    ("identity_update", "package_update"),
    [
        ({"provider": "local-test"}, {}),
        ({"model": "qwen2.5:3b"}, {}),
        ({"local": False}, {}),
        ({"digest": None}, {}),
        ({}, {"prompt_version": "application-materials-stale"}),
    ],
)
def test_material_audit_rejects_every_unqualified_identity(
    identity_update,
    package_update,
) -> None:
    generated = _eligible_generated()
    package = generated.material_package
    package = package.model_copy(
        update={
            "model_identity": package.model_identity.model_copy(update=identity_update),
            **package_update,
        }
    )
    generated.material_package = package
    application = _application()

    blockers = persist_material_audit(application, generated, _selected_cv())

    assert application.material_eligible is False
    assert blockers == ["MATERIAL_MODEL_NOT_QUALIFIED"]
    assert application.material_blockers_json == '["MATERIAL_MODEL_NOT_QUALIFIED"]'


def test_prepare_quality_boundary_rejects_forged_wrong_local_model() -> None:
    application = _application()
    persist_material_audit(application, _eligible_generated(), _selected_cv())
    application.material_model_name = "qwen2.5:3b"

    with pytest.raises(HTTPException) as exc_info:
        _validate_material_quality(application)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "MATERIAL_MODEL_NOT_QUALIFIED"


def test_review_reason_uses_bounded_codes_without_private_field_text() -> None:
    assert (
        material_review_reason(
            selected_cv_id="synthetic",
            routing_fallback_reason=None,
            blockers=["UNFILLED_PLACEHOLDER"],
            placeholder_fields=["private free-form prompt"],
        )
        == "UNFILLED_PLACEHOLDER"
    )


def test_material_blocker_precedes_simultaneous_routing_fallback() -> None:
    assert (
        material_review_reason(
            selected_cv_id=None,
            routing_fallback_reason="confidence_below_threshold",
            blockers=["MATERIAL_CV_ARTIFACT_REQUIRED"],
            placeholder_fields=[],
        )
        == "MATERIAL_CV_ARTIFACT_REQUIRED"
    )


def test_cv_routing_change_revokes_every_material_binding() -> None:
    application = _application()
    persist_material_audit(application, _eligible_generated(), _selected_cv())
    assert application.material_eligible is True

    invalidate_material_audit(application)

    assert application.selected_cv_hash is None
    assert application.material_eligible is False
    assert application.material_blockers_json == '["MATERIAL_NOT_ELIGIBLE"]'
    assert application.material_claims_json == "[]"
    assert application.material_model_provider is None
