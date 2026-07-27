"""Persistence and preparation gates for immutable Greenhouse form plans."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.applications import (
    _form_plan_review_ready,
    _form_plan_valid,
    _require_review_ready_form_plan,
)
from core.form_plan_persistence import (
    FormPlanPersistenceError,
    persist_inspected_form_plan,
)
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
    ReasonCode,
)
from db.models import Application, Base, FormPlan, Job, JobStatus

GREENHOUSE_URL = "https://boards.greenhouse.io/fixture/jobs/1001"


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'greenhouse-form-plan.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _application(db) -> Application:
    job = Job(
        title="Fixture role",
        company="Fixture company",
        source_url=GREENHOUSE_URL,
        apply_url=GREENHOUSE_URL,
        status=JobStatus.DRAFT,
    )
    application = Application(
        job=job,
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
    return application


def _plan(
    application: Application,
    *,
    fingerprint: str = "b" * 64,
    blockers: tuple[ReasonCode, ...] = (),
) -> FormPlanV1:
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
    fields = [resume]
    decisions = [
        AnswerDecisionV1(
            field_id=resume.field_id,
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
            value=VERIFIED_ATTACHMENT_SENTINEL,
            confidence=1.0,
            evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
        )
    ]
    if ReasonCode.REQUIRED_FIELD_UNKNOWN in blockers:
        unknown = FormFieldV1(
            field_id="custom_required",
            canonical_name=None,
            label="Fixture required question",
            field_type=FieldType.TEXT,
            required=True,
            position=1,
        )
        fields.append(unknown)
        decisions.append(
            AnswerDecisionV1(
                field_id=unknown.field_id,
                disposition=AnswerDisposition.OPERATOR_REQUIRED,
                provenance=AnswerProvenance.ABSTAINED,
                reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN,
            )
        )
    return FormPlanV1(
        plan_id=uuid4(),
        application_id=application.id,
        application_revision=application.revision,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-candidate-v9",
        form_fingerprint=fingerprint,
        selected_cv_id=application.selected_cv_id,
        selected_cv_hash=application.selected_cv_hash,
        attached_cv_id=application.selected_cv_id,
        attached_cv_hash=application.selected_cv_hash,
        attachment_verified=True,
        profile_version=application.profile_version,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        fields=tuple(fields),
        decisions=tuple(decisions),
        blockers=blockers,
    )


def test_inspected_greenhouse_plan_is_reviewable_but_not_prepared(tmp_path) -> None:
    db = _factory(tmp_path)()
    application = _application(db)

    row = persist_inspected_form_plan(
        db,
        application=application,
        plan=_plan(application),
    )
    db.commit()

    assert _form_plan_review_ready(row, application) is True
    assert _form_plan_valid(row, application) is False
    assert row.attachment_verification_source == "candidate_browser_upload_complete"
    assert row.attachment_verified_at is not None
    application.prepared_revision = application.revision
    assert _form_plan_valid(row, application) is True
    db.close()


def test_reinspection_invalidates_previous_plan_and_preparation(tmp_path) -> None:
    db = _factory(tmp_path)()
    application = _application(db)
    first = persist_inspected_form_plan(
        db,
        application=application,
        plan=_plan(application, fingerprint="b" * 64),
    )
    application.prepared_revision = application.revision
    application.approved_at = datetime.now(UTC).replace(tzinfo=None)
    application.approval_source = "manual_prepare"

    second = persist_inspected_form_plan(
        db,
        application=application,
        plan=_plan(application, fingerprint="c" * 64),
    )
    db.commit()

    assert first.invalidated_at is not None
    assert first.invalidation_reason == "FORM_REINSPECTED"
    assert second.invalidated_at is None
    assert application.prepared_revision is None
    assert application.approved_at is None
    assert application.approval_source is None
    assert _form_plan_valid(second, application) is False
    assert db.query(FormPlan).count() == 2
    db.close()


def test_persistence_rejects_changed_selected_cv_binding(tmp_path) -> None:
    db = _factory(tmp_path)()
    application = _application(db)
    observed = _plan(application)
    application.selected_cv_hash = "d" * 64

    with pytest.raises(FormPlanPersistenceError, match="FORM_CHANGED"):
        persist_inspected_form_plan(
            db,
            application=application,
            plan=observed,
        )

    assert db.query(FormPlan).count() == 0
    db.close()


def test_partial_plan_persists_for_review_but_cannot_prepare(tmp_path) -> None:
    db = _factory(tmp_path)()
    application = _application(db)
    partial = _plan(
        application,
        blockers=(
            ReasonCode.REQUIRED_FIELD_UNKNOWN,
            ReasonCode.FORM_PLAN_INCOMPLETE,
        ),
    )

    row = persist_inspected_form_plan(
        db,
        application=application,
        plan=partial,
    )
    db.commit()
    db.refresh(application)

    assert partial.ready_for_permit is False
    assert _form_plan_review_ready(row, application) is False
    with pytest.raises(HTTPException) as exc_info:
        _require_review_ready_form_plan(application)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "FORM_PLAN_REQUIRED"
    db.close()


def test_greenhouse_prepare_requires_current_review_ready_plan(tmp_path) -> None:
    db = _factory(tmp_path)()
    application = _application(db)

    with pytest.raises(HTTPException) as exc_info:
        _require_review_ready_form_plan(application)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "FORM_PLAN_REQUIRED"

    persist_inspected_form_plan(
        db,
        application=application,
        plan=_plan(application),
    )
    db.commit()
    db.refresh(application)
    _require_review_ready_form_plan(application)
    db.close()
