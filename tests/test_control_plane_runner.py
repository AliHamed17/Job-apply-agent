"""Transactional and configuration tests for the private control-plane runner."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from control_plane.job_control_plane import crypto as control_crypto
from control_plane.job_control_plane import protocol as control_protocol
from core.config import JOB_AGENT_ENV_FILE, Settings, get_settings
from core.control_plane_review_permits import (
    ReviewGrantProjection,
    ReviewGrantRevocationProjection,
    mint_control_plane_review_grant,
)
from core.runtime_identity import get_runtime_identity
from core.submission_service import SubmissionAdmissionError
from db.models import (
    Application,
    AutomationKillSwitchEvent,
    Base,
    ControlPlaneCommandReceipt,
    ControlPlaneEventOutbox,
    ControlPlaneReviewGrant,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
    SubmissionStatus,
    UserProfileVersion,
)
from llm.contracts import MATERIAL_PROMPT_VERSION
from llm.qualification_registry import load_qualified_local_model
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    QualificationTier,
    adapter_for_url,
)
from worker.control_plane_client import ControlPlaneClientError
from worker.control_plane_event_outbox import (
    enqueue_control_plane_attempt_transition,
)
from worker.control_plane_runner import (
    MAX_REVOCATIONS_PER_CYCLE,
    AcceptedControlCommand,
    ControlPlaneRunner,
    ControlPlaneRunnerError,
    RunnerConfig,
    VerifiedControlCommand,
    accept_control_plane_command,
    load_runner_config,
    wake_control_plane_submission_command,
)

_QUALIFIED_MODEL_DIGEST = load_qualified_local_model().digest


@pytest.fixture(autouse=True)
def _current_qualification_report(monkeypatch):
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda **_kwargs: True,
    )


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'control-runner.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


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
            "digest": _QUALIFIED_MODEL_DIGEST,
            "ready": True,
            "reason_code": None,
        },
    }


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        dry_run=False,
        draft_only=False,
        auto_apply=False,
        portal_final_submit_enabled=True,
        live_automation_acknowledged=True,
        secret_key="operator-auth-test-secret-" + "x" * 32,
    )


def _reviewed_application(factory):
    db = factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    fingerprint = "f" * 64
    job = Job(
        title="Evidence Engineer",
        company="Private Employer",
        source_url="https://boards.greenhouse.io/acme/jobs/123",
        apply_url="https://boards.greenhouse.io/acme/jobs/123",
        status=JobStatus.DRAFT,
    )
    descriptor = adapter_for_url(job.apply_url)
    assert descriptor is not None
    application = Application(
        job=job,
        status=JobStatus.DRAFT,
        selected_cv_id="cv-ai",
        selected_cv_hash="c" * 64,
        profile_version=1,
        material_eligible=True,
        material_blockers_json="[]",
        material_model_provider="ollama",
        material_model_name="qwen2.5:7b",
        material_model_digest=_QUALIFIED_MODEL_DIGEST,
        material_prompt_version=MATERIAL_PROMPT_VERSION,
        revision=1,
        prepared_revision=1,
        approved_at=now,
        approval_source="manual_prepare",
    )
    db.add(application)
    db.flush()
    db.add(
        UserProfileVersion(
            profile_yaml="personal:\n  name: Evidence Candidate\n",
            version=1,
        )
    )
    plan = FormPlan(
        plan_id=str(uuid4()),
        application_id=application.id,
        application_revision=1,
        adapter_name=descriptor.platform,
        adapter_version=descriptor.adapter_version,
        selector_version=descriptor.selector_version,
        fingerprint=fingerprint,
        selected_cv_id="cv-ai",
        selected_cv_hash="c" * 64,
        attached_cv_id="cv-ai",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
        profile_version=1,
        fields_json="[]",
        disclosures_json="[]",
        decisions_json="[]",
        blockers_json="[]",
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(plan)
    db.commit()
    projection = mint_control_plane_review_grant(
        db,
        application_id=application.id,
        form_plan_id=plan.id,
        runner_release=get_runtime_identity().release_id,
        now=now,
    )
    db.commit()
    result = SimpleNamespace(
        application_id=application.id,
        form_plan_id=plan.id,
        fingerprint=fingerprint,
        projection=projection,
        adapter=descriptor.platform,
        adapter_version=descriptor.adapter_version,
        now=now.replace(tzinfo=UTC),
    )
    db.close()
    return result


def _live_descriptor(fingerprint):
    descriptor = adapter_for_url("https://boards.greenhouse.io/acme/jobs/123")
    assert descriptor is not None
    return replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(fingerprint,),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    )


def _command(reviewed, *, fingerprint: str | None = None):
    return VerifiedControlCommand(
        command_id=str(uuid4()),
        grant_id=reviewed.projection.review_grant_ref,
        application_ref=reviewed.projection.remote_application_ref,
        application_revision=1,
        adapter=reviewed.adapter,
        adapter_version=reviewed.adapter_version,
        form_fingerprint_digest=fingerprint or reviewed.fingerprint,
        delivery_nonce=str(uuid4()),
        issued_at=reviewed.now,
        expires_at=reviewed.now + timedelta(minutes=5),
        envelope_digest="e" * 64,
    )


def test_signed_delivery_atomically_admits_once_and_replays_same_attempt(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    command = _command(reviewed)
    db = factory()
    first = accept_control_plane_command(
        db,
        command,
        settings=_settings(),
        capabilities=_capabilities(),
        descriptor_resolver=lambda _url: _live_descriptor(reviewed.fingerprint),
        session_checker=lambda *_args: True,
        now=reviewed.now + timedelta(seconds=1),
    )
    duplicate = accept_control_plane_command(
        db,
        command,
        settings=_settings(),
        capabilities=_capabilities(),
        descriptor_resolver=lambda _url: _live_descriptor(reviewed.fingerprint),
        session_checker=lambda *_args: True,
        now=reviewed.now + timedelta(seconds=2),
    )

    assert duplicate == replace(first, replayed=True)
    assert db.query(ControlPlaneCommandReceipt).count() == 1
    assert db.query(Submission).count() == 1
    assert db.query(SubmissionCommand).count() == 1
    assert db.query(ControlPlaneEventOutbox).count() == 1
    grant = db.query(ControlPlaneReviewGrant).one()
    assert grant.consumed_at is not None
    assert grant.consumed_command_ref == command.command_id
    attempt = db.get(Submission, first.attempt_id)
    assert attempt.final_submit_permit is not None
    assert attempt.final_submit_permit.consumed_at is None
    assert attempt.final_submit_permit.expires_at == command.expires_at.replace(tzinfo=None)
    db.close()


def test_slow_readiness_rechecks_remote_command_expiry_before_admission(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    capabilities = _capabilities()
    command = replace(
        _command(reviewed),
        expires_at=datetime.now(UTC) + timedelta(milliseconds=200),
    )

    def slow_capabilities(_settings, **_kwargs):
        time.sleep(0.4)
        return capabilities

    monkeypatch.setattr(
        "worker.control_plane_runner._capabilities",
        slow_capabilities,
    )
    db = factory()
    with pytest.raises(ControlPlaneRunnerError, match="CONTROL_COMMAND_EXPIRED"):
        accept_control_plane_command(
            db,
            command,
            settings=_settings(),
            descriptor_resolver=lambda _url: _live_descriptor(reviewed.fingerprint),
            session_checker=lambda *_args: True,
        )

    db.rollback()
    assert db.query(ControlPlaneCommandReceipt).count() == 0
    assert db.query(Submission).count() == 0
    assert db.query(ControlPlaneReviewGrant).one().consumed_at is None
    db.close()


def test_runner_admit_rechecks_expiry_after_slow_readiness(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    started_at = reviewed.now + timedelta(seconds=1)
    deadline = started_at + timedelta(seconds=30)
    command = replace(_command(reviewed), expires_at=deadline)
    current_time = [started_at]

    def clock():
        return current_time[0]

    def slow_capabilities(_settings, **_kwargs):
        current_time[0] = deadline
        return _capabilities()

    monkeypatch.setattr(
        "worker.control_plane_runner._capabilities",
        slow_capabilities,
    )
    monkeypatch.setattr(
        "worker.control_plane_runner.get_session_factory",
        lambda: factory,
    )
    runner = object.__new__(ControlPlaneRunner)
    runner._settings = _settings()
    runner._clock = clock

    with pytest.raises(ControlPlaneRunnerError, match="CONTROL_COMMAND_EXPIRED"):
        runner._admit(command)

    db = factory()
    assert db.query(ControlPlaneCommandReceipt).count() == 0
    assert db.query(Submission).count() == 0
    assert db.query(ControlPlaneReviewGrant).one().consumed_at is None
    db.close()


def test_local_admission_accepts_bounded_future_issue_without_extending_expiry(
    tmp_path,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    admission_time = reviewed.now + timedelta(seconds=1)
    deadline = admission_time + timedelta(seconds=45)
    command = replace(
        _command(reviewed),
        issued_at=admission_time + timedelta(seconds=5),
        expires_at=deadline,
    )

    db = factory()
    accepted = accept_control_plane_command(
        db,
        command,
        settings=_settings(),
        capabilities=_capabilities(),
        descriptor_resolver=lambda _url: _live_descriptor(reviewed.fingerprint),
        session_checker=lambda *_args: True,
        now=admission_time,
    )

    attempt = db.get(Submission, accepted.attempt_id)
    assert attempt.final_submit_permit is not None
    assert attempt.final_submit_permit.expires_at == deadline.replace(tzinfo=None)
    db.close()


def test_binding_drift_or_failed_admission_rolls_back_all_remote_authority(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    db = factory()
    with pytest.raises(ControlPlaneRunnerError, match="FORM_CHANGED"):
        accept_control_plane_command(
            db,
            _command(reviewed, fingerprint="a" * 64),
            settings=_settings(),
            capabilities=_capabilities(),
            descriptor_resolver=lambda _url: _live_descriptor(reviewed.fingerprint),
            session_checker=lambda *_args: True,
            now=reviewed.now + timedelta(seconds=1),
        )
    db.rollback()
    assert db.query(ControlPlaneCommandReceipt).count() == 0
    assert db.query(Submission).count() == 0

    with pytest.raises(SubmissionAdmissionError, match="ADAPTER_NOT_QUALIFIED"):
        accept_control_plane_command(
            db,
            _command(reviewed),
            settings=_settings(),
            capabilities=_capabilities(),
            descriptor_resolver=lambda _url: None,
            session_checker=lambda *_args: True,
            now=reviewed.now + timedelta(seconds=1),
        )
    verify = factory()
    grant = verify.query(ControlPlaneReviewGrant).one()
    assert grant.consumed_at is None
    assert verify.query(ControlPlaneCommandReceipt).count() == 0
    assert verify.query(ControlPlaneEventOutbox).count() == 0
    assert verify.query(Submission).count() == 0
    verify.close()
    db.close()


def test_control_plane_attempt_events_cover_redelivery_and_confirmed_evidence(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    accepted = accept_control_plane_command(
        factory(),
        _command(reviewed),
        settings=_settings(),
        capabilities=_capabilities(),
        descriptor_resolver=lambda _url: _live_descriptor(reviewed.fingerprint),
        session_checker=lambda *_args: True,
        now=reviewed.now + timedelta(seconds=1),
    )
    db = factory()
    command = db.get(SubmissionCommand, accepted.submission_command_id)
    timestamp = reviewed.now + timedelta(seconds=2)

    def project(stage, *, outcome=None, reason=None, evidence_type=None, digest=None):
        return enqueue_control_plane_attempt_transition(
            db,
            attempt=SimpleNamespace(
                stage=stage,
                outcome=outcome,
                reason_code=reason,
                verification_kind=evidence_type,
                evidence_digest=digest,
            ),
            command=command,
            occurred_at=timestamp,
        )

    project("inspecting")
    project("preparing")
    project("ready")
    reset = project("queued")
    assert reset.cycle == 1
    project("inspecting")
    project("preparing")
    project("ready")
    project("committing")
    project("verifying")
    terminal = project(
        "finished",
        outcome="confirmed_submitted",
        reason="EMPLOYER_VERIFIED",
        evidence_type="api_receipt",
        digest="d" * 64,
    )
    rows = db.query(ControlPlaneEventOutbox).order_by(ControlPlaneEventOutbox.sequence).all()
    assert [row.sequence for row in rows] == list(range(1, 12))
    assert terminal.sequence == 11
    payload = json.loads(terminal.payload_json)
    assert payload["outcome"] == "confirmed_submitted"
    assert payload["evidence_type"] == "schema_valid_receipt"
    assert payload["evidence_digest"] == "d" * 64
    assert "reason_code" not in payload
    db.close()


@pytest.mark.parametrize(
    ("outcome", "reason", "expected_reason"),
    [
        ("unknown", "FINAL_ACTION_UNCONFIRMED", "FINAL_ACTION_UNCONFIRMED"),
        ("failed_before_commit", "ALI_HAMED", "INTERNAL_ERROR"),
    ],
)
def test_nonconfirmed_terminal_events_never_carry_employer_evidence(
    tmp_path,
    outcome,
    reason,
    expected_reason,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    accepted = accept_control_plane_command(
        factory(),
        _command(reviewed),
        settings=_settings(),
        capabilities=_capabilities(),
        descriptor_resolver=lambda _url: _live_descriptor(reviewed.fingerprint),
        session_checker=lambda *_args: True,
        now=reviewed.now + timedelta(seconds=1),
    )
    db = factory()
    command = db.get(SubmissionCommand, accepted.submission_command_id)
    enqueue_control_plane_attempt_transition(
        db,
        attempt=SimpleNamespace(
            stage="inspecting",
            outcome=None,
            reason_code=None,
            verification_kind=None,
            evidence_digest=None,
        ),
        command=command,
        occurred_at=reviewed.now + timedelta(seconds=2),
    )
    terminal = enqueue_control_plane_attempt_transition(
        db,
        attempt=SimpleNamespace(
            stage="finished",
            outcome=outcome,
            reason_code=reason,
            verification_kind="api_receipt",
            evidence_digest="d" * 64,
        ),
        command=command,
        occurred_at=reviewed.now + timedelta(seconds=3),
    )
    assert terminal is not None
    payload = json.loads(terminal.payload_json)
    assert payload["outcome"] == outcome
    assert payload["reason_code"] == expected_reason
    assert "evidence_type" not in payload
    assert "evidence_digest" not in payload
    db.close()


@pytest.mark.asyncio
async def test_manual_reconciliation_emits_one_next_sequence_without_evidence(
    tmp_path,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    accepted = accept_control_plane_command(
        factory(),
        _command(reviewed),
        settings=_settings(),
        capabilities=_capabilities(),
        descriptor_resolver=lambda _url: _live_descriptor(reviewed.fingerprint),
        session_checker=lambda *_args: True,
        now=reviewed.now + timedelta(seconds=1),
    )
    db = factory()
    attempt = db.get(Submission, accepted.attempt_id)
    command = db.get(SubmissionCommand, accepted.submission_command_id)
    attempt.stage = "finished"
    attempt.outcome = "unknown"
    attempt.status = SubmissionStatus.UNKNOWN
    attempt.reason_code = "FINAL_ACTION_UNCONFIRMED"
    enqueue_control_plane_attempt_transition(
        db,
        attempt=attempt,
        command=command,
        occurred_at=reviewed.now + timedelta(seconds=2),
    )
    db.commit()

    result = await applications_route.reconcile_submission_attempt(
        attempt.id,
        applications_route.ReconcileRequest(
            outcome="confirmed_submitted",
            note="Confirmed in the candidate portal.",
            source="candidate_portal",
            reference="redacted-record",
        ),
        db,
    )
    assert result["outcome"] == "operator_confirmed"
    events = (
        db.query(ControlPlaneEventOutbox)
        .filter(ControlPlaneEventOutbox.remote_command_ref == accepted.remote_command_ref)
        .order_by(ControlPlaneEventOutbox.sequence)
        .all()
    )
    assert [event.sequence for event in events] == [1, 2, 3]
    reconciled = json.loads(events[-1].payload_json)
    assert reconciled["stage"] == "finished"
    assert reconciled["outcome"] == "operator_confirmed"
    assert "reason_code" not in reconciled
    assert "evidence_type" not in reconciled
    assert "evidence_digest" not in reconciled

    with pytest.raises(HTTPException, match="Only unknown"):
        await applications_route.reconcile_submission_attempt(
            attempt.id,
            applications_route.ReconcileRequest(
                outcome="confirmed_not_submitted",
                note="Conflicting repeat reconciliation.",
                source="candidate_portal",
            ),
            db,
        )
    db.close()


def test_runner_config_is_absolute_path_only_and_has_no_inline_secret(tmp_path):
    private_key_path = (tmp_path / "runner.key").resolve()
    public_key_path = (tmp_path / "control.pub").resolve()
    runtime_env_path = (tmp_path / "runtime.env").resolve()
    runtime_env_path.write_text("DRY_RUN=true\n", encoding="utf-8")
    config_path = (tmp_path / "runner.json").resolve()
    device_id = str(uuid4())
    control_signing_key_id = str(uuid4())
    config_path.write_text(
        json.dumps(
            {
                "control_plane_url": "https://control.example",
                "device_id": device_id,
                "control_signing_key_id": control_signing_key_id,
                "control_plane_audience": "job-apply-control-plane",
                "private_key_path": str(private_key_path),
                "control_plane_public_key_path": str(public_key_path),
                "runtime_env_path": str(runtime_env_path),
                "heartbeat_interval_seconds": 10,
                "offline_after_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_runner_config(config_path)
    assert loaded.private_key_path == private_key_path
    assert loaded.runtime_env_path == runtime_env_path
    assert "runner.key" not in repr(loaded)
    assert loaded.poll_interval_seconds == 10
    assert loaded.heartbeat_interval_seconds == 10
    assert loaded.offline_after_seconds == 30
    with pytest.raises(ControlPlaneRunnerError, match="RUNNER_POLL_INTERVAL_INVALID"):
        replace(loaded, poll_interval_seconds=4.9)
    with pytest.raises(ControlPlaneRunnerError, match="RUNNER_POLL_INTERVAL_INVALID"):
        replace(loaded, poll_interval_seconds=10.1)
    with pytest.raises(ControlPlaneRunnerError, match="RUNNER_ENV_PATH_NOT_ABSOLUTE"):
        replace(loaded, runtime_env_path=Path("runtime.env"))
    runtime_env_path.unlink()
    with pytest.raises(ControlPlaneRunnerError, match="RUNNER_ENV_UNAVAILABLE"):
        load_runner_config(config_path)

    config_path.write_text(
        json.dumps(
            {
                "control_plane_url": "https://control.example",
                "device_id": device_id,
                "control_signing_key_id": control_signing_key_id,
                "control_plane_audience": "job-apply-control-plane",
                "private_key_path": str(private_key_path),
                "control_plane_public_key_path": str(public_key_path),
                "secret": "do-not-accept",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ControlPlaneRunnerError, match="RUNNER_CONFIG_INVALID"):
        load_runner_config(config_path)


def test_runtime_file_is_authoritative_and_binds_runner_database(
    tmp_path,
    monkeypatch,
):
    device_private = Ed25519PrivateKey.generate()
    control_private = Ed25519PrivateKey.generate()
    private_path = (tmp_path / "authoritative-runner.key").resolve()
    public_path = (tmp_path / "authoritative-control.pub").resolve()
    runtime_env_path = (tmp_path / "authoritative-runtime.env").resolve()
    runtime_database_url = (
        "postgresql://runtime_user:runtime_password_1234567890@127.0.0.1:55432/runtime_job_agent"
    )
    runtime_env_path.write_text(
        "\n".join(
            (
                "APP_ENV=production",
                f"DATABASE_URL={runtime_database_url}",
                "REDIS_URL=redis://127.0.0.1:56379/0",
                "DRY_RUN=true",
                "DRAFT_ONLY=true",
                "AUTO_APPLY=false",
                "PORTAL_FINAL_SUBMIT_ENABLED=false",
                "LIVE_AUTOMATION_ACKNOWLEDGED=false",
                "TASKS_ALWAYS_EAGER=false",
                f"SECRET_KEY={'s' * 40}",
                f"WHATSAPP_APP_SECRET={'w' * 40}",
                "CORS_ORIGINS=http://127.0.0.1:8000",
                "LLM_PROVIDER=ollama",
                "LLM_MODEL=qwen2.5:7b",
                "OLLAMA_BASE_URL=http://127.0.0.1:11434",
                "OLLAMA_NO_CLOUD=true",
                "CLOUD_VISION_ENABLED=false",
                f"OLLAMA_EXPECTED_MODEL_DIGEST={_QUALIFIED_MODEL_DIGEST}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    key_material = {
        private_path: control_crypto.private_key_to_base64url(device_private).encode(),
        public_path: control_crypto.public_key_to_base64url(control_private.public_key()).encode(),
    }
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'inherited.db'}")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DRAFT_ONLY", "false")
    previous_runtime_selector = os.environ.pop(JOB_AGENT_ENV_FILE, None)
    get_settings.cache_clear()

    runner = ControlPlaneRunner(
        RunnerConfig(
            control_plane_url="https://control.example",
            device_id=str(uuid4()),
            control_signing_key_id=str(uuid4()),
            control_plane_audience=control_protocol.CONTROL_AUDIENCE,
            private_key_path=private_path,
            control_plane_public_key_path=public_path,
            runtime_env_path=runtime_env_path,
        ),
        client=object(),
        key_loader=lambda path: key_material[path],
    )
    try:
        assert runner._settings.database_url == runtime_database_url
        assert runner._settings.dry_run is True
        assert runner._settings.draft_only is True
        assert runner._database_engine.url.drivername == "postgresql"
        assert runner._database_engine.url.host == "127.0.0.1"
        assert runner._database_engine.url.port == 55432
        assert runner._database_engine.url.database == "runtime_job_agent"
        db = runner._session_factory()
        try:
            assert db.get_bind() is runner._database_engine
        finally:
            db.close()
        runtime_env_path.write_text(
            "APP_ENV=test\n"
            f"DATABASE_URL=sqlite:///{tmp_path / 'changed-after-activation.db'}\n"
            "DRY_RUN=false\n"
            "DRAFT_ONLY=false\n",
            encoding="utf-8",
        )
        globally_resolved = get_settings()
        assert globally_resolved.database_url == runtime_database_url
        assert globally_resolved.dry_run is True
        assert globally_resolved.draft_only is True
    finally:
        runner._database_engine.dispose()
        if previous_runtime_selector is None:
            os.environ.pop(JOB_AGENT_ENV_FILE, None)
        else:
            os.environ[JOB_AGENT_ENV_FILE] = previous_runtime_selector
        get_settings.cache_clear()


def test_post_commit_broker_wake_is_best_effort_and_never_replayed(monkeypatch):
    accepted = AcceptedControlCommand(
        remote_command_ref=str(uuid4()),
        remote_attempt_ref=str(uuid4()),
        application_id=1,
        attempt_id=2,
        submission_command_id=3,
        replayed=False,
    )
    calls: list[int] = []

    def unavailable(command_id):
        calls.append(command_id)
        raise ConnectionError("synthetic broker loss")

    monkeypatch.setattr(
        "worker.submission_commands.execute_submission_command_task.delay",
        unavailable,
    )
    assert wake_control_plane_submission_command(accepted) is False
    assert calls == [3]
    assert (
        wake_control_plane_submission_command(
            replace(accepted, replayed=True),
        )
        is False
    )
    assert calls == [3]


def test_runner_uses_canonical_ed25519_protocol_in_both_directions(tmp_path):
    device_private = Ed25519PrivateKey.generate()
    control_private = Ed25519PrivateKey.generate()
    private_path = (tmp_path / "runner.key").resolve()
    public_path = (tmp_path / "control.pub").resolve()
    key_material = {
        private_path: control_crypto.private_key_to_base64url(device_private).encode(),
        public_path: control_crypto.public_key_to_base64url(control_private.public_key()).encode(),
    }
    device_id = str(uuid4())
    control_signing_key_id = str(uuid4())
    config = RunnerConfig(
        control_plane_url="https://control.example",
        device_id=device_id,
        control_signing_key_id=control_signing_key_id,
        control_plane_audience=control_protocol.CONTROL_AUDIENCE,
        private_key_path=private_path,
        control_plane_public_key_path=public_path,
    )
    runner = ControlPlaneRunner(
        config,
        client=object(),
        key_loader=lambda path: key_material[path],
        settings=_settings(),
    )
    now = datetime.now(UTC)
    heartbeat = runner._signed_envelope(
        purpose=control_protocol.EnvelopePurpose.RUNNER_HEARTBEAT,
        payload={
            "boot_id": str(uuid4()),
            "release_digest": "a" * 64,
            "status": "ready",
        },
        now=now,
    )
    parsed_heartbeat = control_protocol.HeartbeatEnvelope.model_validate_json(json.dumps(heartbeat))
    control_crypto.verify_envelope(
        parsed_heartbeat,
        device_private.public_key(),
        expected_purpose=control_protocol.EnvelopePurpose.RUNNER_HEARTBEAT,
        expected_audience=control_protocol.CONTROL_AUDIENCE,
        now=now,
    )

    command_id = str(uuid4())
    command_values = {
        "protocol_version": control_protocol.PROTOCOL_VERSION,
        "key_id": control_signing_key_id,
        "purpose": control_protocol.EnvelopePurpose.CONTROL_COMMAND.value,
        "audience": control_protocol.RUNNER_AUDIENCE,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "nonce": str(uuid4()),
        "payload": {
            "command_id": command_id,
            "grant_id": str(uuid4()),
            "application_ref": str(uuid4()),
            "application_revision": 9,
            "adapter": "greenhouse",
            "adapter_version": "1.0.0",
            "form_fingerprint_digest": "f" * 64,
            "action": "send_application",
        },
    }
    unsigned = control_protocol.ControlCommandEnvelope.model_validate_json(
        json.dumps(command_values)
    )
    signed = control_crypto.sign_envelope(unsigned, control_private)
    verified = runner._verify_command(signed.model_dump(mode="json"), now=now)
    assert verified.command_id == command_id
    assert verified.application_revision == 9
    assert verified.form_fingerprint_digest == "f" * 64

    wrong_key_id_values = {**command_values, "key_id": str(uuid4())}
    wrong_key_id = control_crypto.sign_envelope(
        control_protocol.ControlCommandEnvelope.model_validate_json(
            json.dumps(wrong_key_id_values)
        ),
        control_private,
    )
    with pytest.raises(
        ControlPlaneRunnerError,
        match="CONTROL_SIGNING_KEY_ID_MISMATCH",
    ):
        runner._verify_command(wrong_key_id.model_dump(mode="json"), now=now)

    tampered = signed.model_dump(mode="json")
    tampered["payload"]["application_revision"] = 10
    with pytest.raises(ControlPlaneRunnerError, match="CONTROL_COMMAND_INVALID"):
        runner._verify_command(tampered, now=now)


@pytest.mark.asyncio
async def test_signed_kill_command_is_applied_before_any_application_poll(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'kill-first.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    device_private = Ed25519PrivateKey.generate()
    control_private = Ed25519PrivateKey.generate()
    private_path = (tmp_path / "kill-first-runner.key").resolve()
    public_path = (tmp_path / "kill-first-control.pub").resolve()
    key_material = {
        private_path: control_crypto.private_key_to_base64url(device_private).encode(),
        public_path: control_crypto.public_key_to_base64url(control_private.public_key()).encode(),
    }
    control_signing_key_id = str(uuid4())
    command_id = uuid4()
    now = datetime.now(UTC)
    unsigned = control_protocol.KillSwitchCommandEnvelope.model_validate(
        {
            "protocol_version": control_protocol.PROTOCOL_VERSION,
            "key_id": control_signing_key_id,
            "purpose": control_protocol.EnvelopePurpose.CONTROL_KILL_COMMAND,
            "audience": control_protocol.RUNNER_AUDIENCE,
            "issued_at": now,
            "expires_at": now + timedelta(minutes=5),
            "nonce": uuid4(),
            "payload": {
                "command_id": command_id,
                "action": "activate_kill_switch",
                "reason_code": "REMOTE_OPERATOR_KILL",
            },
            "signature": "",
        }
    )
    signed = control_crypto.sign_envelope(unsigned, control_private).model_dump(mode="json")
    order: list[str] = []
    acknowledgements: list[tuple[str, dict[str, object]]] = []

    class Client:
        async def poll_kill_switch_command(self, _envelope):
            order.append("kill_poll")
            return {"commands": [signed]}

        async def acknowledge_kill_switch_command(self, received_id, envelope):
            order.append("kill_ack")
            acknowledgements.append((received_id, dict(envelope)))

        async def poll_command(self, _envelope):
            raise AssertionError("application commands must wait behind the kill switch")

    runner = ControlPlaneRunner(
        RunnerConfig(
            control_plane_url="https://control.example",
            device_id=str(uuid4()),
            control_signing_key_id=control_signing_key_id,
            control_plane_audience=control_protocol.CONTROL_AUDIENCE,
            private_key_path=private_path,
            control_plane_public_key_path=public_path,
        ),
        client=Client(),
        key_loader=lambda path: key_material[path],
        settings=_settings(),
        session_factory=factory,
    )
    runner._last_heartbeat = now

    assert await runner.run_once() == "kill_switch_activated"
    assert order == ["kill_poll", "kill_ack"]
    assert acknowledgements[0][0] == str(command_id)
    ack = control_protocol.CommandAckEnvelope.model_validate(acknowledgements[0][1])
    assert ack.payload.command_id == command_id
    assert ack.payload.ack_status == control_protocol.CommandAckStatus.RECEIVED
    control_crypto.verify_envelope(
        ack,
        device_private.public_key(),
        expected_purpose=control_protocol.EnvelopePurpose.RUNNER_COMMAND_ACK,
        expected_audience=control_protocol.CONTROL_AUDIENCE,
        now=now,
    )
    db = factory()
    try:
        event = db.query(AutomationKillSwitchEvent).one()
        assert event.active is True
        assert event.source == "vercel_signed_kill"
        assert event.reason_code == "REMOTE_OPERATOR_KILL"
        assert event.command_digest is not None
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("readiness_status", "expected_status"),
    [
        ("ready", "ready"),
        ("degraded", "degraded"),
    ],
)
async def test_heartbeat_uses_readiness_report_status(
    monkeypatch,
    readiness_status,
    expected_status,
):
    runner = object.__new__(ControlPlaneRunner)
    runner._settings = _settings()
    runner._boot_id = str(uuid4())
    runner._last_heartbeat = None
    runner._protocol = SimpleNamespace(
        EnvelopePurpose=SimpleNamespace(RUNNER_HEARTBEAT="runner.heartbeat.v1")
    )
    captured: list[dict[str, object]] = []

    class Client:
        async def send_heartbeat(self, envelope):
            captured.append(dict(envelope))

    def signed_envelope(*, purpose, payload, now):
        assert purpose == "runner.heartbeat.v1"
        return {"payload": dict(payload), "issued_at": now}

    runner.client = Client()
    runner._signed_envelope = signed_envelope
    monkeypatch.setattr(
        "worker.control_plane_runner.readiness_report",
        lambda _settings, **_kwargs: {
            "status": readiness_status,
            "checks": {"database": {"ok": readiness_status == "ready"}},
        },
    )
    monkeypatch.setattr(
        "worker.control_plane_runner.get_runtime_identity",
        lambda: SimpleNamespace(release_id="a" * 64),
    )
    now = datetime.now(UTC)

    await runner._heartbeat(now)

    assert captured == [
        {
            "payload": {
                "boot_id": runner._boot_id,
                "release_digest": "a" * 64,
                "status": expected_status,
            },
            "issued_at": now,
        }
    ]
    assert runner._last_heartbeat == now


@pytest.mark.asyncio
async def test_run_once_refreshes_verification_time_after_slow_network(
    tmp_path,
    monkeypatch,
):
    device_private = Ed25519PrivateKey.generate()
    control_private = Ed25519PrivateKey.generate()
    private_path = (tmp_path / "slow-runner.key").resolve()
    public_path = (tmp_path / "slow-control.pub").resolve()
    key_material = {
        private_path: control_crypto.private_key_to_base64url(device_private).encode(),
        public_path: control_crypto.public_key_to_base64url(control_private.public_key()).encode(),
    }
    control_signing_key_id = str(uuid4())
    runner = ControlPlaneRunner(
        RunnerConfig(
            control_plane_url="https://control.example",
            device_id=str(uuid4()),
            control_signing_key_id=control_signing_key_id,
            control_plane_audience=control_protocol.CONTROL_AUDIENCE,
            private_key_path=private_path,
            control_plane_public_key_path=public_path,
        ),
        client=object(),
        key_loader=lambda path: key_material[path],
        settings=_settings(),
    )
    before_network = datetime.now(UTC)
    after_network = before_network + timedelta(seconds=31)
    command_id = str(uuid4())
    unsigned = control_protocol.ControlCommandEnvelope.model_validate(
        {
            "protocol_version": control_protocol.PROTOCOL_VERSION,
            "key_id": control_signing_key_id,
            "purpose": control_protocol.EnvelopePurpose.CONTROL_COMMAND,
            "audience": control_protocol.RUNNER_AUDIENCE,
            "issued_at": after_network,
            "expires_at": after_network + timedelta(minutes=5),
            "nonce": str(uuid4()),
            "payload": {
                "command_id": command_id,
                "grant_id": str(uuid4()),
                "application_ref": str(uuid4()),
                "application_revision": 1,
                "adapter": "greenhouse",
                "adapter_version": "1.0.0",
                "form_fingerprint_digest": "f" * 64,
                "action": "send_application",
            },
            "signature": "",
        }
    )
    envelope = control_crypto.sign_envelope(unsigned, control_private).model_dump(mode="json")

    class NetworkClock:
        calls = 0

        @classmethod
        def now(cls, _timezone):
            cls.calls += 1
            return before_network if cls.calls == 1 else after_network

    async def no_op(*_args, **_kwargs):
        return None

    async def poll(polled_at):
        assert polled_at == after_network
        return envelope

    accepted = AcceptedControlCommand(
        remote_command_ref=command_id,
        remote_attempt_ref=str(uuid4()),
        application_id=1,
        attempt_id=2,
        submission_command_id=3,
        replayed=False,
    )
    acknowledgements: list[tuple[str, str, datetime]] = []

    async def acknowledge(command_ref, *, status, now):
        acknowledgements.append((command_ref, status, now))

    runner._last_heartbeat = before_network
    runner._publish_one_review_grant = no_op
    runner._publish_one_review_grant_revocation = no_op
    runner._poll = poll
    runner._admit = lambda command: accepted
    runner._ack = acknowledge
    runner._drain_one_event = no_op
    monkeypatch.setattr("worker.control_plane_runner.datetime", NetworkClock)
    monkeypatch.setattr(
        "worker.control_plane_runner.wake_control_plane_submission_command",
        lambda _accepted: True,
    )

    assert await runner.run_once() == "accepted"
    assert NetworkClock.calls >= 4
    assert acknowledgements == [(command_id, "received", after_network)]


@pytest.mark.asyncio
async def test_review_grant_envelope_uses_exact_local_expiry(tmp_path):
    device_private = Ed25519PrivateKey.generate()
    control_private = Ed25519PrivateKey.generate()
    private_path = (tmp_path / "runner.key").resolve()
    public_path = (tmp_path / "control.pub").resolve()
    captured: list[dict[str, object]] = []

    class Client:
        async def publish_review_grant(self, envelope):
            captured.append(dict(envelope))

    key_material = {
        private_path: control_crypto.private_key_to_base64url(device_private).encode(),
        public_path: control_crypto.public_key_to_base64url(control_private.public_key()).encode(),
    }
    runner = ControlPlaneRunner(
        RunnerConfig(
            control_plane_url="https://control.example",
            device_id=str(uuid4()),
            control_signing_key_id=str(uuid4()),
            control_plane_audience=control_protocol.CONTROL_AUDIENCE,
            private_key_path=private_path,
            control_plane_public_key_path=public_path,
        ),
        client=Client(),
        key_loader=lambda path: key_material[path],
        settings=_settings(),
    )
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=4)
    grant = ReviewGrantProjection(
        remote_application_ref=str(uuid4()),
        review_grant_ref=str(uuid4()),
        application_revision=3,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-v1",
        form_fingerprint_digest="f" * 64,
        runner_release="a" * 64,
        issued_at=now,
        expires_at=expires_at,
    )
    await runner.publish_review_grant(grant)
    assert len(captured) == 1
    envelope = control_protocol.ReviewGrantEnvelope.model_validate_json(json.dumps(captured[0]))
    assert envelope.expires_at == expires_at
    control_crypto.verify_envelope(
        envelope,
        device_private.public_key(),
        expected_purpose=control_protocol.EnvelopePurpose.RUNNER_REVIEW_GRANT,
        expected_audience=control_protocol.CONTROL_AUDIENCE,
        now=now,
    )


@pytest.mark.asyncio
async def test_review_grant_revocation_is_exact_signed_and_outbound_only(tmp_path):
    device_private = Ed25519PrivateKey.generate()
    control_private = Ed25519PrivateKey.generate()
    private_path = (tmp_path / "revocation-runner.key").resolve()
    public_path = (tmp_path / "revocation-control.pub").resolve()
    captured: list[dict[str, object]] = []

    class Client:
        async def revoke_review_grant(self, envelope):
            captured.append(dict(envelope))

    key_material = {
        private_path: control_crypto.private_key_to_base64url(device_private).encode(),
        public_path: control_crypto.public_key_to_base64url(control_private.public_key()).encode(),
    }
    runner = ControlPlaneRunner(
        RunnerConfig(
            control_plane_url="https://control.example",
            device_id=str(uuid4()),
            control_signing_key_id=str(uuid4()),
            control_plane_audience=control_protocol.CONTROL_AUDIENCE,
            private_key_path=private_path,
            control_plane_public_key_path=public_path,
        ),
        client=Client(),
        key_loader=lambda path: key_material[path],
        settings=_settings(),
    )
    now = datetime.now(UTC)
    revocation = ReviewGrantRevocationProjection(
        remote_application_ref=str(uuid4()),
        review_grant_ref=str(uuid4()),
        application_revision=3,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        form_fingerprint_digest="f" * 64,
        reviewed_at=now - timedelta(seconds=10),
        grant_expires_at=now + timedelta(minutes=4),
        revoked_at=now - timedelta(seconds=1),
    )

    await runner.publish_review_grant_revocation(revocation)

    assert len(captured) == 1
    envelope = control_protocol.ReviewGrantRevocationEnvelope.model_validate_json(
        json.dumps(captured[0])
    )
    assert str(envelope.payload.grant_id) == revocation.review_grant_ref
    assert str(envelope.payload.application_ref) == revocation.remote_application_ref
    assert envelope.payload.grant_expires_at == revocation.grant_expires_at
    assert envelope.payload.revoked_at == revocation.revoked_at
    control_crypto.verify_envelope(
        envelope,
        device_private.public_key(),
        expected_purpose=(control_protocol.EnvelopePurpose.RUNNER_REVIEW_GRANT_REVOCATION),
        expected_audience=control_protocol.CONTROL_AUDIENCE,
        now=now,
    )


@pytest.mark.asyncio
async def test_run_once_drains_revocations_before_grants_and_polling():
    runner = object.__new__(ControlPlaneRunner)
    runner._last_heartbeat = datetime.now(UTC)
    runner.config = SimpleNamespace(heartbeat_interval_seconds=10)
    order: list[str] = []
    revocations = iter((True, True, False))

    async def revoke():
        order.append("revoke")
        return next(revocations)

    async def grant():
        order.append("grant")
        return False

    async def poll(_now):
        order.append("poll")
        return None

    async def drain():
        order.append("events")

    runner._publish_one_review_grant_revocation = revoke
    runner._publish_one_review_grant = grant
    runner._poll = poll
    runner._drain_one_event = drain

    assert await runner.run_once() == "idle"
    assert order == ["revoke", "revoke", "revoke", "grant", "poll", "events"]


@pytest.mark.asyncio
async def test_run_once_defers_new_authority_when_revocation_batch_is_full():
    runner = object.__new__(ControlPlaneRunner)
    runner._last_heartbeat = datetime.now(UTC)
    runner.config = SimpleNamespace(heartbeat_interval_seconds=10)
    revocations = 0

    async def revoke():
        nonlocal revocations
        revocations += 1
        return True

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("new authority must wait for the revocation backlog")

    runner._publish_one_review_grant_revocation = revoke
    runner._publish_one_review_grant = forbidden
    runner._poll = forbidden

    assert await runner.run_once() == "revocations_draining"
    assert revocations == MAX_REVOCATIONS_PER_CYCLE


@pytest.mark.asyncio
async def test_unaccepted_review_grant_receipt_releases_projection_for_retry(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    monkeypatch.setattr(
        "worker.control_plane_runner.get_session_factory",
        lambda: factory,
    )
    device_private = Ed25519PrivateKey.generate()
    control_private = Ed25519PrivateKey.generate()
    private_path = (tmp_path / "retry-runner.key").resolve()
    public_path = (tmp_path / "retry-control.pub").resolve()
    key_material = {
        private_path: control_crypto.private_key_to_base64url(device_private).encode(),
        public_path: control_crypto.public_key_to_base64url(control_private.public_key()).encode(),
    }

    class Client:
        async def publish_review_grant(self, _envelope):
            raise ControlPlaneClientError("CONTROL_PLANE_RECEIPT_INVALID")

    runner = ControlPlaneRunner(
        RunnerConfig(
            control_plane_url="https://control.example",
            device_id=str(uuid4()),
            control_signing_key_id=str(uuid4()),
            control_plane_audience=control_protocol.CONTROL_AUDIENCE,
            private_key_path=private_path,
            control_plane_public_key_path=public_path,
        ),
        client=Client(),
        key_loader=lambda path: key_material[path],
        settings=_settings(),
        session_factory=factory,
    )

    with pytest.raises(
        ControlPlaneClientError,
        match="CONTROL_PLANE_RECEIPT_INVALID",
    ):
        await runner._publish_one_review_grant()

    db = factory()
    try:
        grant = db.query(ControlPlaneReviewGrant).one()
        assert grant.grant_ref == reviewed.projection.review_grant_ref
        assert grant.projection_state == "pending"
        assert grant.projected_at is None
        assert grant.projection_claim_token is None
        assert grant.last_projection_error_code == "CONTROL_PLANE_DELIVERY_FAILED"
    finally:
        db.close()
