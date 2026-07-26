import json
from datetime import UTC, datetime, timedelta
from profile.models import UserProfile
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from api.routes.applications import (
    _form_plan_review_ready,
    _form_plan_valid,
    _require_review_ready_form_plan,
)
from core.form_plan_persistence import (
    FormPlanPersistenceError,
    persist_inspected_form_plan,
)
from core.form_planning import (
    AnswerPolicyContext,
    AnswerPolicyV1,
    option_set_hash,
    reusable_field_contract_fingerprint,
)
from core.submission_domain import (
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    FieldType,
    FormFieldV1,
    FormPlanV1,
    ReasonCode,
)
from db.models import (
    Application,
    Base,
    FormPlan,
    Job,
    JobStatus,
    OperatorApprovedAnswer,
)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'workday-plan.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _application(db) -> Application:
    job = Job(
        title="Fixture role",
        company="Fixture company",
        source_url="https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-1",
        apply_url="https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-1",
        status=JobStatus.DRAFT,
    )
    app = Application(
        job=job,
        status=JobStatus.DRAFT,
        selected_cv_id="fixture-cv",
        selected_cv_hash="a" * 64,
        profile_version=1,
        material_eligible=True,
        material_model_provider="ollama",
        material_model_name="qwen2.5:7b",
        material_model_digest=f"sha256:{'e' * 64}",
        material_prompt_version="material-package-v1",
    )
    db.add(app)
    db.commit()
    return app


def _plan(app: Application, *, fingerprint: str = "b" * 64) -> FormPlanV1:
    now = datetime.now(UTC)
    return FormPlanV1(
        plan_id=uuid4(),
        application_id=app.id,
        application_revision=app.revision,
        adapter_name="workday",
        adapter_version="2.0.0",
        selector_version="workday-candidate-v2",
        form_fingerprint=fingerprint,
        selected_cv_id=app.selected_cv_id,
        selected_cv_hash=app.selected_cv_hash,
        attached_cv_id=app.selected_cv_id,
        attached_cv_hash=app.selected_cv_hash,
        attachment_verified=True,
        profile_version=app.profile_version,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        fields=(),
        decisions=(),
    )


def _reusable_plan(
    db,
    app: Application,
    *,
    mutate_row=None,
    row_form_fingerprint: str = "b" * 64,
) -> FormPlanV1:
    field = FormFieldV1(
        field_id="phone",
        canonical_name="phone",
        label="Phone",
        field_type=FieldType.PHONE,
        required=True,
        position=0,
    )
    row = OperatorApprovedAnswer(
        canonical_field="phone",
        field_type=field.field_type.value,
        option_set_hash=option_set_hash(field),
        locale="en",
        profile_version=app.profile_version,
        selected_cv_id=app.selected_cv_id,
        selected_cv_hash=app.selected_cv_hash,
        adapter_name="workday",
        adapter_version="2.0.0",
        selector_version="workday-candidate-v2",
        form_fingerprint=row_form_fingerprint,
        field_contract_fingerprint=reusable_field_contract_fingerprint(
            field,
            adapter_name="workday",
            adapter_version="2.0.0",
            selector_version="workday-candidate-v2",
        ),
        policy_version="answer-policy-v1",
        answer_json=json.dumps("+15551234567"),
        evidence_source="operator_confirmation",
        evidence_reference="fixture-review",
        approved_by="fixture-operator",
    )
    db.add(row)
    db.flush()
    if mutate_row is not None:
        mutate_row(row)
    decision = AnswerDecisionV1(
        field_id=field.field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.OPERATOR_APPROVED_REUSABLE,
        value="+15551234567",
        confidence=1.0,
        evidence_refs=(f"operator-approved-answer:{row.id}",),
    )
    base = _plan(app)
    return FormPlanV1.model_validate(
        {
            **base.model_dump(mode="json"),
            "fields": [field.model_dump(mode="json")],
            "decisions": [decision.model_dump(mode="json")],
        }
    )


