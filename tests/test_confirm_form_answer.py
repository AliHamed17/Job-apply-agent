"""API-level safety coverage for explicit form-answer confirmation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from core.submission_service import reconstruct_persisted_form_plan
from db.models import (
    Application,
    ApplicationEvent,
    Base,
    FormPlan,
    Job,
    JobStatus,
    OperatorApprovedAnswer,
)


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'confirmed-answer.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _reviewed_plan(db):
    now = datetime.now(UTC).replace(tzinfo=None)
    job = Job(
        title="Engineer",
        company="Example",
        source_url="https://example.test/jobs/1",
        apply_url="https://example.test/jobs/1",
        status=JobStatus.DRAFT,
    )
    db.add(job)
    db.flush()
    application = Application(
        job_id=job.id,
        status=JobStatus.DRAFT,
        approved_at=now,
        revision=1,
        prepared_revision=1,
        selected_cv_id="cv-ai",
        selected_cv_hash="c" * 64,
        profile_version=7,
    )
    db.add(application)
    db.flush()
    fields = [
        {
            "field_id": "nationality",
            "canonical_name": "nationality",
            "label": "Nationality",
            "field_type": "text",
            "required": True,
            "position": 0,
            "sensitive_category": "nationality",
        }
    ]
    decisions = [
        {
            "field_id": "nationality",
            "disposition": "operator_required",
            "provenance": "abstained",
            "reason_code": "REQUIRED_FIELD_UNKNOWN",
        }
    ]
    plan = FormPlan(
        plan_id=str(uuid4()),
        application_id=application.id,
        application_revision=1,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        fingerprint="f" * 64,
        selected_cv_id="cv-ai",
        selected_cv_hash="c" * 64,
        attached_cv_id="cv-ai",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
        profile_version=7,
        fields_json=json.dumps(fields),
        decisions_json=json.dumps(decisions),
        blockers_json='["REQUIRED_FIELD_UNKNOWN"]',
        locale="en",
        answer_policy_version="answer-policy-v1",
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(plan)
    db.commit()
    return application, plan


@pytest.mark.asyncio
async def test_confirm_answer_clones_plan_and_requires_reprepare(tmp_path):
    db = _db(tmp_path)
    application, plan = _reviewed_plan(db)
    original_created_at = plan.created_at
    original_session_at = plan.session_verified_at
    original_expires_at = plan.expires_at

    result = await applications_route.confirm_application_answer(
        application.id,
        "nationality",
        applications_route.ConfirmAnswerRequest(
            plan_id=plan.plan_id,
            application_revision=1,
            value="Canadian",
            reusable=True,
            evidence_source="operator_confirmation",
            evidence_reference="review-session-1",
        ),
        db,
    )
    db.expire_all()
    application = db.get(Application, application.id)
    plans = (
        db.query(FormPlan)
        .filter(FormPlan.application_id == application.id)
        .order_by(FormPlan.id)
        .all()
    )
    approved = db.query(OperatorApprovedAnswer).one()
    cloned_domain = reconstruct_persisted_form_plan(plans[-1])

    assert application.revision == 2
    assert application.prepared_revision is None
    assert application.approved_at is None
    assert plans[0].invalidated_at is not None
    assert plans[0].invalidation_reason == "ANSWER_CONFIRMED"
    assert plans[1].created_at == original_created_at
    assert plans[1].session_verified_at == original_session_at
    assert plans[1].expires_at == original_expires_at
    assert cloned_domain.decisions[0].value == "Canadian"
    assert cloned_domain.decisions[0].provenance.value == "operator_approved_reusable"
    assert cloned_domain.blockers == ()
    assert approved.canonical_field == "nationality"
    assert approved.profile_version == 7
    assert approved.selected_cv_hash == "c" * 64
    assert approved.form_fingerprint == "f" * 64
    assert approved.evidence_source == "operator_confirmation"
    assert approved.evidence_reference == "review-session-1"
    assert result.application_revision == 2
    assert result.valid is False
    audit_details = db.query(ApplicationEvent).one().details
    assert "Canadian" not in audit_details
    assert "nationality" not in audit_details
    assert "field_id_hash" in audit_details
    db.close()


@pytest.mark.asyncio
async def test_confirm_answer_locks_authority_before_application(
    tmp_path,
    monkeypatch,
):
    db = _db(tmp_path)
    application, plan = _reviewed_plan(db)
    calls: list[str] = []
    real_application_lock = applications_route._lock_mutation_or_http

    monkeypatch.setattr(
        applications_route,
        "lock_automation_authority_fence",
        lambda _db: calls.append("authority"),
    )

    def tracking_application_lock(*args, **kwargs):
        calls.append("application")
        return real_application_lock(*args, **kwargs)

    monkeypatch.setattr(
        applications_route,
        "_lock_mutation_or_http",
        tracking_application_lock,
    )

    await applications_route.confirm_application_answer(
        application.id,
        "nationality",
        applications_route.ConfirmAnswerRequest(
            plan_id=plan.plan_id,
            application_revision=1,
            value="Canadian",
            reusable=True,
            evidence_source="operator_confirmation",
            evidence_reference="review-session-lock-order",
        ),
        db,
    )

    assert calls[:2] == ["authority", "application"]
    db.close()


@pytest.mark.asyncio
async def test_partial_plan_rejects_one_off_confirmation(tmp_path):
    db = _db(tmp_path)
    application, plan = _reviewed_plan(db)
    plan.blockers_json = '["REQUIRED_FIELD_UNKNOWN","FORM_PLAN_INCOMPLETE"]'
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await applications_route.confirm_application_answer(
            application.id,
            "nationality",
            applications_route.ConfirmAnswerRequest(
                plan_id=plan.plan_id,
                application_revision=1,
                value="Canadian",
                reusable=False,
                evidence_source="operator_confirmation",
                evidence_reference="review-session-partial",
            ),
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "FORM_PLAN_INCOMPLETE"
    db.expire_all()
    assert db.get(Application, application.id).revision == 1
    assert db.query(FormPlan).count() == 1
    assert db.query(OperatorApprovedAnswer).count() == 0
    db.close()


@pytest.mark.asyncio
async def test_partial_plan_reusable_confirmation_preserves_global_blocker(tmp_path):
    db = _db(tmp_path)
    application, plan = _reviewed_plan(db)
    plan.blockers_json = '["REQUIRED_FIELD_UNKNOWN","FORM_PLAN_INCOMPLETE"]'
    db.commit()

    result = await applications_route.confirm_application_answer(
        application.id,
        "nationality",
        applications_route.ConfirmAnswerRequest(
            plan_id=plan.plan_id,
            application_revision=1,
            value="Canadian",
            reusable=True,
            evidence_source="operator_confirmation",
            evidence_reference="review-session-partial",
        ),
        db,
    )

    db.expire_all()
    cloned = db.query(FormPlan).order_by(FormPlan.id.desc()).first()
    cloned_domain = reconstruct_persisted_form_plan(cloned)
    assert cloned_domain.blockers == (applications_route.ReasonCode.FORM_PLAN_INCOMPLETE,)
    assert result.valid is False
    assert db.query(OperatorApprovedAnswer).count() == 1
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field_reason",
    [
        "UNSUPPORTED_CONTROL",
        "LLM_UNAVAILABLE",
        "LLM_MODEL_MISSING",
        "LLM_TIMEOUT",
        "LLM_CIRCUIT_OPEN",
        "LLM_SCHEMA_INVALID",
        "UNSUPPORTED_CLAIM",
    ],
)
async def test_confirm_answer_recomputes_field_level_blockers(
    tmp_path,
    field_reason,
):
    db = _db(tmp_path)
    application, plan = _reviewed_plan(db)
    plan.decisions_json = json.dumps(
        [
            {
                "field_id": "nationality",
                "disposition": "operator_required",
                "provenance": "abstained",
                "reason_code": field_reason,
            }
        ]
    )
    plan.blockers_json = json.dumps(["REQUIRED_FIELD_UNKNOWN", field_reason])
    db.commit()

    await applications_route.confirm_application_answer(
        application.id,
        "nationality",
        applications_route.ConfirmAnswerRequest(
            plan_id=plan.plan_id,
            application_revision=1,
            value="Canadian",
        ),
        db,
    )

    cloned = (
        db.query(FormPlan)
        .filter(FormPlan.application_id == application.id)
        .order_by(FormPlan.id.desc())
        .first()
    )
    assert reconstruct_persisted_form_plan(cloned).blockers == ()
    db.close()


@pytest.mark.asyncio
async def test_confirm_answer_rejects_stale_or_expired_plan_without_mutation(tmp_path):
    db = _db(tmp_path)
    application, plan = _reviewed_plan(db)
    plan.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await applications_route.confirm_application_answer(
            application.id,
            "nationality",
            applications_route.ConfirmAnswerRequest(
                plan_id=plan.plan_id,
                application_revision=1,
                value="Canadian",
            ),
            db,
        )

    assert exc_info.value.status_code == 409
    db.expire_all()
    assert db.get(Application, application.id).revision == 1
    assert db.query(OperatorApprovedAnswer).count() == 0
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_type", "valid", "invalid"),
    [
        ("date", "2026-07-26", "tomorrow-ish"),
        ("email", "candidate@example.test", "not-an-email"),
        ("phone", "+1 (555) 123-4567", "abc"),
        ("url", "https://example.test/profile", "definitely not a url"),
    ],
)
async def test_confirm_answer_enforces_typed_string_semantics(
    tmp_path,
    field_type,
    valid,
    invalid,
):
    db = _db(tmp_path)
    application, plan = _reviewed_plan(db)
    field_id = f"typed-{field_type}"
    plan.fields_json = json.dumps(
        [
            {
                "field_id": field_id,
                "canonical_name": field_type,
                "label": field_type.title(),
                "field_type": field_type,
                "required": True,
                "position": 0,
            }
        ]
    )
    plan.decisions_json = json.dumps(
        [
            {
                "field_id": field_id,
                "disposition": "operator_required",
                "provenance": "abstained",
                "reason_code": "REQUIRED_FIELD_UNKNOWN",
            }
        ]
    )
    db.commit()
    application_id = application.id
    plan_id = plan.plan_id

    with pytest.raises(HTTPException) as exc_info:
        await applications_route.confirm_application_answer(
            application_id,
            field_id,
            applications_route.ConfirmAnswerRequest(
                plan_id=plan_id,
                application_revision=1,
                value=invalid,
            ),
            db,
        )

    assert exc_info.value.status_code == 422
    db.expire_all()
    assert db.get(Application, application_id).revision == 1
    assert db.query(OperatorApprovedAnswer).count() == 0

    result = await applications_route.confirm_application_answer(
        application_id,
        field_id,
        applications_route.ConfirmAnswerRequest(
            plan_id=plan_id,
            application_revision=1,
            value=valid,
        ),
        db,
    )

    assert result.application_revision == 2
    db.close()
