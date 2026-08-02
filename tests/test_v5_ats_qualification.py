"""Strict ATS dry-run, canary, drift, replay, and privacy authority tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from api.routes import applications as applications_route
from core.adapter_qualification_service import (
    AdapterQualificationError,
    consume_qualification_canary_authorization,
    effective_inspection_descriptor,
    effective_live_descriptor_for_plan,
    fixture_evidence_digest,
    invalidate_stale_qualification_records,
    mint_qualification_canary_authorization,
    record_dry_run_qualification,
    record_live_canary_confirmation,
    scope_has_live_qualification,
    validate_qualification_canary_authorization,
)
from core.automation_policy_service import form_contract_digest
from core.config import Settings
from core.runtime_identity import get_runtime_identity
from core.submission_domain import (
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    ConfirmedSubmittedOutcome,
    EvidenceType,
    FieldType,
    FormFieldV1,
    FormPlanV1,
    PreparedFinalActionV1,
)
from core.submission_domain import SubmissionEvidence as DomainSubmissionEvidence
from core.submission_service import (
    ClientReleaseIdentity,
    SubmissionAdmissionError,
    SubmissionCommandRequest,
    _require_runtime,
    create_submission_commands,
)
from db.models import (
    AdapterQualificationRecord,
    Application,
    Base,
    BrowserQualificationRun,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionEvidence,
    SubmissionStatus,
    UserProfileVersion,
)
from ingestion.url_utils import normalize_url, url_hash
from jobs.models import JobData
from llm.qualification_registry import load_qualified_local_model
from submitters.platforms import QualificationTier, adapter_for_url
from worker.submission_commands import (
    claim_submission_command,
    execute_claimed_submission_command,
)

_NOW = datetime.now(UTC).replace(microsecond=0)
_JOB_URL = "https://boards.greenhouse.io/acme/jobs/123"
_CV_HASH = "c" * 64
_FINGERPRINT = "f" * 64
_RUNNER_RELEASE = get_runtime_identity().release_id
_PRIVATE_EMAIL = "candidate.private@example.test"
_MODEL_DIGEST = load_qualified_local_model().digest


def _scenario(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'qualification.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    descriptor = adapter_for_url(_JOB_URL)
    assert descriptor is not None
    job = Job(
        title="Private role title",
        company="Private employer",
        source_url=_JOB_URL,
        apply_url=_JOB_URL,
        status=JobStatus.DRAFT,
    )
    application = Application(
        job=job,
        status=JobStatus.DRAFT,
        selected_cv_id="private-cv-ai",
        selected_cv_hash=_CV_HASH,
        profile_version=1,
        revision=1,
        prepared_revision=1,
        approved_at=_NOW.replace(tzinfo=None),
        approval_source="manual_prepare",
        material_eligible=True,
        material_blockers_json="[]",
        material_model_provider="ollama",
        material_model_name="qwen2.5:7b",
        material_model_digest=_MODEL_DIGEST,
        material_prompt_version="application-materials-v1",
    )
    db.add(application)
    db.add(
        UserProfileVersion(
            profile_yaml="personal:\n  name: Evidence Candidate\n",
            version=1,
        )
    )
    db.flush()
    field = FormFieldV1(
        field_id="candidate-email",
        canonical_name="email",
        label="Email",
        field_type=FieldType.EMAIL,
        required=True,
        position=0,
    )
    decision = AnswerDecisionV1(
        field_id=field.field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.DETERMINISTIC_IDENTITY,
        value=_PRIVATE_EMAIL,
        confidence=1.0,
        evidence_refs=("profile:email",),
    )
    domain = FormPlanV1(
        plan_id=uuid4(),
        application_id=application.id,
        application_revision=application.revision,
        adapter_name=descriptor.platform,
        adapter_version=descriptor.adapter_version,
        selector_version=descriptor.selector_version,
        form_fingerprint=_FINGERPRINT,
        selected_cv_id=application.selected_cv_id,
        selected_cv_hash=_CV_HASH,
        attached_cv_id=application.selected_cv_id,
        attached_cv_hash=_CV_HASH,
        attachment_verified=True,
        profile_version=1,
        session_verified_at=_NOW,
        created_at=_NOW,
        expires_at=_NOW + timedelta(minutes=30),
        fields=(field,),
        decisions=(decision,),
    )
    plan = FormPlan(
        plan_id=str(domain.plan_id),
        application_id=application.id,
        application_revision=application.revision,
        adapter_name=domain.adapter_name,
        adapter_version=domain.adapter_version,
        selector_version=domain.selector_version,
        fingerprint=domain.form_fingerprint,
        selected_cv_id=domain.selected_cv_id,
        selected_cv_hash=domain.selected_cv_hash,
        attached_cv_id=domain.attached_cv_id,
        attached_cv_hash=domain.attached_cv_hash,
        attachment_verified=True,
        attachment_verification_source="browser_upload_receipt",
        attachment_verified_at=_NOW.replace(tzinfo=None),
        profile_version=1,
        fields_json=json.dumps([field.model_dump(mode="json")]),
        disclosures_json="[]",
        decisions_json=json.dumps([decision.model_dump(mode="json")]),
        blockers_json="[]",
        locale="en",
        session_verified_at=_NOW.replace(tzinfo=None),
        created_at=_NOW.replace(tzinfo=None),
        expires_at=(_NOW + timedelta(minutes=30)).replace(tzinfo=None),
    )
    db.add(plan)
    db.flush()
    contract_digest = form_contract_digest(plan)
    return db, engine, application, plan, descriptor, contract_digest


def _capabilities(*, first_canary: bool = False) -> dict[str, object]:
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
            "submission_ready": not first_canary,
            "stages": {
                "submission": {
                    "ready": not first_canary,
                    "reason_codes": ["ADAPTER_NOT_QUALIFIED"] if first_canary else [],
                }
            },
        },
        "submission": {
            "allowed": not first_canary,
            "reasons": ["ADAPTER_NOT_QUALIFIED"] if first_canary else [],
        },
        "llm": {
            "provider": "ollama",
            "model": "qwen2.5:7b",
            "local": True,
            "digest": _MODEL_DIGEST,
            "ready": True,
            "reason_code": None,
        },
    }


def _client_release() -> ClientReleaseIdentity:
    identity = get_runtime_identity()
    return ClientReleaseIdentity(
        build_sha=identity.build_sha,
        ui_asset_digest=identity.ui_asset_digest,
        source_digest=identity.source_digest,
        protocol_version=identity.protocol_version,
        boot_id=identity.boot_id,
    )


def test_first_canary_runtime_waives_only_missing_live_qualification() -> None:
    capabilities = _capabilities(first_canary=True)
    assert _require_runtime(capabilities, authority_kind="qualification_canary") == _RUNNER_RELEASE
    with pytest.raises(SubmissionAdmissionError):
        _require_runtime(capabilities, authority_kind="explicit_operator")
    submission = capabilities["submission"]
    assert isinstance(submission, dict)
    reasons = submission["reasons"]
    assert isinstance(reasons, list)
    reasons.append("WORKER_NOT_READY")
    with pytest.raises(SubmissionAdmissionError):
        _require_runtime(capabilities, authority_kind="qualification_canary")


@pytest.mark.asyncio
async def test_canary_route_requires_auth_literal_ack_and_replays_one_command(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: True,
    )
    db, engine, application, plan, _descriptor, _contract_digest = _scenario(tmp_path)
    settings = Settings(
        _env_file=None,
        app_env="test",
        secret_key="qualification-route-secret-" + "x" * 40,
        dry_run=False,
        draft_only=False,
        portal_final_submit_enabled=True,
        live_automation_acknowledged=True,
    )
    record_dry_run_qualification(
        db,
        application=application,
        plan=plan,
        job_url=_JOB_URL,
        runner_release=_RUNNER_RELEASE,
        now=_NOW,
    )
    db.commit()

    original_create = applications_route.create_submission_commands

    def create_bound_commands(session, requests):
        return original_create(
            session,
            requests,
            settings=settings,
            capabilities=_capabilities(first_canary=True),
            session_checker=lambda *_args: True,
            now=datetime.now(UTC).replace(tzinfo=None),
        )

    wake_calls: list[int] = []
    monkeypatch.setattr(applications_route, "get_settings", lambda: settings)
    monkeypatch.setattr(
        applications_route,
        "create_submission_commands",
        create_bound_commands,
    )
    monkeypatch.setattr(
        applications_route,
        "_wake_submission_command",
        wake_calls.append,
    )
    monkeypatch.setattr(applications_route, "runtime_source_is_current", lambda _identity: True)

    client_release = _client_release()
    payload = applications_route.QualificationCanaryRequest(
        acknowledgement="SEND_QUALIFICATION_CANARY",
        idempotency_key="qualification-route-canary",
        application_revision=application.revision,
        form_plan_id=plan.plan_id,
        client_release=applications_route.ClientReleaseIdentityRequest(
            build_sha=client_release.build_sha,
            ui_asset_digest=client_release.ui_asset_digest,
            source_digest=client_release.source_digest,
            protocol_version=client_release.protocol_version,
            boot_id=client_release.boot_id,
        ),
    )
    authenticated = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/applications/{application.id}/qualification/canary",
            "headers": [(b"authorization", f"Bearer {settings.secret_key}".encode("ascii"))],
            "query_string": b"",
        }
    )
    unauthenticated = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/applications/{application.id}/qualification/canary",
            "headers": [],
            "query_string": b"",
        }
    )

    try:
        with pytest.raises(ValidationError):
            applications_route.QualificationCanaryRequest.model_validate(
                {
                    **payload.model_dump(),
                    "acknowledgement": "SEND_APPLICATION",
                }
            )
        with pytest.raises(HTTPException) as auth_error:
            await applications_route.submit_qualification_canary(
                application.id,
                payload,
                unauthenticated,
                db,
            )
        assert auth_error.value.status_code == 401
        assert db.query(Submission).count() == 0

        first = await applications_route.submit_qualification_canary(
            application.id,
            payload,
            authenticated,
            db,
        )
        replay = await applications_route.submit_qualification_canary(
            application.id,
            payload,
            authenticated,
            db,
        )

        assert first.replayed is False
        assert replay.replayed is True
        assert replay.attempt_id == first.attempt_id
        assert replay.command_id == first.command_id
        assert wake_calls == [first.command_id]
        assert db.query(Submission).count() == 1
        attempt = db.get(Submission, first.attempt_id)
        assert attempt.authority_kind == "qualification_canary"
        assert attempt.qualification_canary_authorization.consumed_at is not None
    finally:
        db.close()
        engine.dispose()


class _AllowGovernor:
    def reserve_final_action(self, *, reservation_id, platform):
        del reservation_id, platform
        return True, "reserved"


class _ConfirmedCanaryExecutor:
    commit_calls = 0

    async def preflight(self, *, plan, permit):
        prepared_at = datetime.now(UTC)
        return PreparedFinalActionV1(
            attempt_id=permit.attempt_id,
            adapter_name=plan.adapter_name,
            adapter_version=plan.adapter_version,
            selector_version=plan.selector_version,
            form_fingerprint=plan.form_fingerprint,
            attached_cv_hash=plan.attached_cv_hash,
            prepared_at=prepared_at,
            expires_at=min(prepared_at + timedelta(minutes=1), permit.expires_at),
            action_nonce="8" * 64,
        )

    async def commit(self, *, action, permit):
        self.commit_calls += 1
        return ConfirmedSubmittedOutcome(
            evidence=DomainSubmissionEvidence(
                attempt_id=permit.attempt_id,
                evidence_type=EvidenceType.EMPLOYER_APPLICATION_ID,
                employer_application_id="opaque-canary-provider-reference",
                form_fingerprint=action.form_fingerprint,
                attached_cv_hash=action.attached_cv_hash,
                observed_at=datetime.now(UTC),
                digest="7" * 64,
            )
        )


def test_legacy_telemetry_never_authorizes_and_dry_run_never_enables_commit(tmp_path):
    db, engine, application, plan, descriptor, contract_digest = _scenario(tmp_path)
    try:
        db.add(
            BrowserQualificationRun(
                selector_version=descriptor.selector_version,
                terminal_reason="LIVE_CANARY_CONFIRMED",
                qualified=True,
                trace_json=json.dumps({"unsafe": _PRIVATE_EMAIL}),
                adapter_name=descriptor.platform,
                adapter_version=descriptor.adapter_version,
                qualification_tier="live_canary_qualified",
                form_fingerprint=plan.fingerprint,
                form_contract_digest=contract_digest,
                fixture_digest=fixture_evidence_digest(descriptor.platform),
            )
        )
        db.commit()

        assert effective_inspection_descriptor(db, _JOB_URL) is None
        assert effective_live_descriptor_for_plan(db, job_url=_JOB_URL, plan=plan) is None
        assert not scope_has_live_qualification(
            db,
            adapter_name=descriptor.platform,
            adapter_version=descriptor.adapter_version,
            selector_version=descriptor.selector_version,
            form_contract_digest_value=contract_digest,
        )

        dry_run = record_dry_run_qualification(
            db,
            application=application,
            plan=plan,
            job_url=_JOB_URL,
            runner_release=_RUNNER_RELEASE,
            now=_NOW,
        )
        db.commit()
        inspection = effective_inspection_descriptor(db, _JOB_URL)
        assert inspection is not None
        assert inspection.qualification is QualificationTier.DRY_RUN_QUALIFIED
        assert inspection.allows_final_execution is False
        assert effective_live_descriptor_for_plan(db, job_url=_JOB_URL, plan=plan) is None
        trace = (
            db.query(BrowserQualificationRun)
            .filter(BrowserQualificationRun.terminal_reason == "DRY_RUN_QUALIFIED")
            .one()
            .trace_json
        )
        for private_value in (
            _PRIVATE_EMAIL,
            _JOB_URL,
            application.selected_cv_id,
            "Private role title",
            "Private employer",
        ):
            assert private_value not in trace
        assert dry_run.job_url_hash == url_hash(normalize_url(_JOB_URL))
    finally:
        db.close()
        engine.dispose()


def test_semantic_contract_binds_attachment_verification_mechanism(tmp_path) -> None:
    db, engine, _application, plan, _descriptor, original_digest = _scenario(tmp_path)
    try:
        plan.attachment_verification_source = "different_upload_receipt"
        assert form_contract_digest(plan) != original_digest
    finally:
        db.close()
        engine.dispose()


def test_canary_command_is_one_use_and_confirmation_promotes_in_same_flow(
    tmp_path,
    monkeypatch,
):
    scenario_now = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: True,
    )
    db, engine, application, plan, _descriptor, contract_digest = _scenario(tmp_path)
    try:
        record_dry_run_qualification(
            db,
            application=application,
            plan=plan,
            job_url=_JOB_URL,
            runner_release=_RUNNER_RELEASE,
            now=scenario_now,
        )
        authorization = mint_qualification_canary_authorization(
            db,
            application=application,
            plan=plan,
            job_url=_JOB_URL,
            runner_release=_RUNNER_RELEASE,
            now=scenario_now,
        )
        request = SubmissionCommandRequest(
            application_id=application.id,
            client_idempotency_key="qualification-canary-command",
            application_revision=application.revision,
            form_plan_id=plan.plan_id,
            client_release=_client_release(),
            authority_expires_at=authorization.expires_at,
            authority_kind="qualification_canary",
            qualification_canary_authorization_id=authorization.id,
        )
        settings = Settings(
            _env_file=None,
            app_env="test",
            secret_key="qualification-command-secret-" + "x" * 40,
            dry_run=False,
            draft_only=False,
            portal_final_submit_enabled=True,
            live_automation_acknowledged=True,
        )
        [created] = create_submission_commands(
            db,
            [request],
            settings=settings,
            capabilities=_capabilities(first_canary=True),
            session_checker=lambda *_args: True,
            now=scenario_now.replace(tzinfo=None),
        )
        [replayed] = create_submission_commands(
            db,
            [request],
            settings=settings,
            capabilities=_capabilities(first_canary=True),
            session_checker=lambda *_args: True,
            now=scenario_now.replace(tzinfo=None),
        )
        assert replayed.replayed is True
        assert replayed.attempt_id == created.attempt_id
        db.expire_all()
        attempt = db.get(Submission, created.attempt_id)
        assert attempt.authority_kind == "qualification_canary"
        assert attempt.qualification_canary_authorization.consumed_at is not None
        assert (
            attempt.final_submit_permit.qualification_canary_authorization_digest
            == authorization.authorization_digest
        )

        assert (
            claim_submission_command(
                db,
                command_id=created.command_id,
                now=scenario_now.replace(tzinfo=None),
            )
            == created.command_id
        )
        executor = _ConfirmedCanaryExecutor()
        registry = SimpleNamespace(resolve_final_executor=lambda *_args: executor)
        result = execute_claimed_submission_command(
            db,
            created.command_id,
            registry=registry,
            settings=settings,
            governor=_AllowGovernor(),
        )
        assert result == "confirmed_submitted"
        assert executor.commit_calls == 1
        assert (
            execute_claimed_submission_command(
                db,
                created.command_id,
                registry=registry,
                settings=settings,
                governor=_AllowGovernor(),
            )
            == "skipped"
        )
        assert executor.commit_calls == 1
        db.expire_all()
        attempt = db.get(Submission, created.attempt_id)
        assert attempt.outcome == "confirmed_submitted"
        assert attempt.submitted_at is not None
        live = (
            db.query(AdapterQualificationRecord)
            .filter(AdapterQualificationRecord.qualification_tier == "live_canary_qualified")
            .one()
        )
        assert live.attempt_id == attempt.id
        assert live.form_contract_digest == contract_digest
    finally:
        db.close()
        engine.dispose()


def test_one_use_canary_is_exact_and_only_employer_evidence_promotes(tmp_path):
    db, engine, application, plan, descriptor, contract_digest = _scenario(tmp_path)
    try:
        record_dry_run_qualification(
            db,
            application=application,
            plan=plan,
            job_url=_JOB_URL,
            runner_release=_RUNNER_RELEASE,
            now=_NOW + timedelta(minutes=1),
        )
        authorization = mint_qualification_canary_authorization(
            db,
            application=application,
            plan=plan,
            job_url=_JOB_URL,
            runner_release=_RUNNER_RELEASE,
            now=_NOW + timedelta(minutes=2),
        )
        with pytest.raises(
            AdapterQualificationError,
            match="CANARY_AUTHORIZATION_NOT_YET_VALID",
        ):
            validate_qualification_canary_authorization(
                db,
                authorization_id=authorization.id,
                authorization_digest=authorization.authorization_digest,
                application=application,
                plan=plan,
                job_url=_JOB_URL,
                runner_release=_RUNNER_RELEASE,
                consumed=False,
                now=_NOW + timedelta(minutes=1),
            )
        validate_qualification_canary_authorization(
            db,
            authorization_id=authorization.id,
            authorization_digest=authorization.authorization_digest,
            application=application,
            plan=plan,
            job_url=_JOB_URL,
            runner_release=_RUNNER_RELEASE,
            consumed=False,
            now=_NOW + timedelta(minutes=2),
        )
        original_hash = application.selected_cv_hash
        application.selected_cv_hash = "d" * 64
        with pytest.raises(AdapterQualificationError, match="CANARY_AUTHORIZATION_CHANGED"):
            validate_qualification_canary_authorization(
                db,
                authorization_id=authorization.id,
                authorization_digest=authorization.authorization_digest,
                application=application,
                plan=plan,
                job_url=_JOB_URL,
                runner_release=_RUNNER_RELEASE,
                consumed=False,
                now=_NOW + timedelta(minutes=2),
            )
        application.selected_cv_hash = original_hash
        consume_qualification_canary_authorization(
            authorization,
            now=_NOW + timedelta(minutes=2),
        )
        with pytest.raises(AdapterQualificationError, match="CANARY_AUTHORIZATION_REPLAYED"):
            consume_qualification_canary_authorization(authorization)
        validate_qualification_canary_authorization(
            db,
            authorization_id=authorization.id,
            authorization_digest=authorization.authorization_digest,
            application=application,
            plan=plan,
            job_url=_JOB_URL,
            runner_release=_RUNNER_RELEASE,
            consumed=True,
            now=_NOW + timedelta(minutes=2),
        )
        with pytest.raises(AdapterQualificationError, match="CANARY_AUTHORIZATION_EXPIRED"):
            validate_qualification_canary_authorization(
                db,
                authorization_id=authorization.id,
                authorization_digest=authorization.authorization_digest,
                application=application,
                plan=plan,
                job_url=_JOB_URL,
                runner_release=_RUNNER_RELEASE,
                consumed=True,
                now=_NOW + timedelta(minutes=8),
            )

        evidence_digest = "9" * 64
        submitted_at = (_NOW + timedelta(minutes=3)).replace(tzinfo=None)
        attempt = Submission(
            application=application,
            attempt_number=1,
            idempotency_key="qualification-canary-attempt",
            submitter_name=descriptor.platform,
            status=SubmissionStatus.UNKNOWN,
            stage="finished",
            outcome="unknown",
            application_revision=application.revision,
            adapter_name=descriptor.platform,
            adapter_version=descriptor.adapter_version,
            selector_version=descriptor.selector_version,
            form_plan_id=plan.id,
            form_plan_fingerprint=plan.fingerprint,
            selected_cv_id=plan.selected_cv_id,
            requested_cv_id=plan.selected_cv_id,
            requested_cv_hash=plan.selected_cv_hash,
            attached_cv_id=plan.attached_cv_id,
            attached_cv_hash=plan.attached_cv_hash,
            attachment_verified=True,
            profile_version=plan.profile_version,
            runner_release=_RUNNER_RELEASE,
            authority_kind="qualification_canary",
            qualification_canary_authorization=authorization,
            qualification_canary_authorization_digest=authorization.authorization_digest,
        )
        db.add(attempt)
        db.flush()
        with pytest.raises(AdapterQualificationError, match="EMPLOYER_EVIDENCE_REQUIRED"):
            record_live_canary_confirmation(
                db,
                attempt=attempt,
                plan=plan,
                evidence_digest=evidence_digest,
                runner_release=_RUNNER_RELEASE,
                now=_NOW + timedelta(minutes=3),
            )
        assert (
            db.query(AdapterQualificationRecord)
            .filter(AdapterQualificationRecord.qualification_tier == "live_canary_qualified")
            .count()
            == 0
        )

        attempt.status = SubmissionStatus.SUCCESS
        attempt.outcome = "confirmed_submitted"
        attempt.final_action_at = (_NOW + timedelta(minutes=2, seconds=30)).replace(tzinfo=None)
        attempt.submitted_at = submitted_at
        attempt.verification_kind = "employer_application_id"
        attempt.evidence_digest = evidence_digest
        db.add(
            SubmissionEvidence(
                attempt_id=attempt.id,
                evidence_type=attempt.verification_kind,
                evidence_digest=evidence_digest,
                employer_application_ref="opaque-provider-reference",
                form_fingerprint=plan.fingerprint,
                cv_hash=plan.attached_cv_hash,
                observed_at=submitted_at,
            )
        )
        live = record_live_canary_confirmation(
            db,
            attempt=attempt,
            plan=plan,
            evidence_digest=evidence_digest,
            runner_release=_RUNNER_RELEASE,
            now=_NOW + timedelta(minutes=3),
        )
        db.commit()
        assert live.qualification_tier == "live_canary_qualified"
        assert live.attempt_id == attempt.id
        assert scope_has_live_qualification(
            db,
            adapter_name=descriptor.platform,
            adapter_version=descriptor.adapter_version,
            selector_version=descriptor.selector_version,
            form_contract_digest_value=contract_digest,
        )
        effective = effective_live_descriptor_for_plan(db, job_url=_JOB_URL, plan=plan)
        assert effective is not None
        assert effective.allows_final_execution is True

        live.runner_release = "retired-release"
        db.commit()
        assert effective_live_descriptor_for_plan(db, job_url=_JOB_URL, plan=plan) is None
        assert (
            invalidate_stale_qualification_records(
                db,
                now=_NOW + timedelta(minutes=4),
            )
            == 1
        )
        assert live.invalidation_reason == "BUILD_MISMATCH"
    finally:
        db.close()
        engine.dispose()


def test_scoped_registry_never_elevates_a_different_adapter_or_form(tmp_path):
    db, engine, application, plan, descriptor, _contract_digest = _scenario(tmp_path)
    try:
        record_dry_run_qualification(
            db,
            application=application,
            plan=plan,
            job_url=_JOB_URL,
            runner_release=_RUNNER_RELEASE,
            now=_NOW + timedelta(minutes=1),
        )
        inspection = effective_inspection_descriptor(db, _JOB_URL)
        assert inspection is not None
        from submitters.registry import build_scoped_two_phase_registry

        registry = build_scoped_two_phase_registry(inspection)
        greenhouse_job = JobData(
            title="Role",
            company="Employer",
            location="Israel",
            apply_url=_JOB_URL,
            source_url=_JOB_URL,
        )
        lever_job = JobData(
            title="Other role",
            company="Other employer",
            location="Israel",
            apply_url="https://jobs.lever.co/acme/another-role",
            source_url="https://jobs.lever.co/acme/another-role",
        )
        assert registry.get_inspector(greenhouse_job) is not None
        assert registry.get_inspector(lever_job) is None
        assert (
            effective_inspection_descriptor(
                db,
                "https://jobs.lever.co/acme/another-role",
            )
            is None
        )
        assert descriptor.platform == "greenhouse"
    finally:
        db.close()
        engine.dispose()