def test_inspected_plan_is_reviewable_before_preparation(tmp_path) -> None:
    db = _factory(tmp_path)()
    app = _application(db)

    row = persist_inspected_form_plan(db, application=app, plan=_plan(app))
    db.commit()

    assert _form_plan_review_ready(row, app) is True
    assert _form_plan_valid(row, app) is False
    assert row.attachment_verification_source == "candidate_browser_upload_complete"
    assert row.attachment_verified_at is not None
    app.prepared_revision = app.revision
    assert _form_plan_valid(row, app) is True
    db.close()


def test_reinspection_invalidates_previous_immutable_plan(tmp_path) -> None:
    db = _factory(tmp_path)()
    app = _application(db)
    first = persist_inspected_form_plan(
        db,
        application=app,
        plan=_plan(app, fingerprint="b" * 64),
    )
    app.prepared_revision = app.revision
    app.approved_at = datetime.now(UTC).replace(tzinfo=None)
    app.approval_source = "manual_prepare"
    second = persist_inspected_form_plan(
        db,
        application=app,
        plan=_plan(app, fingerprint="c" * 64),
    )
    db.commit()

    assert first.invalidated_at is not None
    assert first.invalidation_reason == "FORM_REINSPECTED"
    assert second.invalidated_at is None
    assert app.prepared_revision is None
    assert app.approved_at is None
    assert app.approval_source is None
    assert _form_plan_valid(second, app) is False
    assert db.query(FormPlan).count() == 2
    db.close()


def test_duplicate_plan_does_not_revoke_its_exact_preparation(tmp_path) -> None:
    db = _factory(tmp_path)()
    app = _application(db)
    observed = _plan(app)
    first = persist_inspected_form_plan(db, application=app, plan=observed)
    db.commit()
    app.prepared_revision = app.revision
    app.approved_at = datetime.now(UTC).replace(tzinfo=None)
    app.approval_source = "manual_prepare"

    duplicate = persist_inspected_form_plan(db, application=app, plan=observed)

    assert duplicate.id == first.id
    assert app.prepared_revision == app.revision
    assert app.approved_at is not None
    assert _form_plan_valid(duplicate, app) is True
    db.close()


def test_persistence_rejects_changed_cv_binding(tmp_path) -> None:
    db = _factory(tmp_path)()
    app = _application(db)
    observed = _plan(app)
    app.selected_cv_hash = "d" * 64

    with pytest.raises(FormPlanPersistenceError, match="FORM_CHANGED"):
        persist_inspected_form_plan(db, application=app, plan=observed)
    db.close()


def test_persistence_accepts_active_exact_reusable_evidence(tmp_path) -> None:
    db = _factory(tmp_path)()
    app = _application(db)
    observed = _reusable_plan(db, app)

    persisted = persist_inspected_form_plan(db, application=app, plan=observed)

    assert persisted.id is not None
    db.close()


def test_persistence_accepts_reusable_evidence_from_matching_partial_step(tmp_path) -> None:
    db = _factory(tmp_path)()
    app = _application(db)
    observed = _reusable_plan(
        db,
        app,
        row_form_fingerprint="d" * 64,
    )

    persisted = persist_inspected_form_plan(db, application=app, plan=observed)

    assert persisted.fingerprint == "b" * 64
    evidence = db.query(OperatorApprovedAnswer).one()
    assert evidence.form_fingerprint == "d" * 64
    assert evidence.field_contract_fingerprint is not None
    db.close()


