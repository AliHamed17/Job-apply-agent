"""Offline API integration coverage for Greenhouse form inspection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from profile.models import UserProfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from core.submission_domain import (
    VERIFIED_ATTACHMENT_EVIDENCE_REF,
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    FieldType,
    FormFieldConstraintsV1,
    FormFieldV1,
    FormPlanV1,
)
from db.models import (
    Application,
    Base,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
)
from submitters.greenhouse_v1 import GreenhouseBrowserV1

GREENHOUSE_URL = "https://boards.greenhouse.io/fixture/jobs/1001"


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'greenhouse-inspection-api.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _application(factory) -> Application:
    db = factory()
    application = Application(
        job=Job(
            title="Fixture role",
            company="Fixture company",
            source_url=GREENHOUSE_URL,
            apply_url=GREENHOUSE_URL,
            status=JobStatus.DRAFT,
        ),
        status=JobStatus.DRAFT,
        revision=1,
        selected_cv_id="fixture-cv",
        selected_cv_hash="a" * 64,
        profile_version=3,
        material_eligible=True,
        material_model_provider="ollama",
        material_model_name="qwen2.5:7b",
        material_model_digest=f"sha256:{'e' * 64}",
        material_prompt_version="material-package-v1",
    )
    db.add(application)
    db.commit()
    db.close()
    return application


def _domain_plan(application_id: int, revision: int) -> FormPlanV1:
    now = datetime.now(UTC)
    resume = FormFieldV1(
        field_id="resume",
        canonical_name="resume",
        label="Resume",
        field_type=FieldType.FILE,
        required=True,
        position=0,
        constraints=FormFieldConstraintsV1(
            accepted_file_types=(".pdf", "application/pdf"),
        ),
    )
    attachment = AnswerDecisionV1(
        field_id=resume.field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
        value=VERIFIED_ATTACHMENT_SENTINEL,
        confidence=1.0,
        evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
    )
    return FormPlanV1(
        plan_id=uuid4(),
        application_id=application_id,
        application_revision=revision,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-candidate-v9",
        form_fingerprint="b" * 64,
        selected_cv_id="fixture-cv",
        selected_cv_hash="a" * 64,
        attached_cv_id="fixture-cv",
        attached_cv_hash="a" * 64,
        attachment_verified=True,
        profile_version=3,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        fields=(resume,),
        decisions=(attachment,),
    )


class _OfflineInspector:
    def __init__(self, plan: FormPlanV1) -> None:
        self.plan = plan
        self.calls: list[dict] = []

    async def inspect(self, *, answer_policy=None, **kwargs):
        self.calls.append(
            {
                **kwargs,
                "answer_policy": answer_policy,
                "db_transaction_open": answer_policy.db.in_transaction(),
            }
        )
        return self.plan


def _patch_private_inputs(monkeypatch, tmp_path) -> None:
    cv_path = tmp_path / "fixture.pdf"
    cv_path.write_bytes(b"sanitized offline fixture bytes")
    selected = SimpleNamespace(resolved_path=str(cv_path))
    monkeypatch.setattr(applications_route, "_validate_selected_cv", lambda _app: None)
    monkeypatch.setattr(applications_route, "_validate_material_quality", lambda _app: None)
    monkeypatch.setattr(
        applications_route,
        "get_selected_cv_artifact_by_id",
        lambda _cv_id: selected,
    )
    monkeypatch.setattr(
        applications_route,
        "require_current_selected_cv_artifact",
        lambda artifact, **_kwargs: artifact,
    )
    monkeypatch.setattr(
        applications_route,
        "load_versioned_profile_snapshot",
        lambda _db, **_kwargs: SimpleNamespace(profile=UserProfile()),
    )


@pytest.mark.asyncio
async def test_fixture_qualified_registry_rejects_arbitrary_greenhouse_url_before_command(
    tmp_path,
    monkeypatch,
) -> None:
    factory = _factory(tmp_path)
    application = _application(factory)
    _patch_private_inputs(monkeypatch, tmp_path)
    forbidden_inspection = AsyncMock(
        side_effect=AssertionError("fixture qualification must not open an employer form")
    )
    monkeypatch.setattr(GreenhouseBrowserV1, "inspect", forbidden_inspection)
    db = factory()

    with pytest.raises(HTTPException) as exc_info:
        await applications_route.inspect_application_form(
            application.id,
            applications_route.InspectApplicationRequest(application_revision=1),
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "code": "ADAPTER_NOT_QUALIFIED",
        "message": "No version-qualified browser inspector is available for this form.",
    }
    forbidden_inspection.assert_not_awaited()
    assert db.query(FormPlan).count() == 0
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.expire_all()
    current = db.get(Application, application.id)
    assert current.status == JobStatus.DRAFT
    assert current.revision == 1
    assert current.prepared_revision is None
    assert current.approved_at is None
    db.close()


@pytest.mark.asyncio
async def test_injected_offline_inspector_persists_greenhouse_plan_without_command(
    tmp_path,
    monkeypatch,
) -> None:
    factory = _factory(tmp_path)
    application = _application(factory)
    inspector = _OfflineInspector(_domain_plan(application.id, application.revision))
    _patch_private_inputs(monkeypatch, tmp_path)
    import submitters.registry

    monkeypatch.setattr(
        submitters.registry,
        "get_two_phase_registry",
        lambda: SimpleNamespace(get_inspector=lambda _job: inspector),
    )
    db = factory()

    response = await applications_route.inspect_application_form(
        application.id,
        applications_route.InspectApplicationRequest(application_revision=1),
        db,
    )

    assert response.adapter_name == "greenhouse"
    assert response.adapter_version == "1.0.0"
    assert response.selector_version == "greenhouse-candidate-v9"
    assert response.attachment_verified is True
    assert response.attachment_verification_source == "candidate_browser_upload_complete"
    assert response.attachment_verified_at is not None
    assert response.selected_cv_ref == response.attached_cv_ref
    assert response.selected_cv_ref != response.selected_cv_id
    assert response.valid is False
    assert response.fields == [
        {
            "field_id": "resume",
            "canonical_name": "resume",
            "label": "Resume",
            "field_type": "file",
            "required": True,
            "position": 0,
            "options": [],
            "constraints": {
                "min_length": None,
                "max_length": None,
                "min_value": None,
                "max_value": None,
                "pattern": None,
                "accepted_file_types": [".pdf", "application/pdf"],
                "max_file_bytes": None,
                "multiple": False,
            },
            "sensitive_category": None,
        }
    ]
    assert len(inspector.calls) == 1
    assert inspector.calls[0]["selected_cv_id"] == "fixture-cv"
    assert inspector.calls[0]["answer_policy"].db is db
    assert inspector.calls[0]["answer_policy"].llm_client is None
    assert inspector.calls[0]["db_transaction_open"] is False
    assert db.query(FormPlan).count() == 1
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.expire_all()
    current = db.get(Application, application.id)
    assert current.status == JobStatus.DRAFT
    assert current.prepared_revision is None
    db.close()
