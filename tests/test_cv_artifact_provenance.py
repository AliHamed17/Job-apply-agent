"""CV byte identity and profile provenance safety."""

from __future__ import annotations

import hashlib
from profile.cv_content_cache import (
    CVArtifactBindingError,
    clear_cv_text_cache,
    get_selected_cv_artifact_by_id,
    require_current_selected_cv_artifact,
)
from profile.cv_facts import build_selected_cv_fact_catalog
from profile.models import (
    CVArtifactFactV1,
    SelectedCVFactCatalog,
    UserProfile,
)

import pytest
import yaml
from pydantic import ValidationError


def _routing_file(tmp_path, filename: str = "resume.pdf"):
    path = tmp_path / "cv_routing.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "cvs": [{"id": "software", "file": filename}],
                "fallback_cv_id": "software",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_selected_cv_artifact_is_content_addressed_and_private(tmp_path, monkeypatch):
    clear_cv_text_cache()
    pdf = tmp_path / "resume.pdf"
    first_bytes = b"%PDF-1.4 first synthetic CV"
    second_bytes = b"%PDF-1.4 replaced synthetic CV"
    pdf.write_bytes(first_bytes)
    routing = _routing_file(tmp_path)
    calls: list[bytes] = []

    def extract(path):
        payload = path.read_bytes()
        calls.append(payload)
        return f"extracted:{payload.decode(errors='ignore')}"

    monkeypatch.setattr("profile.cv_content_cache.extract_text_from_pdf", extract)

    first = get_selected_cv_artifact_by_id(
        "software",
        cv_routing_path=routing,
        cv_directory=tmp_path,
    )
    assert first is not None
    assert first.pdf_sha256 == hashlib.sha256(first_bytes).hexdigest()
    assert first.extracted_text.startswith("extracted:")
    assert first.cv_id == "software"
    serialized = first.model_dump()
    assert "resolved_path" not in serialized
    assert "extracted_text" not in serialized["artifact"]

    cached = get_selected_cv_artifact_by_id(
        "software",
        cv_routing_path=routing,
        cv_directory=tmp_path,
    )
    assert cached is not None
    assert cached.pdf_sha256 == first.pdf_sha256
    assert calls == [first_bytes]

    pdf.write_bytes(second_bytes)
    replaced = get_selected_cv_artifact_by_id(
        "software",
        cv_routing_path=routing,
        cv_directory=tmp_path,
    )
    assert replaced is not None
    assert replaced.pdf_sha256 == hashlib.sha256(second_bytes).hexdigest()
    assert replaced.pdf_sha256 != first.pdf_sha256
    assert calls == [first_bytes, second_bytes]

    with pytest.raises(ValidationError):
        replaced.artifact.pdf_sha256 = "0" * 64


def test_configured_cv_cannot_escape_private_cv_directory(tmp_path):
    clear_cv_text_cache()
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"not reachable")
    routing = _routing_file(tmp_path, "../outside.pdf")

    assert (
        get_selected_cv_artifact_by_id(
            "software",
            cv_routing_path=routing,
            cv_directory=tmp_path,
        )
        is None
    )