@pytest.mark.asyncio
async def test_partial_confirm_reusable_then_full_reinspection_persists(tmp_path) -> None:
    db = _factory(tmp_path)()
    app = _application(db)
    phone = FormFieldV1(
        field_id="phone",
        canonical_name="phone",
        label="Phone",
        field_type=FieldType.PHONE,
        required=True,
        position=0,
    )
    partial_base = _plan(app, fingerprint="d" * 64)
    partial = FormPlanV1.model_validate(
        {
            **partial_base.model_dump(mode="json"),
            "fields": [phone.model_dump(mode="json")],
            "decisions": [
                AnswerDecisionV1(
                    field_id=phone.field_id,
                    disposition=AnswerDisposition.OPERATOR_REQUIRED,
                    provenance=AnswerProvenance.ABSTAINED,
                    reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN,
                ).model_dump(mode="json")
            ],
            "blockers": [
                ReasonCode.REQUIRED_FIELD_UNKNOWN.value,
                ReasonCode.FORM_PLAN_INCOMPLETE.value,
            ],
        }
    )
    partial_row = persist_inspected_form_plan(db, application=app, plan=partial)
    db.commit()

    await applications_route.confirm_application_answer(
        app.id,
        phone.field_id,
        applications_route.ConfirmAnswerRequest(
            plan_id=partial_row.plan_id,
            application_revision=1,
            value="+15551234567",
            reusable=True,
            evidence_source="operator_confirmation",
            evidence_reference="partial-step-review",
        ),
        db,
    )

    db.expire_all()
    app = db.get(Application, app.id)
    evidence = db.query(OperatorApprovedAnswer).one()
    policy = await AnswerPolicyV1(db=db).plan_fields(
        (phone,),
        AnswerPolicyContext(
            profile=UserProfile(),
            profile_version=app.profile_version,
            selected_cv_id=app.selected_cv_id,
            selected_cv_hash=app.selected_cv_hash,
            attached_cv_id=app.selected_cv_id,
            attached_cv_hash=app.selected_cv_hash,
            attachment_verified=True,
            adapter_name="workday",
            adapter_version="2.0.0",
            selector_version="workday-candidate-v2",
            form_fingerprint="d" * 64,
            locale="en",
        ),
    )
    assert policy.decisions[0].provenance is AnswerProvenance.OPERATOR_APPROVED_REUSABLE
    assert policy.decisions[0].evidence_refs == (f"operator-approved-answer:{evidence.id}",)
    email = FormFieldV1(
        field_id="email",
        canonical_name="email",
        label="Email",
        field_type=FieldType.EMAIL,
        required=True,
        position=1,
    )
    full_base = _plan(app, fingerprint="e" * 64)
    full = FormPlanV1.model_validate(
        {
            **full_base.model_dump(mode="json"),
            "fields": [
                phone.model_dump(mode="json"),
                email.model_dump(mode="json"),
            ],
            "decisions": [
                policy.decisions[0].model_dump(mode="json"),
                AnswerDecisionV1(
                    field_id=email.field_id,
                    disposition=AnswerDisposition.RESOLVED,
                    provenance=AnswerProvenance.DETERMINISTIC_IDENTITY,
                    value="candidate@example.test",
                    confidence=1.0,
                    evidence_refs=("profile:identity:email",),
                ).model_dump(mode="json"),
            ],
            "blockers": [],
        }
    )

    full_row = persist_inspected_form_plan(db, application=app, plan=full)

    assert evidence.form_fingerprint == "d" * 64
    assert full_row.fingerprint == "e" * 64
    assert full_row.invalidated_at is None
    db.close()


@pytest.mark.parametrize(
    "mutate_row",
    [
        lambda row: (
            setattr(row, "revoked_at", datetime.now(UTC).replace(tzinfo=None)),
            setattr(row, "revoked_by", "fixture-operator"),
            setattr(row, "revocation_reason", "fixture-revocation"),
        ),
        lambda row: setattr(row, "field_contract_fingerprint", "f" * 64),
        lambda row: setattr(row, "policy_version", "older-policy"),
        lambda row: setattr(row, "answer_json", json.dumps("+19999999999")),
    ],
    ids=["revoked", "field-contract", "policy", "answer"],
)
def test_persistence_rejects_revoked_or_mismatched_reusable_evidence(
    tmp_path,
    mutate_row,
) -> None:
    db = _factory(tmp_path)()
    app = _application(db)
    observed = _reusable_plan(db, app, mutate_row=mutate_row)

    with pytest.raises(FormPlanPersistenceError, match="FORM_CHANGED"):
        persist_inspected_form_plan(db, application=app, plan=observed)

    assert db.query(FormPlan).count() == 0
    db.close()


def test_workday_prepare_requires_current_review_ready_plan(tmp_path) -> None:
    db = _factory(tmp_path)()
    app = _application(db)

    with pytest.raises(HTTPException) as exc_info:
        _require_review_ready_form_plan(app)
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "FORM_PLAN_REQUIRED"

    persist_inspected_form_plan(db, application=app, plan=_plan(app))
    db.commit()
    db.refresh(app)
    _require_review_ready_form_plan(app)
    db.close()
