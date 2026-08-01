"""Release-4 authority, cap, quarantine, and exact-command safety tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.automation_policy_service as policy_service
from api.routes import automation as automation_route
from core.automation_policy import (
    AutomationGeography,
    AutoSubmitPolicyV1,
    PolicyAuthoritySource,
    QualifiedFormContractV1,
    canonical_model_bytes,
    sign_auto_submit_policy,
    verify_auto_submit_policy,
)
from core.automation_policy_keys import (
    generate_automation_policy_signing_key,
    load_automation_policy_signing_identity,
)
from core.automation_policy_service import (
    AutomationPolicyError,
    activate_auto_submit_policy,
    confirmed_answer_revision,
    evaluate_auto_submit_policy,
    form_contract_digest,
    policy_usage_status,
    revoke_auto_submit_policy,
    set_automation_kill_switch,
    validate_current_automation_decision,
)
from core.config import Settings
from core.runtime_identity import get_runtime_identity
from core.submission_domain import PreparedFinalActionV1
from db.models import (
    Application,
    ApplicationPolicyDecision,
    AutomationPolicyRevisionRecord,
    AutopilotInspectionRun,
    Base,
    BrowserQualificationRun,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
    UserProfileVersion,
)
from db.session import get_db
from ingestion.url_utils import normalize_url, url_hash
from jobs.models import JobData
from llm.qualification_registry import load_qualified_local_model
from match.job_fit import (
    FitDisposition,
    FitEvidenceV1,
    FitThresholdsV1,
    JobFitDecisionV1,
    job_content_digest,
)
from match.job_fit_store import persist_job_fit_decision
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    QualificationTier,
    adapter_for_url,
)
from worker.autopilot import AutopilotDispatchResult, dispatch_qualified_autopilot
from worker.autopilot_inspection import (
    AutopilotInspectionLeaseLostError,
    _claim_inspection_run,
    _fence_inspection_run,
    _finalize_autopilot_dispatch_result,
    _finish_inspection_run,
    enqueue_qualified_autopilot_inspection,
    execute_qualified_autopilot_inspection,
)
from worker.control_plane_runner import (
    ControlPlaneRunnerError,
    VerifiedKillSwitchCommand,
    activate_control_plane_kill_switch,
)
from worker.submission_commands import (
    _CommitBoundaryRejectedError,
    _enter_commit_boundary,
    _validate_attempt_automation_authority,
    claim_submission_command,
)

_NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)
_CV_HASH = "c" * 64
_ROUTING_DIGEST = "a" * 64
_MANIFEST_DIGEST = "b" * 64
_QUALIFICATION_DIGEST = "d" * 64
_FINGERPRINT = "f" * 64
_MODEL_DIGEST = load_qualified_local_model().digest


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        dry_run=False,
        draft_only=False,
        auto_apply=True,
        portal_final_submit_enabled=True,
        live_automation_acknowledged=True,
        secret_key="release-four-operator-secret-" + "x" * 32,
    )


def _capabilities() -> dict[str, object]:
    identity = get_runtime_identity()
    return {
        "release": {
            "build_sha": identity.build_sha,
            "ui_asset_digest": identity.ui_asset_digest,
            "source_digest": identity.source_digest,
            "release_id": identity.release_id,
            "protocol_version": identity.protocol_version,
            "boot_id": identity.boot_id,
        },
        "automation": {
            "submission_ready": True,
            "stages": {"submission": {"ready": True, "reason_codes": []}},
        },
        "submission": {"allowed": True, "reasons": []},
        "llm": {
            "provider": "ollama",
            "model": "qwen2.5:7b",
            "local": True,
            "digest": _MODEL_DIGEST,
            "ready": True,
            "reason_code": None,
        },
    }


def _evidence() -> tuple[FitEvidenceV1, ...]:
    return tuple(
        FitEvidenceV1(
            factor=factor,
            result="matched",
            points=10,
            maximum_points=10,
        )
        for factor in (
            "role",
            "skills",
            "location",
            "seniority",
            "employment",
            "experience",
            "language_authorization",
        )
    )


def _scenario(tmp_path, monkeypatch) -> SimpleNamespace:
    engine = create_engine(f"sqlite:///{tmp_path / 'autopilot.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    key_path = tmp_path / "automation-policy.pem"
    generate_automation_policy_signing_key(key_path)
    monkeypatch.setenv("AUTOMATION_POLICY_SIGNING_KEY_PATH", str(key_path))
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: True,
    )

    base_descriptor = adapter_for_url("https://boards.greenhouse.io/acme/jobs/123")
    assert base_descriptor is not None
    live_descriptor = replace(
        base_descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(_FINGERPRINT,),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    )
    monkeypatch.setattr(policy_service, "adapter_for_url", lambda _url: live_descriptor)
    monkeypatch.setattr(policy_service, "adapter_for_platform", lambda _name: live_descriptor)

    db = factory()
    job = Job(
        title="AI Engineer",
        company="Acme",
        location="Tel Aviv, Israel",
        employment_type="full-time",
        seniority="senior",
        description="Build Python ML systems",
        requirements="Python",
        keywords=json.dumps(["python"]),
        source_url="https://boards.greenhouse.io/acme/jobs/123",
        apply_url="https://boards.greenhouse.io/acme/jobs/123",
        status=JobStatus.DRAFT,
    )
    db.add(job)
    db.flush()
    job_data = JobData(
        title=job.title,
        company=job.company or "",
        location=job.location or "",
        employment_type=job.employment_type or "",
        seniority=job.seniority or "",
        description=job.description or "",
        requirements=job.requirements or "",
        apply_url=job.apply_url or "",
        source_url=job.source_url,
        keywords=["python"],
    )
    fit = JobFitDecisionV1(
        job_digest=job_content_digest(job_data),
        profile_version=1,
        routing_config_digest=_ROUTING_DIGEST,
        cv_manifest_digest=_MANIFEST_DIGEST,
        selected_cv_id="cv-ai",
        selected_cv_hash=_CV_HASH,
        routing_confidence=0.97,
        routing_margin=0.25,
        fit_score=96,
        disposition=FitDisposition.ELIGIBLE,
        quality_eligible=True,
        evidence=_evidence(),
        thresholds=FitThresholdsV1(),
        qualification_digest=_QUALIFICATION_DIGEST,
    )
    fit_record = persist_job_fit_decision(db, job_id=job.id, decision=fit)
    application = Application(
        job=job,
        status=JobStatus.DRAFT,
        selected_cv_id="cv-ai",
        selected_cv_hash=_CV_HASH,
        profile_version=1,
        cv_routing_confidence=0.97,
        cv_routing_margin=0.25,
        revision=1,
        material_eligible=True,
        material_blockers_json="[]",
        material_claims_json="[]",
        material_model_provider="ollama",
        material_model_name="qwen2.5:7b",
        material_model_digest=_MODEL_DIGEST,
        material_prompt_version="application-materials-v1",
        job_fit_decision_id=fit_record.id,
    )
    db.add(application)
    db.add(
        UserProfileVersion(
            profile_yaml=(
                "personal:\n  name: Evidence Candidate\n  email: candidate@example.test\n"
            ),
            version=1,
        )
    )
    db.flush()
    plan = FormPlan(
        plan_id=str(uuid4()),
        application_id=application.id,
        application_revision=1,
        adapter_name=live_descriptor.platform,
        adapter_version=live_descriptor.adapter_version,
        selector_version=live_descriptor.selector_version,
        fingerprint=_FINGERPRINT,
        selected_cv_id="cv-ai",
        selected_cv_hash=_CV_HASH,
        attached_cv_id="cv-ai",
        attached_cv_hash=_CV_HASH,
        attachment_verified=True,
        attachment_verification_source="browser_upload_receipt",
        attachment_verified_at=_NOW.replace(tzinfo=None),
        profile_version=1,
        fields_json="[]",
        disclosures_json="[]",
        decisions_json="[]",
        blockers_json="[]",
        session_verified_at=_NOW.replace(tzinfo=None),
        created_at=_NOW.replace(tzinfo=None),
        expires_at=(_NOW + timedelta(minutes=30)).replace(tzinfo=None),
    )
    db.add(plan)
    db.flush()
    contract_digest = form_contract_digest(plan)
    scope = QualifiedFormContractV1(
        adapter_name=live_descriptor.platform,
        adapter_version=live_descriptor.adapter_version,
        selector_version=live_descriptor.selector_version,
        form_contract_digest=contract_digest,
    )
    db.add(
        BrowserQualificationRun(
            selector_version=live_descriptor.selector_version,
            terminal_reason="LIVE_CANARY_CONFIRMED",
            qualified=True,
            trace_json="[]",
            adapter_name=live_descriptor.platform,
            adapter_version=live_descriptor.adapter_version,
            qualification_tier="live_canary_qualified",
            form_fingerprint=_FINGERPRINT,
            form_contract_digest=contract_digest,
            fixture_digest="e" * 64,
        )
    )
    answer_revision = confirmed_answer_revision(db, profile_version=1)
    policy = AutoSubmitPolicyV1(
        policy_id=uuid4(),
        revision=1,
        role_families=("cv-ai",),
        geographies=(
            AutomationGeography.ISRAEL,
            AutomationGeography.WORLDWIDE_REMOTE,
        ),
        minimum_fit_score=85,
        daily_limit=25,
        hourly_limit=5,
        company_limit=2,
        permitted_adapters=("greenhouse",),
        qualified_form_contracts=(scope,),
        profile_version=1,
        routing_config_digest=_ROUTING_DIGEST,
        cv_manifest_digest=_MANIFEST_DIGEST,
        fit_qualification_digest=_QUALIFICATION_DIGEST,
        confirmed_answer_revision=answer_revision,
        activated_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
    )
    identity = load_automation_policy_signing_identity(key_path)
    signed = sign_auto_submit_policy(
        policy,
        key_id=identity.key_id,
        private_key=identity.private_key,
    )
    policy_record = AutomationPolicyRevisionRecord(
        policy_id=str(policy.policy_id),
        revision=policy.revision,
        schema_version=policy.schema_version,
        payload_json=canonical_model_bytes(policy).decode("utf-8"),
        payload_digest=policy.payload_digest,
        signing_key_id=str(identity.key_id),
        signature=signed.signature,
        active_slot=1,
        activated_at=_NOW.replace(tzinfo=None),
        expires_at=policy.expires_at.replace(tzinfo=None),
    )
    db.add(policy_record)
    db.commit()
    result = SimpleNamespace(
        engine=engine,
        factory=factory,
        key_path=key_path,
        identity=identity,
        policy=policy,
        policy_record_id=policy_record.id,
        application_id=application.id,
        plan_id=plan.id,
        plan_public_id=plan.plan_id,
        fit_record_id=fit_record.id,
        live_descriptor=live_descriptor,
    )
    db.close()
    return result


def test_dispatch_quarantine_is_persisted_on_the_application(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    queued = enqueue_qualified_autopilot_inspection(
        db,
        application_id=scenario.application_id,
        now=_NOW,
    )
    assert queued.run_id is not None
    claimed = _claim_inspection_run(db, run_id=queued.run_id, now=_NOW)
    assert claimed is not None
    _, claim_token = claimed
    result = _finalize_autopilot_dispatch_result(
        db,
        application_id=scenario.application_id,
        result=AutopilotDispatchResult(
            state="quarantined",
            reason_code="RUNTIME_NOT_READY",
        ),
        inspection_run_id=queued.run_id,
        claim_token=claim_token,
        now=_NOW + timedelta(seconds=1),
    )

    assert result["state"] == "quarantined"
    assert result["reason_code"] == "RUNTIME_NOT_READY"
    db.expire_all()
    application = db.get(Application, scenario.application_id)
    assert application is not None
    assert application.needs_review_reason == "RUNTIME_NOT_READY"
    db.close()


def test_unexpected_inspection_failure_persists_quarantine(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    queued = enqueue_qualified_autopilot_inspection(
        db,
        application_id=scenario.application_id,
        now=_NOW,
    )
    assert queued.run_id is not None
    db.close()

    monkeypatch.setattr(
        "worker.autopilot_inspection.get_session_factory",
        lambda: scenario.factory,
    )

    def fail_inspection(*_args, **_kwargs):
        raise RuntimeError("unexpected private browser failure")

    monkeypatch.setattr(
        "worker.autopilot_inspection.inspect_and_dispatch_qualified_autopilot",
        fail_inspection,
    )
    monkeypatch.setattr(
        "worker.autopilot_inspection._utc_now",
        lambda: _NOW + timedelta(seconds=2),
    )
    result = execute_qualified_autopilot_inspection(
        queued.run_id,
        now=_NOW + timedelta(seconds=1),
    )

    assert result["state"] == "quarantined"
    assert result["reason_code"] == "FORM_INSPECTION_FAILED"
    assert result["lease_finished"] is True
    verify = scenario.factory()
    application = verify.get(Application, scenario.application_id)
    run = verify.get(AutopilotInspectionRun, queued.run_id)
    assert application.needs_review_reason == "FORM_INSPECTION_FAILED"
    assert run.state == "finished"
    assert run.reason_code == "FORM_INSPECTION_FAILED"
    verify.close()


def test_unexpected_inspection_failure_after_lease_expiry_has_no_side_effect(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    queued = enqueue_qualified_autopilot_inspection(
        db,
        application_id=scenario.application_id,
        now=_NOW,
    )
    assert queued.run_id is not None
    db.close()

    monkeypatch.setattr(
        "worker.autopilot_inspection.get_session_factory",
        lambda: scenario.factory,
    )

    def fail_after_lease_expiry(*_args, **_kwargs):
        raise RuntimeError("late failure")

    monkeypatch.setattr(
        "worker.autopilot_inspection.inspect_and_dispatch_qualified_autopilot",
        fail_after_lease_expiry,
    )
    monkeypatch.setattr(
        "worker.autopilot_inspection._utc_now",
        lambda: _NOW + timedelta(minutes=16),
    )

    result = execute_qualified_autopilot_inspection(queued.run_id, now=_NOW)

    assert result == {
        "state": "not_claimed",
        "reason_code": "AUTOPILOT_INSPECTION_LEASE_LOST",
        "run_id": queued.run_id,
        "lease_finished": False,
    }
    verify = scenario.factory()
    application = verify.get(Application, scenario.application_id)
    run = verify.get(AutopilotInspectionRun, queued.run_id)
    assert application.needs_review_reason is None
    assert run.state == "running"
    assert run.reason_code is None
    verify.close()


def test_policy_contract_is_signed_frozen_and_strictly_bounded(tmp_path) -> None:
    key_path = tmp_path / "key.pem"
    generate_automation_policy_signing_key(key_path)
    identity = load_automation_policy_signing_identity(key_path)
    policy = AutoSubmitPolicyV1(
        policy_id=uuid4(),
        revision=1,
        role_families=("ai",),
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
        routing_config_digest="a" * 64,
        cv_manifest_digest="b" * 64,
        fit_qualification_digest="c" * 64,
        confirmed_answer_revision="d" * 64,
        activated_at=_NOW,
        expires_at=_NOW + timedelta(days=30),
    )
    signed = sign_auto_submit_policy(
        policy,
        key_id=identity.key_id,
        private_key=identity.private_key,
    )
    verify_auto_submit_policy(signed, public_key=identity.public_key)
    with pytest.raises(ValidationError):
        AutoSubmitPolicyV1.model_validate({**policy.model_dump(), "daily_limit": 26})
    with pytest.raises(ValidationError):
        AutoSubmitPolicyV1.model_validate(
            {**policy.model_dump(), "minimum_fit_score": float("nan")}
        )
    with pytest.raises(ValidationError):
        AutoSubmitPolicyV1.model_validate({**policy.model_dump(), "unexpected_authority": True})


def test_legacy_auto_apply_environment_flag_never_creates_authority(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'no-authority.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setenv("AUTO_APPLY", "true")
    db = factory()
    try:
        status = policy_usage_status(db, now=_NOW)
        assert status == {
            "active": False,
            "reason_code": "AUTOMATION_POLICY_NOT_ACTIVE",
            "kill_switch_active": False,
        }
    finally:
        db.close()


def test_local_policy_api_requires_auth_and_exact_acknowledgements(tmp_path, monkeypatch) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'policy-api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    key_path = tmp_path / "api-policy-key.pem"
    generate_automation_policy_signing_key(key_path)
    monkeypatch.setenv("AUTOMATION_POLICY_SIGNING_KEY_PATH", str(key_path))
    settings = _settings()
    monkeypatch.setattr(automation_route, "get_settings", lambda: settings)
    monkeypatch.setattr(
        policy_service,
        "_private_bindings",
        lambda *_args, **_kwargs: {
            "profile_version": 1,
            "role_families": ("cv-ai",),
            "routing_config_digest": _ROUTING_DIGEST,
            "cv_manifest_digest": _MANIFEST_DIGEST,
            "fit_qualification_digest": _QUALIFICATION_DIGEST,
            "confirmed_answer_revision": "e" * 64,
        },
    )
    monkeypatch.setattr(policy_service, "_scope_has_live_canary", lambda *_args: True)
    base_descriptor = adapter_for_url("https://boards.greenhouse.io/acme/jobs/123")
    assert base_descriptor is not None
    scope = QualifiedFormContractV1(
        adapter_name="greenhouse",
        adapter_version=base_descriptor.adapter_version,
        selector_version=base_descriptor.selector_version,
        form_contract_digest="f" * 64,
    )
    monkeypatch.setattr(policy_service, "adapter_for_platform", lambda _name: base_descriptor)
    policy_db = factory()
    with pytest.raises(AutomationPolicyError, match="AUTOMATION_POLICY_ADAPTER_NOT_QUALIFIED"):
        activate_auto_submit_policy(
            policy_db,
            settings=settings,
            role_families=("cv-ai",),
            geographies=(AutomationGeography.ISRAEL,),
            permitted_adapters=("greenhouse",),
            qualified_form_contracts=(scope,),
            now=_NOW,
        )
    policy_db.rollback()
    policy_db.close()
    descriptor = replace(
        base_descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=("f" * 64,),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    )
    monkeypatch.setattr(policy_service, "adapter_for_platform", lambda _name: descriptor)

    app = FastAPI()
    app.include_router(automation_route.router, prefix="/api")

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    payload = {
        "acknowledgement": "ACTIVATE_QUALIFIED_AUTOPILOT",
        "role_families": ["cv-ai"],
        "geographies": ["israel"],
        "permitted_adapters": ["greenhouse"],
        "qualified_form_contracts": [
            {
                "adapter_name": "greenhouse",
                "adapter_version": descriptor.adapter_version,
                "selector_version": descriptor.selector_version,
                "form_contract_digest": "f" * 64,
            }
        ],
    }
    missing = client.post("/api/automation/policy/activate", json=payload)
    assert missing.status_code == 401
    wrong = client.post(
        "/api/automation/policy/activate",
        json=payload,
        headers={"Authorization": "Bearer wrong"},
    )
    assert wrong.status_code == 403
    invalid_ack = client.post(
        "/api/automation/policy/activate",
        json={**payload, "acknowledgement": "ENABLE"},
        headers={"Authorization": f"Bearer {settings.secret_key}"},
    )
    assert invalid_ack.status_code == 422
    activated = client.post(
        "/api/automation/policy/activate",
        json=payload,
        headers={"Authorization": f"Bearer {settings.secret_key}"},
    )
    assert activated.status_code == 201
    assert activated.json()["active"] is True
    revoked = client.post(
        "/api/automation/policy/revoke",
        json={
            "acknowledgement": "REVOKE_QUALIFIED_AUTOPILOT",
            "reason_code": "OPERATOR_REVOKED",
        },
        headers={"Authorization": f"Bearer {settings.secret_key}"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["active"] is False
    assert revoked.json()["reason_code"] == "AUTOMATION_POLICY_NOT_ACTIVE"


def test_exact_qualified_application_receives_a_short_lived_decision(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    try:
        record = evaluate_auto_submit_policy(
            db,
            application_id=scenario.application_id,
            form_plan_id=scenario.plan_id,
            now=_NOW,
        )
        assert record.allowed is True
        assert record.reason_codes_json == "[]"
        assert record.authority_expires_at == (_NOW + timedelta(minutes=5)).replace(tzinfo=None)
        decision = validate_current_automation_decision(
            db,
            decision_record=record,
            now=_NOW + timedelta(minutes=1),
        )
        assert decision.allowed is True
        assert decision.selected_cv_hash == _CV_HASH
        assert db.get(Application, scenario.application_id).prepared_revision == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ("attachment", "ATTACHMENT_UNVERIFIED"),
        ("required_field", "FORM_PLAN_BLOCKED"),
        ("form_revision", "FORM_CHANGED"),
    ],
)
def test_uncertain_or_changed_form_is_quarantined(
    tmp_path,
    monkeypatch,
    mutation: str,
    reason_code: str,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    plan = db.get(FormPlan, scenario.plan_id)
    application = db.get(Application, scenario.application_id)
    if mutation == "attachment":
        plan.attachment_verified = False
        plan.attachment_verification_source = None
        plan.attachment_verified_at = None
    elif mutation == "required_field":
        plan.blockers_json = '["REQUIRED_FIELD_UNKNOWN"]'
    else:
        application.revision = 2
    db.commit()
    record = evaluate_auto_submit_policy(
        db,
        application_id=scenario.application_id,
        form_plan_id=scenario.plan_id,
        now=_NOW,
    )
    assert record.allowed is False
    assert reason_code in json.loads(record.reason_codes_json)
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.close()


def test_kill_switch_and_revocation_each_fail_closed(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    set_automation_kill_switch(
        db,
        active=True,
        source=PolicyAuthoritySource.LOCAL_OPERATOR,
        reason_code="OPERATOR_STOP",
        now=_NOW,
    )
    db.commit()
    denied = evaluate_auto_submit_policy(
        db,
        application_id=scenario.application_id,
        form_plan_id=scenario.plan_id,
        now=_NOW,
    )
    assert json.loads(denied.reason_codes_json)[0] == "KILL_SWITCH_ACTIVE"
    db.rollback()
    set_automation_kill_switch(
        db,
        active=False,
        source=PolicyAuthoritySource.LOCAL_OPERATOR,
        reason_code="OPERATOR_RESUME",
        now=_NOW + timedelta(seconds=1),
    )
    revoke_auto_submit_policy(db, now=_NOW + timedelta(seconds=2))
    db.commit()
    with pytest.raises(AutomationPolicyError, match="AUTOMATION_POLICY_NOT_ACTIVE"):
        evaluate_auto_submit_policy(
            db,
            application_id=scenario.application_id,
            form_plan_id=scenario.plan_id,
            now=_NOW + timedelta(seconds=3),
        )
    db.close()


def test_policy_expiry_and_signature_tampering_each_fail_closed(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    expired = policy_usage_status(
        db,
        now=scenario.policy.expires_at + timedelta(seconds=1),
    )
    assert expired["active"] is False
    assert expired["reason_code"] == "AUTOMATION_POLICY_EXPIRED"
    with pytest.raises(AutomationPolicyError, match="AUTOMATION_POLICY_NOT_ACTIVE"):
        evaluate_auto_submit_policy(
            db,
            application_id=scenario.application_id,
            form_plan_id=scenario.plan_id,
            now=scenario.policy.expires_at + timedelta(seconds=1),
        )

    record = db.get(AutomationPolicyRevisionRecord, scenario.policy_record_id)
    replacement = "A" if not record.signature.startswith("A") else "B"
    record.signature = replacement + record.signature[1:]
    db.commit()
    tampered = policy_usage_status(db, now=_NOW)
    assert tampered["active"] is False
    assert tampered["reason_code"] == "AUTOMATION_POLICY_SIGNATURE_INVALID"
    with pytest.raises(AutomationPolicyError, match="AUTOMATION_POLICY_SIGNATURE_INVALID"):
        evaluate_auto_submit_policy(
            db,
            application_id=scenario.application_id,
            form_plan_id=scenario.plan_id,
            now=_NOW,
        )
    db.close()


@pytest.mark.parametrize(
    ("counts", "reason_code"),
    [
        ((25, 0, 0), "AUTOMATION_DAILY_LIMIT_REACHED"),
        ((0, 5, 0), "AUTOMATION_HOURLY_LIMIT_REACHED"),
        ((0, 0, 2), "AUTOMATION_COMPANY_LIMIT_REACHED"),
    ],
)
def test_policy_limits_reserve_before_command_creation(
    tmp_path,
    monkeypatch,
    counts: tuple[int, int, int],
    reason_code: str,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(policy_service, "_usage_counts", lambda *_args, **_kwargs: counts)
    db = scenario.factory()
    record = evaluate_auto_submit_policy(
        db,
        application_id=scenario.application_id,
        form_plan_id=scenario.plan_id,
        now=_NOW,
    )
    assert record.allowed is False
    assert reason_code in json.loads(record.reason_codes_json)
    assert db.query(SubmissionCommand).count() == 0
    db.close()


def test_usage_limits_span_superseded_policy_revisions(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    reserved = evaluate_auto_submit_policy(
        db,
        application_id=scenario.application_id,
        form_plan_id=scenario.plan_id,
        now=_NOW,
    )
    assert reserved.allowed is True

    superseded_at = _NOW + timedelta(minutes=1)
    old_record = db.get(AutomationPolicyRevisionRecord, scenario.policy_record_id)
    old_record.active_slot = None
    old_record.revoked_at = superseded_at.replace(tzinfo=None)
    old_record.revoked_by = "local_operator"
    old_record.revocation_reason = "AUTOMATION_POLICY_SUPERSEDED"
    next_policy = scenario.policy.model_copy(
        update={
            "policy_id": uuid4(),
            "revision": 2,
            "daily_limit": 1,
            "hourly_limit": 1,
            "activated_at": superseded_at,
            "expires_at": superseded_at + timedelta(days=30),
        }
    )
    signed = sign_auto_submit_policy(
        next_policy,
        key_id=scenario.identity.key_id,
        private_key=scenario.identity.private_key,
    )
    db.add(
        AutomationPolicyRevisionRecord(
            policy_id=str(next_policy.policy_id),
            revision=next_policy.revision,
            schema_version=next_policy.schema_version,
            payload_json=canonical_model_bytes(next_policy).decode("utf-8"),
            payload_digest=next_policy.payload_digest,
            signing_key_id=str(scenario.identity.key_id),
            signature=signed.signature,
            active_slot=1,
            activated_at=next_policy.activated_at.replace(tzinfo=None),
            expires_at=next_policy.expires_at.replace(tzinfo=None),
        )
    )
    db.commit()

    status = policy_usage_status(db, now=superseded_at + timedelta(minutes=1))
    assert status["revision"] == 2
    assert status["daily_used"] == 1
    assert status["hourly_used"] == 1
    assert status["daily_remaining"] == 0
    assert status["hourly_remaining"] == 0
    db.close()


def test_policy_decision_attempt_permit_and_command_share_one_exact_binding(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "worker.submission_commands.execute_submission_command_task.delay",
        lambda *_args, **_kwargs: None,
    )
    db = scenario.factory()
    first = dispatch_qualified_autopilot(
        db,
        application_id=scenario.application_id,
        form_plan_id=scenario.plan_id,
        settings=_settings(),
        capabilities=_capabilities(),
        descriptor_resolver=lambda _url: scenario.live_descriptor,
        session_checker=lambda *_args: True,
        now=_NOW,
    )
    assert first.state == "queued", first
    second = dispatch_qualified_autopilot(
        db,
        application_id=scenario.application_id,
        form_plan_id=scenario.plan_id,
        settings=_settings(),
        capabilities=_capabilities(),
        descriptor_resolver=lambda _url: scenario.live_descriptor,
        session_checker=lambda *_args: True,
        now=_NOW + timedelta(seconds=1),
    )
    assert second.replayed is True
    assert second.attempt_id == first.attempt_id
    attempt = db.get(Submission, first.attempt_id)
    assert attempt.authority_kind == "qualified_autopilot"
    assert attempt.automation_policy_decision_id == first.policy_decision_id
    assert (
        attempt.automation_policy_decision_digest
        == attempt.final_submit_permit.automation_policy_decision_digest
    )
    assert attempt.final_submit_permit.authority_kind == "qualified_autopilot"
    assert db.query(ApplicationPolicyDecision).filter_by(allowed=True).count() == 1
    assert db.query(Submission).count() == 1
    assert db.query(SubmissionCommand).count() == 1
    db.close()


@pytest.mark.parametrize(
    ("stop_kind", "expected_reason"),
    [
        ("kill", "GOVERNOR_DENIED"),
        ("revoke", "PERMIT_BINDING_MISMATCH"),
    ],
)
def test_kill_or_revocation_before_commit_never_consumes_final_action(
    tmp_path,
    monkeypatch,
    stop_kind: str,
    expected_reason: str,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "worker.submission_commands.execute_submission_command_task.delay",
        lambda *_args, **_kwargs: None,
    )
    db = scenario.factory()
    queued = dispatch_qualified_autopilot(
        db,
        application_id=scenario.application_id,
        form_plan_id=scenario.plan_id,
        settings=_settings(),
        capabilities=_capabilities(),
        descriptor_resolver=lambda _url: scenario.live_descriptor,
        session_checker=lambda *_args: True,
        now=_NOW,
    )
    assert queued.command_id is not None
    assert claim_submission_command(db, command_id=queued.command_id, now=_NOW) == queued.command_id
    command = db.get(SubmissionCommand, queued.command_id)
    attempt = command.attempt
    attempt.stage = "ready"
    attempt.stage_started_at = _NOW.replace(tzinfo=None)
    db.commit()
    if stop_kind == "kill":
        set_automation_kill_switch(
            db,
            active=True,
            source=PolicyAuthoritySource.LOCAL_OPERATOR,
            reason_code="OPERATOR_STOP",
            now=_NOW + timedelta(seconds=1),
        )
    else:
        revoke_auto_submit_policy(db, now=_NOW + timedelta(seconds=1))
    db.commit()
    permit = attempt.final_submit_permit
    action = PreparedFinalActionV1(
        attempt_id=attempt.id,
        adapter_name=attempt.adapter_name,
        adapter_version=attempt.adapter_version,
        selector_version=attempt.selector_version,
        form_fingerprint=attempt.form_plan_fingerprint,
        attached_cv_hash=attempt.attached_cv_hash,
        prepared_at=_NOW + timedelta(seconds=1),
        expires_at=permit.expires_at.replace(tzinfo=UTC),
        action_nonce="9" * 64,
    )
    job_url = attempt.application.job.apply_url or attempt.application.job.source_url
    with pytest.raises(_CommitBoundaryRejectedError, match=expected_reason):
        _enter_commit_boundary(
            db,
            command_id=command.id,
            expected_claim_token=command.claim_token,
            job_url_hash=url_hash(normalize_url(job_url)),
            action=action,
            now=_NOW + timedelta(seconds=2),
        )
    db.expire_all()
    stopped_attempt = db.get(Submission, queued.attempt_id)
    assert stopped_attempt.stage == "finished"
    assert stopped_attempt.outcome == "failed_before_commit"
    assert stopped_attempt.reason_code == expected_reason
    assert stopped_attempt.final_action_at is None
    assert stopped_attempt.final_submit_permit.consumed_at is None
    db.close()


def test_reversible_inspection_queue_is_exact_and_replay_safe(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    first = enqueue_qualified_autopilot_inspection(
        db,
        application_id=scenario.application_id,
        now=_NOW,
    )
    second = enqueue_qualified_autopilot_inspection(
        db,
        application_id=scenario.application_id,
        now=_NOW,
    )
    assert first.state == "queued"
    assert second.run_id == first.run_id
    assert second.replayed is True
    assert db.query(AutopilotInspectionRun).count() == 1
    assert db.query(Submission).count() == 0
    db.close()


def test_stale_inspection_lease_is_reclaimed_without_accepting_old_completion(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    queued = enqueue_qualified_autopilot_inspection(
        db,
        application_id=scenario.application_id,
        now=_NOW,
    )
    first_claim = _claim_inspection_run(db, run_id=queued.run_id, now=_NOW)
    assert first_claim is not None
    first_application_id, first_token = first_claim
    assert first_application_id == scenario.application_id
    assert (
        _claim_inspection_run(
            db,
            run_id=queued.run_id,
            now=_NOW + timedelta(minutes=14),
        )
        is None
    )

    second_claim = _claim_inspection_run(
        db,
        run_id=queued.run_id,
        now=_NOW + timedelta(minutes=16),
    )
    assert second_claim is not None
    second_application_id, second_token = second_claim
    assert second_application_id == scenario.application_id
    assert second_token != first_token
    with pytest.raises(
        AutopilotInspectionLeaseLostError,
        match="AUTOPILOT_INSPECTION_LEASE_LOST",
    ):
        _fence_inspection_run(
            db,
            run_id=queued.run_id,
            application_id=scenario.application_id,
            claim_token=first_token,
            now=_NOW + timedelta(minutes=16, seconds=1),
        )
    assert db.query(SubmissionCommand).count() == 0
    assert (
        _finish_inspection_run(
            db,
            run_id=queued.run_id,
            claim_token=first_token,
            reason_code="COMMAND_QUEUED",
            now=_NOW + timedelta(minutes=16, seconds=1),
        )
        is False
    )
    assert (
        _finish_inspection_run(
            db,
            run_id=queued.run_id,
            claim_token=second_token,
            reason_code="COMMAND_QUEUED",
            now=_NOW + timedelta(minutes=16, seconds=2),
        )
        is True
    )
    row = db.get(AutopilotInspectionRun, queued.run_id)
    assert row.state == "finished"
    assert row.reason_code == "COMMAND_QUEUED"
    db.close()


def test_transient_claim_denial_remains_retryable(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    queued = enqueue_qualified_autopilot_inspection(
        db,
        application_id=scenario.application_id,
        now=_NOW,
    )
    assert queued.run_id is not None
    set_automation_kill_switch(
        db,
        active=True,
        source=PolicyAuthoritySource.LOCAL_OPERATOR,
        reason_code="OPERATOR_STOP",
        now=_NOW,
    )
    db.commit()

    assert _claim_inspection_run(db, run_id=queued.run_id, now=_NOW) is None
    row = db.get(AutopilotInspectionRun, queued.run_id)
    assert row.state == "queued"
    assert row.reason_code is None
    assert row.finished_at is None

    set_automation_kill_switch(
        db,
        active=False,
        source=PolicyAuthoritySource.LOCAL_OPERATOR,
        reason_code="OPERATOR_RESUME",
        now=_NOW + timedelta(seconds=1),
    )
    db.commit()
    claimed = _claim_inspection_run(
        db,
        run_id=queued.run_id,
        now=_NOW + timedelta(seconds=2),
    )
    assert claimed is not None
    assert claimed[0] == scenario.application_id
    db.close()


def test_commit_authority_validation_uses_locked_governor_fence(monkeypatch) -> None:
    observed: list[bool] = []
    decision = SimpleNamespace(decision_digest="a" * 64)
    attempt = SimpleNamespace(
        authority_kind="qualified_autopilot",
        automation_policy_decision=decision,
        automation_policy_decision_digest=decision.decision_digest,
    )

    def validate(*_args, lock: bool, **_kwargs):
        observed.append(lock)

    monkeypatch.setattr(policy_service, "validate_current_automation_decision", validate)
    assert _validate_attempt_automation_authority(object(), attempt, now=_NOW) == (None, None)
    assert observed == [True]


def test_commit_boundary_acquires_authority_fence_before_claim_context(monkeypatch) -> None:
    events: list[str] = []

    monkeypatch.setattr(
        "worker.submission_commands.lock_automation_authority_fence",
        lambda _db: events.append("authority_fence"),
    )

    def no_context(*_args, **_kwargs):
        events.append("claim_context")
        return None

    monkeypatch.setattr("worker.submission_commands._lock_claimed_context", no_context)
    result = _enter_commit_boundary(
        object(),
        command_id=1,
        expected_claim_token="claim-token",
        job_url_hash="a" * 64,
        action=SimpleNamespace(),
        now=_NOW,
    )

    assert result is None
    assert events == ["authority_fence", "claim_context"]


def test_signed_remote_kill_is_replay_protected_and_can_never_clear(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'remote-kill.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    command = VerifiedKillSwitchCommand(
        command_id=str(uuid4()),
        runner_boot_id=str(uuid4()),
        delivery_nonce=str(uuid4()),
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
        envelope_digest="9" * 64,
    )
    db = factory()
    assert activate_control_plane_kill_switch(db, command, now=_NOW) is False
    assert activate_control_plane_kill_switch(db, command, now=_NOW) is True
    with pytest.raises(AutomationPolicyError, match="REMOTE_KILL_CAN_ONLY_ACTIVATE"):
        set_automation_kill_switch(
            db,
            active=False,
            source=PolicyAuthoritySource.VERCEL_SIGNED_KILL,
            reason_code="REMOTE_OPERATOR_KILL",
            command_digest="8" * 64,
            now=_NOW,
        )
    with pytest.raises(ControlPlaneRunnerError, match="CONTROL_COMMAND_EXPIRED"):
        activate_control_plane_kill_switch(
            db,
            replace(command, envelope_digest="7" * 64),
            now=_NOW + timedelta(minutes=6),
        )
    db.close()