def test_selected_cv_fact_catalog_is_literal_bounded_and_redacted():
    from profile.models import CVArtifact, SelectedCVArtifact

    from core.form_plan_evidence import SelectedCVPlanningBinding
    from core.form_planning import AnswerPolicyV1
    from core.submission_domain import AnswerProvenance, FormFieldV1

    text = (
        "Primary programming language: Python\n"
        "Cloud platform: AWS\n"
        "Nationality: private value\n"
        "Skills: Python, AWS, Kubernetes\n"
    )
    artifact = CVArtifact(
        pdf_sha256=hashlib.sha256(b"synthetic exact CV bytes").hexdigest(),
        byte_size=24,
        extracted_text=text,
    )
    selected = SelectedCVArtifact(
        cv_id="software",
        resolved_path="C:/private/resume.pdf",
        artifact=artifact,
    )
    catalog = build_selected_cv_fact_catalog(artifact)
    binding = SelectedCVPlanningBinding(selected_cv=selected, fact_catalog=catalog)

    assert catalog.schema_version == "selected-cv-fact-catalog-v1"
    assert catalog.as_dict() == {
        "cloud_platform": "AWS",
        "primary_language": "Python",
    }
    serialized = catalog.model_dump()
    assert "artifact" not in serialized
    assert "facts" not in serialized
    diagnostics = binding.redacted_diagnostics()
    assert diagnostics["fact_count"] == 2
    assert diagnostics["selected_cv_hash"] == artifact.pdf_sha256
    assert diagnostics["fact_catalog_digest"] == catalog.catalog_sha256
    assert "Python" not in str(diagnostics)
    assert "resume.pdf" not in str(diagnostics)

    from core.form_planning import AnswerPolicyContext

    context = AnswerPolicyContext(
        profile=UserProfile(),
        profile_version=1,
        selected_cv_id="software",
        selected_cv_hash=artifact.pdf_sha256,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
        selected_cv_fact_catalog=catalog,
    )
    field = FormFieldV1.model_validate(
        {
            "field_id": "language",
            "canonical_name": "primary_language",
            "label": "Primary programming language",
            "field_type": "text",
            "required": True,
            "position": 0,
        }
    )
    result = __import__("asyncio").run(AnswerPolicyV1().plan_fields((field,), context))
    assert result.decisions[0].value == "Python"
    assert result.decisions[0].provenance == AnswerProvenance.CV_EVIDENCE
    assert result.decisions[0].evidence_refs == (f"cv:{artifact.pdf_sha256}:primary_language",)


def test_catalog_rejects_semantically_mislabelled_source_value():
    from profile.models import CVArtifact

    artifact = CVArtifact(
        pdf_sha256="c" * 64,
        byte_size=42,
        extracted_text="Primary programming language: Python",
    )
    with pytest.raises(ValidationError):
        SelectedCVFactCatalog(
            artifact=artifact,
            facts=(
                CVArtifactFactV1(
                    canonical_name="highest_degree",
                    value="Python",
                    source_quote="Primary programming language: Python",
                ),
            ),
        )


def test_selected_cv_binding_fails_after_path_mutation(tmp_path, monkeypatch):
    clear_cv_text_cache()
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 stable CV")
    routing = _routing_file(tmp_path)
    monkeypatch.setattr(
        "profile.cv_content_cache.extract_text_from_pdf",
        lambda _path: "Primary programming language: Python",
    )
    selected = get_selected_cv_artifact_by_id(
        "software",
        cv_routing_path=routing,
        cv_directory=tmp_path,
    )
    assert selected is not None
    require_current_selected_cv_artifact(
        selected,
        expected_sha256=selected.pdf_sha256,
    )

    pdf.write_bytes(b"%PDF-1.4 replaced CV")

    with pytest.raises(CVArtifactBindingError, match="CV_ARTIFACT_CHANGED"):
        require_current_selected_cv_artifact(
            selected,
            expected_sha256=selected.pdf_sha256,
        )


def test_legacy_sensitive_profile_value_is_quarantined_not_confirmed():
    profile = UserProfile.model_validate(
        {
            "personal": {
                "name": "Example Candidate",
                "work_authorization": "Legacy unproven value",
            }
        }
    )

    assert profile.personal.work_authorization == "Legacy unproven value"
    assert profile.evidence.cv_extracted["work_authorization"] == "Legacy unproven value"
    assert profile.evidence.confirmed_fact("work authorization") is None
    assert "work_authorization" not in profile.model_dump()["personal"]


def test_conflicting_confirmed_fact_aliases_fail_closed():
    with pytest.raises(ValidationError):
        UserProfile.model_validate(
            {
                "evidence": {
                    "user_confirmed": {
                        "work authorization": "First value",
                        "work_authorization": "Conflicting value",
                    }
                }
            }
        )
