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
from core.adapter_qualification_service import fixture_evidence_digest
from core.automation_artifact_snapshot import policy_artifact_snapshot_id
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
    validate_automation_inspection_candidate,
    validate_current_automation_decision,
)
from core.config import Settings
from core.runtime_identity import get_runtime_identity
from core.submission_domain import (
    ConfirmedSubmittedOutcome,
    EvidenceType,
    PreparedFinalActionV1,
)
from core.submission_domain import (
    SubmissionEvidence as DomainSubmissionEvidence,
)
from core.submission_service import SubmissionAdmissionError
from db.models import (
    AdapterQualificationRecord,
    Application,
    ApplicationPolicyDecision,
    AutomationPolicyRevisionRecord,
    AutopilotInspectionRun,
    Base,
    BrowserQualificationRun,
    FormPlan,
    Job,
    JobStatus,
    OperatorApprovedAnswer,
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
    execute_claimed_submission_command,
)

_NOW = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _freeze_submission_service_wall_clock(monkeypatch) -> None:
    # Production still reads the real clock; only these fixed-time scenarios are frozen.
    monkeypatch.setattr("core.submission_service._utc_now", lambda: _NOW)


_CV_HASH = "c" * 64
_ROUTING_DIGEST = "a" * 64
_MANIFEST_DIGEST = "b" * 64
_QUALIFICATION_DIGEST = "d" * 64
_FINGERPRINT = "f" * 64
_MODEL_DIGEST = load_qualified_local_model().digest


def _artifact_bindings(**overrides: object) -> dict[str, object]:
    bindings: dict[str, object] = {
        "role_families": ("cv-ai",),
        "routing_config_digest": _ROUTING_DIGEST,
        "cv_manifest_digest": _MANIFEST_DIGEST,
        "fit_qualification_digest": _QUALIFICATION_DIGEST,
    }
    bindings.update(overrides)
    return bindings


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
        policy_service,
        "_current_artifact_bindings",
        lambda *_args, **_kwargs: _artifact_bindings(),
    )
    monkeypatch.setattr(
        policy_service,
        "require_policy_artifact_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(snapshot_id="8" * 64),
    )
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
    db.add(
        AdapterQualificationRecord(
            qualification_tier="live_canary_qualified",
            adapter_name=live_descriptor.platform,
            adapter_version=live_descriptor.adapter_version,
            selector_version=live_descriptor.selector_version,
            execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
            form_fingerprint=_FINGERPRINT,
            form_contract_digest=contract_digest,
            fixture_digest=fixture_evidence_digest(live_descriptor.platform),
            application_id=application.id,
            application_revision=application.revision,
            form_plan_id=plan.id,
            attempt_id=900_001,
            job_url_hash=url_hash(normalize_url(job.apply_url)),
            evidence_digest="9" * 64,
            runner_release=get_runtime_identity().release_id,
            qualified_at=_NOW.replace(tzinfo=None),
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


@pytest.mark.parametrize(
    "reason_code",
    [
        "KILL_SWITCH_ACTIVE",
        "OUTSIDE_ACTIVE_HOURS",
        "AUTOMATION_DAILY_LIMIT_REACHED",
        "AUTOMATION_HOURLY_LIMIT_REACHED",
        "AUTOMATION_COMPANY_LIMIT_REACHED",
    ],
)
def test_post_inspection_transient_denial_requeues_exact_run(
    tmp_path,
    monkeypatch,
    reason_code: str,
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
    monkeypatch.setattr(
        "worker.autopilot_inspection._utc_now",
        lambda: _NOW + timedelta(seconds=2),
    )

    def transient_after_inspection(
        application_id: int,
        *,
        inspection_run_id: int,
        claim_token: str,
    ):
        finalize_db = scenario.factory()
        try:
            application = finalize_db.get(Application, application_id)
            application.needs_review_reason = reason_code
            finalize_db.commit()
            return _finalize_autopilot_dispatch_result(
                finalize_db,
                application_id=application_id,
                result=AutopilotDispatchResult(
                    state="quarantined",
                    reason_code=reason_code,
                ),
                inspection_run_id=inspection_run_id,
                claim_token=claim_token,
                now=_NOW + timedelta(seconds=1),
            )
        finally:
            finalize_db.close()

    monkeypatch.setattr(
        "worker.autopilot_inspection.inspect_and_dispatch_qualified_autopilot",
        transient_after_inspection,
    )
    result = execute_qualified_autopilot_inspection(
        queued.run_id,
        now=_NOW,
    )

    assert result == {
        "state": "retryable",
        "reason_code": reason_code,
        "policy_decision_id": None,
        "attempt_id": None,
        "command_id": None,
        "replayed": False,
        "run_id": queued.run_id,
        "lease_finished": True,
    }
    verify = scenario.factory()
    application = verify.get(Application, scenario.application_id)
    run = verify.get(AutopilotInspectionRun, queued.run_id)
    assert application.needs_review_reason is None
    assert run.state == "queued"
    assert run.claimed_at is None
    assert run.lease_expires_at is None
    assert run.claim_token is None
    assert run.finished_at is None
    assert run.reason_code is None
    assert verify.query(Submission).count() == 0
    assert verify.query(SubmissionCommand).count() == 0
    reclaimed = _claim_inspection_run(
        verify,
        run_id=queued.run_id,
        now=_NOW + timedelta(seconds=3),
    )
    assert reclaimed is not None
    assert reclaimed[0] == scenario.application_id
    verify.close()


def test_mid_inspection_kill_switch_dispatch_remains_retryable(tmp_path, monkeypatch) -> None:
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
    application_id, claim_token = claimed

    set_automation_kill_switch(
        db,
        active=True,
        source=PolicyAuthoritySource.LOCAL_OPERATOR,
        reason_code="OPERATOR_STOP",
        now=_NOW + timedelta(seconds=1),
    )
    db.commit()
    dispatch_result = dispatch_qualified_autopilot(
        db,
        application_id=application_id,
        form_plan_id=scenario.plan_id,
        settings=_settings(),
        capabilities=_capabilities(),
        now=_NOW + timedelta(seconds=2),
    )
    assert dispatch_result.state == "quarantined"
    assert dispatch_result.reason_code == "KILL_SWITCH_ACTIVE"
    db.expire_all()
    assert db.get(Application, application_id).needs_review_reason is None

    result = _finalize_autopilot_dispatch_result(
        db,
        application_id=application_id,
        result=dispatch_result,
        inspection_run_id=queued.run_id,
        claim_token=claim_token,
        now=_NOW + timedelta(seconds=3),
    )
    assert result["state"] == "retryable"
    db.expire_all()
    assert db.get(Application, application_id).needs_review_reason is None
    assert db.get(AutopilotInspectionRun, queued.run_id).state == "queued"
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0

    set_automation_kill_switch(
        db,
        active=False,
        source=PolicyAuthoritySource.LOCAL_OPERATOR,
        reason_code="OPERATOR_RESUME",
        now=_NOW + timedelta(seconds=4),
    )
    db.commit()
    reclaimed = _claim_inspection_run(
        db,
        run_id=queued.run_id,
        now=_NOW + timedelta(seconds=5),
    )
    assert reclaimed is not None
    assert reclaimed[0] == application_id
    db.close()


@pytest.mark.parametrize("reason_code", ["KILL_SWITCH_ACTIVE", "OUTSIDE_ACTIVE_HOURS"])
def test_transient_denial_immediately_after_claim_requeues_exact_run(
    tmp_path,
    monkeypatch,
    reason_code: str,
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
    monkeypatch.setattr(
        "worker.autopilot_inspection._utc_now",
        lambda: _NOW + timedelta(seconds=2),
    )

    class InspectionClock:
        @classmethod
        def now(cls, _timezone=None):
            return _NOW + timedelta(seconds=1)

    monkeypatch.setattr("worker.autopilot_inspection.datetime", InspectionClock)
    validation_calls = 0

    def deny_second_validation(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 2:
            raise AutomationPolicyError(reason_code)
        return validate_automation_inspection_candidate(*args, **kwargs)

    monkeypatch.setattr(
        "worker.autopilot_inspection.validate_automation_inspection_candidate",
        deny_second_validation,
    )
    result = execute_qualified_autopilot_inspection(queued.run_id, now=_NOW)

    assert result == {
        "state": "retryable",
        "reason_code": reason_code,
        "policy_decision_id": None,
        "attempt_id": None,
        "command_id": None,
        "replayed": False,
        "run_id": queued.run_id,
        "lease_finished": True,
    }
    verify = scenario.factory()
    application = verify.get(Application, scenario.application_id)
    run = verify.get(AutopilotInspectionRun, queued.run_id)
    assert application.needs_review_reason is None
    assert run.state == "queued"
    assert run.claimed_at is None
    assert run.lease_expires_at is None
    assert run.claim_token is None
    assert run.finished_at is None
    assert run.reason_code is None
    assert verify.query(Submission).count() == 0
    assert verify.query(SubmissionCommand).count() == 0
    reclaimed = _claim_inspection_run(
        verify,
        run_id=queued.run_id,
        now=_NOW + timedelta(seconds=3),
    )
    assert reclaimed is not None
    assert reclaimed[0] == scenario.application_id
    verify.close()


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
    activation_calls: list[str] = []

    def private_bindings(db, *_args, **_kwargs):
        activation_calls.append("bindings")
        return {
            "profile_version": 1,
            "role_families": ("cv-ai",),
            "routing_config_digest": _ROUTING_DIGEST,
            "cv_manifest_digest": _MANIFEST_DIGEST,
            "fit_qualification_digest": _QUALIFICATION_DIGEST,
            "confirmed_answer_revision": confirmed_answer_revision(
                db,
                profile_version=1,
            ),
        }

    monkeypatch.setattr(
        policy_service,
        "_private_bindings",
        private_bindings,
    )
    monkeypatch.setattr(
        policy_service,
        "_current_artifact_bindings",
        lambda *_args, **_kwargs: _artifact_bindings(),
    )
    monkeypatch.setattr(
        policy_service,
        "require_policy_artifact_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(snapshot_id="8" * 64),
    )
    monkeypatch.setattr(
        policy_service,
        "materialize_policy_artifact_snapshot",
        lambda *_args, **_kwargs: SimpleNamespace(snapshot_id="8" * 64),
    )
    monkeypatch.setattr(
        policy_service,
        "lock_automation_authority_fence",
        lambda _db: activation_calls.append("authority"),
    )
    monkeypatch.setattr(policy_service, "_scope_has_live_canary", lambda *_args: False)
    seed_db = factory()
    seed_db.add(
        UserProfileVersion(
            profile_yaml=(
                "personal:\n  name: Evidence Candidate\n  email: candidate@example.test\n"
            ),
            version=1,
        )
    )
    seed_db.commit()
    seed_db.close()
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
    with pytest.raises(
        AutomationPolicyError,
        match="AUTOMATION_POLICY_FORM_SCOPE_NOT_QUALIFIED",
    ):
        activate_auto_submit_policy(
            policy_db,
            settings=settings,
            role_families=("cv-ai",),
            geographies=(AutomationGeography.ISRAEL,),
            permitted_adapters=("greenhouse",),
            qualified_form_contracts=(scope,),
            now=_NOW,
        )
    assert activation_calls[:2] == ["authority", "bindings"]
    policy_db.rollback()
    policy_db.close()
    monkeypatch.setattr(policy_service, "_scope_has_live_canary", lambda *_args: True)
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
    activation_count = len(activation_calls)
    for invalid_scope in (
        {"adapter_name": "Greenhouse"},
        {"adapter_version": "abcde"},
        {"selector_version": "selector with spaces"},
    ):
        malformed = client.post(
            "/api/automation/policy/activate",
            json={
                **payload,
                "qualified_form_contracts": [
                    {**payload["qualified_form_contracts"][0], **invalid_scope}
                ],
            },
            headers={"Authorization": f"Bearer {settings.secret_key}"},
        )
        assert malformed.status_code == 422
    assert len(activation_calls) == activation_count
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


def test_new_profile_version_invalidates_inspection_reservation_and_status(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    try:
        reserved = evaluate_auto_submit_policy(
            db,
            application_id=scenario.application_id,
            form_plan_id=scenario.plan_id,
            now=_NOW,
        )
        assert reserved.allowed is True
        db.commit()

        db.add(
            UserProfileVersion(
                profile_yaml=(
                    "personal:\n  name: Updated Candidate\n  email: updated@example.test\n"
                ),
                version=2,
            )
        )
        db.commit()

        with pytest.raises(AutomationPolicyError, match="PROFILE_VERSION_CHANGED"):
            validate_automation_inspection_candidate(
                db,
                application_id=scenario.application_id,
                now=_NOW + timedelta(minutes=1),
            )
        with pytest.raises(AutomationPolicyError, match="PROFILE_VERSION_CHANGED"):
            validate_current_automation_decision(
                db,
                decision_record=reserved,
                now=_NOW + timedelta(minutes=1),
                lock=True,
            )
        status = policy_usage_status(db, now=_NOW + timedelta(minutes=1))
        assert status["active"] is False
        assert status["reason_code"] == "PROFILE_VERSION_CHANGED"
    finally:
        db.close()


def test_changed_confirmed_answer_revision_invalidates_policy_status(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    try:
        assert policy_usage_status(db, now=_NOW)["active"] is True
        db.add(
            OperatorApprovedAnswer(
                canonical_field="non_sensitive_example",
                field_type="text",
                option_set_hash="0" * 64,
                locale="en",
                profile_version=1,
                selected_cv_id="cv-ai",
                selected_cv_hash=_CV_HASH,
                adapter_name=scenario.live_descriptor.platform,
                adapter_version=scenario.live_descriptor.adapter_version,
                selector_version=scenario.live_descriptor.selector_version,
                form_fingerprint=_FINGERPRINT,
                policy_version="answer-policy-v1",
                answer_json='"operator-confirmed"',
                evidence_reference="operator-confirmation-test",
                approved_at=_NOW.replace(tzinfo=None),
            )
        )
        db.commit()

        status = policy_usage_status(db, now=_NOW + timedelta(minutes=1))
        assert status["active"] is False
        assert status["reason_code"] == "CONFIRMED_ANSWERS_CHANGED"
    finally:
        db.close()


def test_stale_profile_policy_cannot_reserve_new_authority(tmp_path, monkeypatch) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    try:
        db.add(
            UserProfileVersion(
                profile_yaml="personal:\n  name: Updated Candidate\n",
                version=2,
            )
        )
        db.commit()

        decision = evaluate_auto_submit_policy(
            db,
            application_id=scenario.application_id,
            form_plan_id=scenario.plan_id,
            now=_NOW,
        )
        assert decision.allowed is False
        assert "PROFILE_VERSION_CHANGED" in json.loads(decision.reason_codes_json)
    finally:
        db.close()


@pytest.mark.parametrize(
    "binding_name",
    [
        "routing_config_digest",
        "cv_manifest_digest",
        "fit_qualification_digest",
    ],
)
def test_changed_artifact_binding_invalidates_inspection_commit_and_status(
    tmp_path,
    monkeypatch,
    binding_name: str,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    db = scenario.factory()
    try:
        reserved = evaluate_auto_submit_policy(
            db,
            application_id=scenario.application_id,
            form_plan_id=scenario.plan_id,
            now=_NOW,
        )
        assert reserved.allowed is True
        db.commit()

        monkeypatch.setattr(
            policy_service,
            "_current_artifact_bindings",
            lambda *_args, **_kwargs: _artifact_bindings(**{binding_name: "9" * 64}),
        )

        with pytest.raises(AutomationPolicyError, match="FIT_QUALIFICATION_CHANGED"):
            validate_automation_inspection_candidate(
                db,
                application_id=scenario.application_id,
                now=_NOW + timedelta(minutes=1),
            )
        with pytest.raises(AutomationPolicyError, match="FIT_QUALIFICATION_CHANGED"):
            validate_current_automation_decision(
                db,
                decision_record=reserved,
                now=_NOW + timedelta(minutes=1),
                lock=True,
            )
        status = policy_usage_status(db, now=_NOW + timedelta(minutes=1))
        assert status["active"] is False
        assert status["reason_code"] == "FIT_QUALIFICATION_CHANGED"
    finally:
        db.close()


@pytest.mark.parametrize(
    "binding_name",
    [
        "routing_config_digest",
        "cv_manifest_digest",
        "fit_qualification_digest",
    ],
)
def test_changed_artifact_binding_cannot_reserve_new_authority(
    tmp_path,
    monkeypatch,
    binding_name: str,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)
    monkeypatch.setattr(
        policy_service,
        "_current_artifact_bindings",
        lambda *_args, **_kwargs: _artifact_bindings(**{binding_name: "9" * 64}),
    )
    db = scenario.factory()
    try:
        decision = evaluate_auto_submit_policy(
            db,
            application_id=scenario.application_id,
            form_plan_id=scenario.plan_id,
            now=_NOW,
        )
        assert decision.allowed is False
        assert "FIT_QUALIFICATION_CHANGED" in json.loads(decision.reason_codes_json)
        assert db.query(Submission).count() == 0
        assert db.query(SubmissionCommand).count() == 0
    finally:
        db.close()


def test_unexpected_artifact_read_failure_is_a_bounded_authority_denial(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = _scenario(tmp_path, monkeypatch)

    def fail_artifact_read(*_args, **_kwargs):
        raise OSError("private artifact storage unavailable")

    monkeypatch.setattr(
        policy_service,
        "_current_artifact_bindings",
        fail_artifact_read,
    )
    db = scenario.factory()
    try:
        with pytest.raises(AutomationPolicyError, match="FIT_QUALIFICATION_CHANGED"):
            validate_automation_inspection_candidate(
                db,
                application_id=scenario.application_id,
                now=_NOW,
            )
        status = policy_usage_status(db, now=_NOW)
        assert status["active"] is False
        assert status["reason_code"] == "FIT_QUALIFICATION_CHANGED"
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
    assert db.get(Application, scenario.application_id).needs_review_reason is None
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


def test_inspection_lease_fence_uses_authority_before_run_row(
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
    claimed = _claim_inspection_run(db, run_id=queued.run_id, now=_NOW)
    assert claimed is not None
    _application_id, claim_token = claimed
    observed: list[str] = []
    original_query = db.query

    def observe_query(*entities, **kwargs):
        if AutopilotInspectionRun in entities:
            observed.append("run")
        return original_query(*entities, **kwargs)

    monkeypatch.setattr(
        "worker.autopilot_inspection.lock_automation_authority_fence",
        lambda _db: observed.append("authority"),
    )
    monkeypatch.setattr(db, "query", observe_query)

    _fence_inspection_run(
        db,
        run_id=queued.run_id,
        application_id=scenario.application_id,
        claim_token=claim_token,
        now=_NOW + timedelta(seconds=1),
    )

    assert observed[:2] == ["authority", "run"]
    db.rollback()
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


def test_autopilot_final_admission_uses_immutable_snapshot_after_live_swap(
    tmp_path,
    monkeypatch,
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

    immutable_cv = (tmp_path / "immutable-policy-snapshot" / "cvs" / "000.pdf").resolve()
    immutable_cv.parent.mkdir(parents=True)
    immutable_cv.write_bytes(b"immutable reviewed CV bytes")
    mutable_live_cv = (tmp_path / "cvs" / "ai.pdf").resolve()
    mutable_live_cv.parent.mkdir()
    mutable_live_cv.write_bytes(b"original live CV bytes")
    expected_snapshot_id = policy_artifact_snapshot_id(scenario.policy)
    selected = SimpleNamespace(
        cv_id="cv-ai",
        pdf_sha256=_CV_HASH,
        resolved_path=str(immutable_cv),
    )
    resolver_calls: list[str] = []

    def resolve_snapshot(policy, *, cv_id, expected_sha256, settings):
        assert policy.payload_digest == scenario.policy.payload_digest
        assert cv_id == "cv-ai"
        assert expected_sha256 == _CV_HASH
        resolver_calls.append(str(settings.data_dir))
        return selected, expected_snapshot_id

    snapshot_checks: list[str] = []

    def require_snapshot(policy, *, selected_cv_id, selected_cv_hash, **_kwargs):
        assert policy.payload_digest == scenario.policy.payload_digest
        assert selected_cv_id == "cv-ai"
        assert selected_cv_hash == _CV_HASH
        snapshot_checks.append(immutable_cv.read_text(encoding="utf-8"))
        if len(snapshot_checks) == 1:
            # This is the reviewed race: mutable operator files can change
            # after the first admission read, but preflight and commit remain
            # bound to the content-addressed versioned CV path.
            mutable_live_cv.write_bytes(b"replacement after authority read")
        return SimpleNamespace(snapshot_id=expected_snapshot_id)

    monkeypatch.setattr(
        "worker.submission_commands.resolve_selected_cv_artifact_snapshot",
        resolve_snapshot,
    )
    monkeypatch.setattr(
        "worker.submission_commands.require_policy_artifact_snapshot",
        require_snapshot,
    )
    monkeypatch.setattr(
        "profile.cv_content_cache.require_current_selected_cv_artifact",
        lambda candidate, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        "worker.submission_commands._now",
        lambda: _NOW.replace(tzinfo=None),
    )

    class SnapshotExecutor:
        context = None

        async def preflight(self, *, plan, permit, context):
            self.context = context
            prepared_at = _NOW
            return PreparedFinalActionV1(
                attempt_id=permit.attempt_id,
                adapter_name=plan.adapter_name,
                adapter_version=plan.adapter_version,
                selector_version=plan.selector_version,
                form_fingerprint=plan.form_fingerprint,
                attached_cv_hash=plan.attached_cv_hash,
                prepared_at=prepared_at,
                expires_at=min(permit.expires_at, prepared_at + timedelta(minutes=1)),
                action_nonce="9" * 64,
            )

        async def commit(self, *, action, permit):
            return ConfirmedSubmittedOutcome(
                evidence=DomainSubmissionEvidence(
                    attempt_id=permit.attempt_id,
                    evidence_type=EvidenceType.EMPLOYER_APPLICATION_ID,
                    employer_application_id="opaque-snapshot-proof",
                    form_fingerprint=action.form_fingerprint,
                    attached_cv_hash=action.attached_cv_hash,
                    observed_at=_NOW + timedelta(seconds=1),
                    digest="7" * 64,
                )
            )

    executor = SnapshotExecutor()
    registry = SimpleNamespace(resolve_final_executor=lambda *_args: executor)
    result = execute_claimed_submission_command(
        db,
        queued.command_id,
        registry=registry,
        settings=_settings(),
        governor=SimpleNamespace(
            reserve_final_action=lambda **_kwargs: (True, "reserved"),
        ),
    )

    assert result == "confirmed_submitted"
    assert resolver_calls == ["."]
    assert len(snapshot_checks) == 2
    assert snapshot_checks == ["immutable reviewed CV bytes"] * 2
    assert mutable_live_cv.read_bytes() == b"replacement after authority read"
    assert executor.context.resume_path == str(immutable_cv)
    db.expire_all()
    attempt = db.get(Submission, queued.attempt_id)
    assert attempt.final_action_at is not None
    assert attempt.outcome == "confirmed_submitted"
    db.close()


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


def test_dispatch_does_not_shadow_qualification_aware_descriptor_resolution(
    tmp_path,
    monkeypatch,
) -> None:
    """The production call site passes no resolver, and must not get one.

    dispatch_qualified_autopilot defaulted descriptor_resolver to
    adapter_for_url and forwarded it unconditionally, so
    create_submission_commands never fell through to its own default,
    effective_live_descriptor_for_plan. Qualification-aware resolution
    therefore never ran on the production path and every autopilot send
    raised ADAPTER_NOT_QUALIFIED. Every other test in this file injects a
    resolver, which is exactly why the defect survived.

    End-to-end coverage of the resolved path needs a real job URL and a real
    fixture digest; this scenario's descriptor is synthetic. So this test pins
    the defect itself: absent an explicit resolver, none is forwarded.
    """
    scenario = _scenario(tmp_path, monkeypatch)
    captured: dict[str, object] = {}

    def _capture(db, requests, **kwargs):
        captured.update(kwargs)
        raise SubmissionAdmissionError("PROBE_STOP")

    monkeypatch.setattr("worker.autopilot.create_submission_commands", _capture)
    db = scenario.factory()
    dispatch_qualified_autopilot(
        db,
        application_id=scenario.application_id,
        form_plan_id=scenario.plan_id,
        settings=_settings(),
        capabilities=_capabilities(),
        session_checker=lambda *_args: True,
        now=_NOW,
    )
    assert "descriptor_resolver" not in captured, (
        "no resolver was supplied, so none may be forwarded — forwarding one "
        "shadows effective_live_descriptor_for_plan"
    )
    db.close()


def test_dispatch_still_honours_an_explicit_resolver(tmp_path, monkeypatch) -> None:
    """An explicitly supplied resolver must still reach create_submission_commands."""
    scenario = _scenario(tmp_path, monkeypatch)
    captured: dict[str, object] = {}
    sentinel = lambda _url: scenario.live_descriptor  # noqa: E731

    def _capture(db, requests, **kwargs):
        captured.update(kwargs)
        raise SubmissionAdmissionError("PROBE_STOP")

    monkeypatch.setattr("worker.autopilot.create_submission_commands", _capture)
    db = scenario.factory()
    dispatch_qualified_autopilot(
        db,
        application_id=scenario.application_id,
        form_plan_id=scenario.plan_id,
        settings=_settings(),
        capabilities=_capabilities(),
        descriptor_resolver=sentinel,
        session_checker=lambda *_args: True,
        now=_NOW,
    )
    assert captured.get("descriptor_resolver") is sentinel
    db.close()


def test_qualified_autopilot_is_a_recognised_audit_actor() -> None:
    """worker/autopilot_inspection.py records this actor.

    Absent from _ALLOWED_ACTORS it was silently relabelled "system", making an
    unattended send indistinguishable from routine worker activity in the
    audit trail — in a design whose whole basis is knowing who authorised a
    send.
    """
    from core.application_audit import _ALLOWED_ACTORS

    assert "qualified_autopilot" in _ALLOWED_ACTORS
