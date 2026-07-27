"""End-to-end safety tests for the database-authoritative submission command."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from celery.result import denied_join_result
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from api.routes import applications as applications_route
from core.config import Settings
from core.runtime_identity import get_runtime_identity
from core.submission_domain import (
    AlreadyAppliedOutcome,
    ConfirmedSubmittedOutcome,
    EvidenceType,
    NeedsReviewOutcome,
    PreparedFinalActionV1,
    ReasonCode,
)
from core.submission_domain import (
    SubmissionEvidence as DomainSubmissionEvidence,
)
from core.submission_service import (
    ClientReleaseIdentity,
    SubmissionAdmissionError,
    SubmissionCommandRequest,
    create_submission_commands,
)
from core.submission_truth import is_employer_verified
from db.models import (
    Application,
    Base,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
    SubmissionEvidence,
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
from worker.submission_attempts import mark_stale_attempts_unknown
from worker.submission_commands import (
    _CommitBoundaryRejectedError,
    _enter_commit_boundary,
    _finish_claimed_before_commit,
    claim_submission_command,
    drain_submission_commands_task,
    execute_claimed_submission_command,
    reconcile_stale_submission_commands,
)

_QUALIFIED_MODEL_DIGEST = load_qualified_local_model().digest


@pytest.fixture(autouse=True)
def _current_qualification_report(monkeypatch):
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: True,
    )


class _AllowGovernor:
    reservations = 0

    def can_act(self):
        return True, "ok"

    def can_apply_linkedin(self):
        return True, "ok"

    def record_application(self):
        return None

    def reserve_final_action(self, *, reservation_id, platform):
        del reservation_id, platform
        self.reservations += 1
        return True, "reserved"


class _DenyGovernor(_AllowGovernor):
    def __init__(self, reason: str):
        self.reason = reason

    def can_act(self):
        return False, self.reason

    def can_apply_linkedin(self):
        return False, self.reason

    def reserve_final_action(self, *, reservation_id, platform):
        del reservation_id, platform
        return False, self.reason


class _UnavailableGovernor(_AllowGovernor):
    def reserve_final_action(self, *, reservation_id, platform):
        del reservation_id, platform
        raise ConnectionError("synthetic Redis loss")


def _ready_action(plan, permit, *, prepared_at: datetime | None = None):
    timestamp = prepared_at or datetime.now(UTC)
    return PreparedFinalActionV1(
        attempt_id=permit.attempt_id,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        form_fingerprint=plan.form_fingerprint,
        attached_cv_hash=plan.attached_cv_hash,
        prepared_at=timestamp,
        expires_at=min(timestamp + timedelta(minutes=1), permit.expires_at),
        action_nonce="9" * 64,
    )


def _action_for_attempt(attempt, *, prepared_at: datetime):
    permit = attempt.final_submit_permit
    return PreparedFinalActionV1(
        attempt_id=attempt.id,
        adapter_name=attempt.adapter_name,
        adapter_version=attempt.adapter_version,
        selector_version=attempt.selector_version,
        form_fingerprint=attempt.form_plan_fingerprint,
        attached_cv_hash=attempt.attached_cv_hash,
        prepared_at=prepared_at.replace(tzinfo=UTC),
        expires_at=permit.expires_at.replace(tzinfo=UTC),
        action_nonce="8" * 64,
    )


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'command-kernel.db'}")
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


def _client_release() -> ClientReleaseIdentity:
    identity = get_runtime_identity()
    return ClientReleaseIdentity(
        build_sha=identity.build_sha,
        ui_asset_digest=identity.ui_asset_digest,
        source_digest=identity.source_digest,
        protocol_version=identity.protocol_version,
        boot_id=identity.boot_id,
    )


def _live_settings() -> Settings:
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


def _live_descriptor(fingerprint: str):
    descriptor = adapter_for_url("https://boards.greenhouse.io/acme/jobs/123")
    assert descriptor is not None
    return replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(fingerprint,),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    )


def test_outbox_drainer_executes_directly_inside_task_context(monkeypatch):
    """Celery tasks must not wait on nested task results in a worker process."""

    class Session:
        closed = False

        def close(self):
            self.closed = True

    session = Session()
    calls = []
    monkeypatch.setattr(
        "worker.submission_commands.get_session_factory",
        lambda: lambda: session,
    )
    command_ids = iter((41, None))

    def claim(db):
        command_id = next(command_ids)
        calls.append(("claim", db, command_id))
        return command_id

    monkeypatch.setattr("worker.submission_commands.claim_submission_command", claim)
    monkeypatch.setattr(
        "worker.submission_commands.execute_claimed_submission_command",
        lambda db, command_id: calls.append(("execute", db, command_id)) or "draft_only",
    )

    with denied_join_result():
        assert drain_submission_commands_task.run() == 1

    assert calls == [
        ("claim", session, 41),
        ("execute", session, 41),
        ("claim", session, None),
    ]
    assert session.closed is True


def test_outbox_drainer_recovers_a_lost_wake_batch(monkeypatch):
    class Session:
        def close(self):
            return None

    session = Session()
    command_ids = iter((11, 12, 13, None))
    executed = []
    monkeypatch.setattr(
        "worker.submission_commands.get_session_factory",
        lambda: lambda: session,
    )
    monkeypatch.setattr(
        "worker.submission_commands.claim_submission_command",
        lambda _db: next(command_ids),
    )
    monkeypatch.setattr(
        "worker.submission_commands.execute_claimed_submission_command",
        lambda _db, command_id: executed.append(command_id),
    )

    assert drain_submission_commands_task.run() == 3
    assert executed == [11, 12, 13]


def _reviewed_application(factory, *, now: datetime | None = None):
    timestamp = now or datetime.now(UTC).replace(tzinfo=None)
    fingerprint = "f" * 64
    cv_hash = "c" * 64
    db = factory()
    job = Job(
        title="Evidence Engineer",
        company="Acme",
        source_url="https://boards.greenhouse.io/acme/jobs/123",
        apply_url="https://boards.greenhouse.io/acme/jobs/123",
        status=JobStatus.DRAFT,
    )
    application = Application(
        job=job,
        status=JobStatus.DRAFT,
        selected_cv_id="cv-ai",
        selected_cv_hash=cv_hash,
        profile_version=1,
        material_eligible=True,
        material_blockers_json="[]",
        material_model_provider="ollama",
        material_model_name="qwen2.5:7b",
        material_model_digest=_QUALIFIED_MODEL_DIGEST,
        material_prompt_version="application-materials-v1",
        revision=1,
        prepared_revision=1,
        approved_at=timestamp,
        approval_source="manual_prepare",
    )
    db.add(application)
    db.flush()
    if db.query(UserProfileVersion.id).filter(UserProfileVersion.version == 1).first() is None:
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
        adapter_name="greenhouse",
        adapter_version=adapter_for_url(job.apply_url).adapter_version,
        selector_version=adapter_for_url(job.apply_url).selector_version,
        fingerprint=fingerprint,
        selected_cv_id="cv-ai",
        selected_cv_hash=cv_hash,
        attached_cv_id="cv-ai",
        attached_cv_hash=cv_hash,
        attachment_verified=True,
        profile_version=1,
        fields_json="[]",
        decisions_json="[]",
        blockers_json="[]",
        session_verified_at=timestamp,
        created_at=timestamp,
        expires_at=timestamp + timedelta(minutes=30),
    )
    db.add(plan)
    db.commit()
    result = SimpleNamespace(
        application_id=application.id,
        job_id=job.id,
        plan_id=plan.plan_id,
        plan_db_id=plan.id,
        fingerprint=fingerprint,
        cv_hash=cv_hash,
        now=timestamp,
    )
    db.close()
    return result


def _admit(
    factory,
    reviewed,
    *,
    key: str = "operator-click-1",
    client_release: ClientReleaseIdentity | None = None,
    settings: Settings | None = None,
    capabilities: dict[str, object] | None = None,
):
    db = factory()
    try:
        [created] = create_submission_commands(
            db,
            [
                SubmissionCommandRequest(
                    application_id=reviewed.application_id,
                    client_idempotency_key=key,
                    application_revision=1,
                    form_plan_id=reviewed.plan_id,
                    client_release=client_release or _client_release(),
                )
            ],
            settings=settings or _live_settings(),
            capabilities=capabilities or _capabilities(),
            descriptor_resolver=lambda _url: _live_descriptor(reviewed.fingerprint),
            session_checker=lambda *_args: True,
            now=reviewed.now,
        )
        return created
    finally:
        db.close()


def test_admission_atomically_creates_attempt_permit_and_outbox(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)

    created = _admit(factory, reviewed)
    replay = _admit(factory, reviewed)

    assert replay == replace(created, replayed=True)
    db = factory()
    attempt = db.get(Submission, created.attempt_id)
    command = db.get(SubmissionCommand, created.command_id)
    assert attempt.stage == "queued"
    assert attempt.outcome is None
    assert attempt.status == SubmissionStatus.PENDING
    assert attempt.final_submit_permit is not None
    assert attempt.final_submit_permit.consumed_at is None
    assert attempt.runner_release == get_runtime_identity().release_id
    assert command.state == "pending"
    assert command.attempt_id == attempt.id
    assert db.query(Submission).count() == 1
    assert db.query(SubmissionCommand).count() == 1
    db.close()


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        (
            lambda application: setattr(application, "material_eligible", False),
            "MATERIAL_NOT_ELIGIBLE",
        ),
        (
            lambda application: setattr(application, "selected_cv_hash", "d" * 64),
            "ATTACHMENT_CHANGED",
        ),
    ],
)
def test_admission_requires_current_evidence_bound_materials(
    tmp_path,
    mutation,
    reason_code,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    db = factory()
    mutation(db.get(Application, reviewed.application_id))
    db.commit()
    db.close()

    with pytest.raises(SubmissionAdmissionError) as exc_info:
        _admit(factory, reviewed)

    assert exc_info.value.reason_code == reason_code
    verify = factory()
    assert verify.query(Submission).count() == 0
    assert verify.query(SubmissionCommand).count() == 0
    verify.close()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("material_prompt_version", "application-materials-stale"),
        ("material_model_provider", "local-test"),
        ("material_model_name", "qwen2.5:3b"),
    ],
)
def test_admission_rejects_unqualified_material_identity(tmp_path, attribute, value):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    db = factory()
    application = db.get(Application, reviewed.application_id)
    assert application.material_prompt_version == MATERIAL_PROMPT_VERSION
    setattr(application, attribute, value)
    db.commit()
    db.close()

    with pytest.raises(SubmissionAdmissionError) as exc_info:
        _admit(factory, reviewed)

    assert exc_info.value.reason_code == "MATERIAL_MODEL_NOT_QUALIFIED"


def test_admission_rejects_unqualified_runtime_model_digest(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    capabilities = _capabilities()
    capabilities["llm"] = {
        **capabilities["llm"],
        "digest": f"sha256:{'b' * 64}",
    }

    with pytest.raises(SubmissionAdmissionError) as exc_info:
        _admit(factory, reviewed, capabilities=capabilities)

    assert exc_info.value.reason_code == "RUNTIME_NOT_READY"
    db = factory()
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.close()


@pytest.mark.parametrize(
    ("client_release", "reason_code"),
    [
        (replace(_client_release(), boot_id=str(uuid4())), "BUILD_MISMATCH"),
        (
            replace(_client_release(), source_digest="sha256:" + "0" * 64),
            "BUILD_MISMATCH",
        ),
        (
            replace(_client_release(), protocol_version="submission-control.stale"),
            "PROTOCOL_MISMATCH",
        ),
    ],
)
def test_stale_dashboard_release_cannot_create_a_command(
    tmp_path,
    client_release,
    reason_code,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)

    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed, client_release=client_release)

    assert exc.value.reason_code == reason_code
    db = factory()
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.close()


def test_injected_capabilities_cannot_bypass_weak_operator_auth(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    weak_settings = _live_settings().model_copy(
        update={
            "app_env": "development",
            "secret_key": "change-me",
        }
    )

    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed, settings=weak_settings)

    assert exc.value.reason_code == "OPERATOR_AUTH_REQUIRED"
    db = factory()
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.close()


@pytest.mark.asyncio
async def test_submit_endpoints_reject_weak_dev_auth_before_admission(monkeypatch):
    weak_settings = _live_settings().model_copy(
        update={
            "app_env": "development",
            "secret_key": "change-me",
        }
    )
    create = MagicMock()
    monkeypatch.setattr(applications_route, "get_settings", lambda: weak_settings)
    monkeypatch.setattr(applications_route, "create_submission_commands", create)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/applications/1/submit",
            "headers": [(b"authorization", b"Bearer change-me")],
        }
    )
    release = _client_release()
    release_payload = applications_route.ClientReleaseIdentityRequest(
        build_sha=release.build_sha,
        ui_asset_digest=release.ui_asset_digest,
        source_digest=release.source_digest,
        protocol_version=release.protocol_version,
        boot_id=release.boot_id,
    )
    payload = applications_route.SubmitApplicationRequest(
        acknowledgement="SEND_APPLICATION",
        idempotency_key="weak-auth-command",
        application_revision=1,
        form_plan_id=str(uuid4()),
        client_release=release_payload,
    )

    with pytest.raises(HTTPException) as exc:
        await applications_route.submit_application(
            1,
            payload,
            request,
            db=MagicMock(),
        )

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "OPERATOR_AUTH_REQUIRED"

    batch_payload = applications_route.BatchSubmitRequest(
        acknowledgement="SEND_SELECTED_APPLICATIONS",
        applications=[
            applications_route.BatchSubmitItem(
                application_id=1,
                idempotency_key="weak-auth-batch-command",
                application_revision=1,
                form_plan_id=str(uuid4()),
            )
        ],
        client_release=release_payload,
    )
    with pytest.raises(HTTPException) as batch_exc:
        await applications_route.batch_submit_applications(
            batch_payload,
            request,
            db=MagicMock(),
        )

    assert batch_exc.value.status_code == 503
    assert batch_exc.value.detail["code"] == "OPERATOR_AUTH_REQUIRED"
    create.assert_not_called()


def test_missing_profile_snapshot_cannot_create_a_command(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    db = factory()
    db.query(UserProfileVersion).delete()
    db.commit()
    db.close()

    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed)

    assert exc.value.reason_code == "PROFILE_VERSION_NOT_FOUND"
    db = factory()
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.close()


def test_malformed_profile_snapshot_cannot_create_a_command(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    db = factory()
    db.query(UserProfileVersion).update({"profile_yaml": "[]"})
    db.commit()
    db.close()

    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed)

    assert exc.value.reason_code == "PROFILE_SNAPSHOT_INVALID"


def test_second_click_with_different_key_cannot_create_another_action(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    _admit(factory, reviewed, key="first-click")

    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed, key="second-click")

    assert exc.value.reason_code == "SUBMISSION_ALREADY_ACTIVE"
    db = factory()
    assert db.query(Submission).count() == 1
    assert db.query(SubmissionCommand).count() == 1
    db.close()


def test_already_applied_outcome_can_never_be_resubmitted(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    attempt = db.get(Submission, created.attempt_id)
    attempt.stage = "finished"
    attempt.outcome = "already_applied"
    attempt.status = SubmissionStatus.FAILED
    attempt.reason_code = "ALREADY_APPLIED"
    attempt.finished_at = datetime.now(UTC).replace(tzinfo=None)
    attempt.command.state = "completed"
    attempt.command.completed_at = attempt.finished_at
    attempt.application.status = JobStatus.SUBMITTED
    attempt.application.job.status = JobStatus.SUBMITTED
    db.commit()
    db.close()

    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed, key="resubmit-already-applied")

    assert exc.value.reason_code == "APPLICATION_NOT_ELIGIBLE"


def test_older_unknown_history_blocks_admission_after_a_later_draft(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    db = factory()
    application = db.get(Application, reviewed.application_id)
    application.approval_source = "retry_prepare"
    db.add_all(
        [
            Submission(
                application_id=application.id,
                attempt_number=1,
                idempotency_key="historical-unknown",
                submitter_name="greenhouse",
                status=SubmissionStatus.UNKNOWN,
                stage="finished",
                outcome="unknown",
                reason_code="FINAL_ACTION_UNCONFIRMED",
            ),
            Submission(
                application_id=application.id,
                attempt_number=2,
                idempotency_key="historical-draft",
                submitter_name="greenhouse",
                status=SubmissionStatus.DRAFT_ONLY,
                stage="finished",
                outcome="draft_only",
                reason_code="DRY_RUN_DISCARDED",
            ),
        ]
    )
    db.commit()
    db.close()

    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed, key="blocked-by-older-unknown")

    assert exc.value.reason_code == "APPLICATION_NOT_ELIGIBLE"
    verify = factory()
    assert verify.query(Submission).count() == 2
    assert verify.query(SubmissionCommand).count() == 0
    verify.close()


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        (lambda app, plan: setattr(app, "revision", 2), "APPLICATION_REVISION_CHANGED"),
        (lambda app, plan: setattr(app, "prepared_revision", None), "APPLICATION_REVIEW_REQUIRED"),
        (
            lambda app, plan: setattr(
                plan,
                "blockers_json",
                '["REQUIRED_FIELD_UNKNOWN"]',
            ),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda app, plan: setattr(plan, "attachment_verified", False),
            "ATTACHMENT_UNVERIFIED",
        ),
        (lambda app, plan: setattr(plan, "session_verified_at", None), "SESSION_EXPIRED"),
        (
            lambda app, plan: setattr(
                plan,
                "session_verified_at",
                plan.created_at + timedelta(minutes=10),
            ),
            "SESSION_EXPIRED",
        ),
        (
            lambda app, plan: setattr(
                plan,
                "invalidated_at",
                datetime.now(UTC).replace(tzinfo=None),
            ),
            "FORM_CHANGED",
        ),
    ],
)
def test_admission_fails_closed_without_partial_rows(
    tmp_path,
    mutation,
    reason_code,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    db = factory()
    application = db.get(Application, reviewed.application_id)
    plan = db.get(FormPlan, reviewed.plan_db_id)
    mutation(application, plan)
    db.commit()
    db.close()

    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed)

    assert exc.value.reason_code == reason_code
    db = factory()
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.close()


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        (
            lambda plan, reviewed: setattr(plan, "fields_json", "{"),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: setattr(plan, "decisions_json", "{}"),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: setattr(
                plan,
                "fields_json",
                (
                    '[{"field_id":"required-one","label":"Required",'
                    '"field_type":"text","required":true,"position":0}]'
                ),
            ),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: (
                setattr(
                    plan,
                    "fields_json",
                    (
                        '[{"field_id":"nationality","label":"Nationality",'
                        '"field_type":"text","required":true,"position":0,'
                        '"sensitive_category":"nationality"}]'
                    ),
                ),
                setattr(
                    plan,
                    "decisions_json",
                    (
                        '[{"field_id":"nationality","disposition":"resolved",'
                        '"provenance":"local_llm","value":"unsupported",'
                        '"evidence_refs":["profile:1"]}]'
                    ),
                ),
            ),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: (
                setattr(
                    plan,
                    "fields_json",
                    (
                        '[{"field_id":"nationality","label":"Nationality",'
                        '"field_type":"text","required":true,"position":0,'
                        '"sensitive_category":"nationality"}]'
                    ),
                ),
                setattr(
                    plan,
                    "decisions_json",
                    (
                        '[{"field_id":"nationality","disposition":"resolved",'
                        '"provenance":"user_confirmed","value":"confirmed"}]'
                    ),
                ),
            ),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: setattr(
                plan,
                "fields_json",
                (
                    '[{"field_id":"field-one","field_id":"field-two",'
                    '"label":"Duplicate","field_type":"text",'
                    '"required":false,"position":0}]'
                ),
            ),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: setattr(
                plan,
                "fields_json",
                (
                    '[{"field_id":"field-one","label":"Extra",'
                    '"field_type":"text","required":false,"position":0,'
                    '"unreviewed_data":"must-not-pass"}]'
                ),
            ),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: setattr(plan, "selected_cv_hash", "C" * 64),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: setattr(plan, "fingerprint", "g" * 64),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: setattr(
                plan,
                "expires_at",
                plan.created_at + timedelta(minutes=30, seconds=1),
            ),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: (
                setattr(plan, "plan_id", reviewed.plan_id.upper()),
                setattr(reviewed, "plan_id", reviewed.plan_id.upper()),
            ),
            "FORM_PLAN_BLOCKED",
        ),
        (
            lambda plan, reviewed: setattr(plan, "attached_cv_id", "cv-other"),
            "ATTACHMENT_UNVERIFIED",
        ),
    ],
    ids=[
        "malformed-fields-json",
        "non-array-decisions-json",
        "unresolved-required-field",
        "sensitive-llm-answer",
        "sensitive-answer-without-evidence",
        "duplicate-json-key",
        "unknown-schema-property",
        "invalid-sha256",
        "invalid-form-fingerprint",
        "overlong-plan-ttl",
        "noncanonical-uuid",
        "not-ready-for-permit",
    ],
)
def test_admission_revalidates_the_persisted_immutable_form_plan(
    tmp_path,
    mutation,
    reason_code,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    db = factory()
    plan = db.get(FormPlan, reviewed.plan_db_id)
    mutation(plan, reviewed)
    db.commit()
    assert not applications_route._form_plan_valid(
        plan,
        db.get(Application, reviewed.application_id),
    )
    db.close()

    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed)

    assert exc.value.reason_code == reason_code
    db = factory()
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.close()


def test_claiming_one_command_is_idempotent(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)

    first = factory()
    second = factory()
    try:
        assert (
            claim_submission_command(
                first,
                command_id=created.command_id,
                runner_id="runner-one",
            )
            == created.command_id
        )
        assert (
            claim_submission_command(
                second,
                command_id=created.command_id,
                runner_id="runner-two",
            )
            is None
        )
    finally:
        first.close()
        second.close()

    db = factory()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "inspecting"
    assert attempt.status == SubmissionStatus.PENDING
    assert attempt.command.claimed_by == "runner-one"
    db.close()


def test_worker_build_mismatch_finishes_before_external_action(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    attempt = db.get(Submission, created.attempt_id)
    attempt.runner_release = "different-runner-release"
    db.commit()

    assert claim_submission_command(db, command_id=created.command_id) is None
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "finished"
    assert attempt.outcome == "failed_before_commit"
    assert attempt.reason_code == "BUILD_MISMATCH"
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    db.close()


def test_worker_source_drift_finishes_claim_before_external_action(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    monkeypatch.setattr(
        "worker.submission_commands.runtime_source_is_current",
        lambda *_args, **_kwargs: False,
    )

    assert claim_submission_command(db, command_id=created.command_id) is None

    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.outcome == "failed_before_commit"
    assert attempt.reason_code == "BUILD_MISMATCH"
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    db.close()


def test_worker_source_drift_at_commit_boundary_never_calls_executor_commit(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _ConfirmedExecutor()
    monkeypatch.setattr(
        "worker.submission_commands.runtime_source_is_current",
        lambda *_args, **_kwargs: False,
    )

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=_live_settings(),
        governor=_AllowGovernor(),
    )

    assert result == "failed_before_commit"
    assert executor.committed is False
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.reason_code == "BUILD_MISMATCH"
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    db.close()


def test_missing_final_executor_finishes_before_commit_without_consuming_permit(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=SimpleNamespace(resolve_final_executor=lambda *_args: None),
        settings=_live_settings(),
        governor=_AllowGovernor(),
    )

    assert result == "failed_before_commit"
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "finished"
    assert attempt.outcome == "failed_before_commit"
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    assert attempt.submitted_at is None
    assert not is_employer_verified(attempt)
    db.close()
    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed, key="direct-resubmit-without-retry")
    assert exc.value.reason_code == "APPLICATION_NOT_ELIGIBLE"


def test_worker_effective_mode_is_rechecked_before_external_action(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _ConfirmedExecutor()

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=Settings(_env_file=None, dry_run=True),
        governor=_AllowGovernor(),
    )

    assert result == "failed_before_commit"
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.reason_code == "RUNTIME_NOT_READY"
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    assert db.query(SubmissionEvidence).count() == 0
    db.close()


class _ConfirmedExecutor:
    committed = False

    async def preflight(self, *, plan, permit):
        self.plan = plan
        return _ready_action(plan, permit)

    async def commit(self, *, action, permit):
        self.committed = True
        return ConfirmedSubmittedOutcome(
            evidence=DomainSubmissionEvidence(
                attempt_id=permit.attempt_id,
                evidence_type=EvidenceType.EMPLOYER_APPLICATION_ID,
                employer_application_id="opaque-employer-ref",
                form_fingerprint=action.form_fingerprint,
                attached_cv_hash=action.attached_cv_hash,
                observed_at=datetime.now(UTC),
                digest="e" * 64,
            )
        )


class _SameLoopExecutor(_ConfirmedExecutor):
    def __init__(self, *, crash_after_boundary: bool = False):
        self.crash_after_boundary = crash_after_boundary
        self.committed = False
        self.async_calls: list[tuple[str, int, int]] = []
        self.cleaned_action = None

    def _record_async_call(self, name: str) -> None:
        self.async_calls.append(
            (
                name,
                id(asyncio.get_running_loop()),
                threading.get_ident(),
            )
        )

    async def preflight(self, *, plan, permit):
        self._record_async_call("preflight")
        return await super().preflight(plan=plan, permit=permit)

    async def commit(self, *, action, permit):
        self._record_async_call("commit")
        self.committed = True
        if self.crash_after_boundary:
            raise RuntimeError("synthetic same-loop post-boundary crash")
        return await super().commit(action=action, permit=permit)

    async def cleanup_prepared_action(self, *, action):
        self._record_async_call("cleanup")
        self.cleaned_action = action


class _ContextAwareSameLoopExecutor(_SameLoopExecutor):
    preflight_context = None

    async def preflight(self, *, plan, permit, context):
        self.preflight_context = context
        return await super().preflight(plan=plan, permit=permit)


class _LLMCallingExecutor:
    """A broken adapter must be unable to invoke an LLM in final execution."""

    committed = False

    async def preflight(self, *, plan, permit):
        del plan, permit
        from llm.client import OllamaClient

        await OllamaClient().generate("this request must be rejected before transport")
        raise AssertionError("the final-stage LLM guard did not reject generation")

    async def commit(self, *, action, permit):
        del action, permit
        self.committed = True
        raise AssertionError("commit must not run after prohibited preflight inference")


class _CrashingExecutor:
    async def preflight(self, *, plan, permit):
        return _ready_action(plan, permit)

    async def commit(self, *, action, permit):
        del action, permit
        raise RuntimeError("synthetic post-commit crash")


class _PreActionEvidenceExecutor:
    async def preflight(self, *, plan, permit):
        self.plan = plan
        return _ready_action(plan, permit)

    async def commit(self, *, action, permit):
        return ConfirmedSubmittedOutcome(
            evidence=DomainSubmissionEvidence(
                attempt_id=permit.attempt_id,
                evidence_type=EvidenceType.EMPLOYER_APPLICATION_ID,
                employer_application_id="pre-action-ref",
                form_fingerprint=action.form_fingerprint,
                attached_cv_hash=action.attached_cv_hash,
                observed_at=self.plan.created_at,
                digest="d" * 64,
            )
        )


class _MismatchedEvidenceExecutor:
    async def preflight(self, *, plan, permit):
        return _ready_action(plan, permit)

    async def commit(self, *, action, permit):
        return ConfirmedSubmittedOutcome(
            evidence=DomainSubmissionEvidence(
                attempt_id=permit.attempt_id,
                evidence_type=EvidenceType.EMPLOYER_APPLICATION_ID,
                employer_application_id="wrong-form-ref",
                form_fingerprint="0" * 64,
                attached_cv_hash=action.attached_cv_hash,
                observed_at=datetime.now(UTC),
                digest="a" * 64,
            )
        )


class _DefinitivePreflightExecutor:
    def __init__(self, outcome):
        self.outcome = outcome
        self.committed = False

    async def preflight(self, *, plan, permit):
        del plan, permit
        return self.outcome

    async def commit(self, *, action, permit):
        del action, permit
        self.committed = True
        raise AssertionError("commit must not run after a definitive preflight outcome")


class _MismatchedActionExecutor:
    committed = False

    async def preflight(self, *, plan, permit):
        action = _ready_action(plan, permit)
        return action.model_copy(update={"form_fingerprint": "0" * 64})

    async def commit(self, *, action, permit):
        del action, permit
        self.committed = True
        raise AssertionError("commit must not run with a mismatched action handle")


def _registry(executor):
    return SimpleNamespace(resolve_final_executor=lambda *_args: executor)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            NeedsReviewOutcome(reason_code=ReasonCode.SESSION_EXPIRED),
            "needs_review",
        ),
        (AlreadyAppliedOutcome(), "already_applied"),
    ],
)
def test_definitive_preflight_outcome_never_crosses_commit_boundary(
    tmp_path,
    outcome,
    expected,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _DefinitivePreflightExecutor(outcome)

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=_live_settings(),
        governor=_AllowGovernor(),
    )

    assert result == expected
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "finished"
    assert attempt.outcome == expected
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    assert executor.committed is False
    assert not is_employer_verified(attempt)
    db.close()


@pytest.mark.parametrize("reason", ["kill switch active", "in challenge cooldown"])
def test_governor_toggled_after_admission_blocks_the_final_action(tmp_path, reason):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _ConfirmedExecutor()

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=_live_settings(),
        governor=_DenyGovernor(reason),
    )

    assert result == "failed_before_commit"
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "finished"
    assert attempt.outcome == "failed_before_commit"
    assert attempt.reason_code == "GOVERNOR_DENIED"
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    assert executor.committed is False
    db.close()


def test_governor_backend_loss_after_admission_fails_closed(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _ConfirmedExecutor()

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=_live_settings(),
        governor=_UnavailableGovernor(),
    )

    assert result == "failed_before_commit"
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.reason_code == "GOVERNOR_DENIED"
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    assert executor.committed is False
    db.close()


@pytest.mark.asyncio
async def test_mismatched_preflight_handle_invalidates_plan_and_requires_reinspection(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _MismatchedActionExecutor()

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=_live_settings(),
        governor=_AllowGovernor(),
    )

    assert result == "failed_before_commit"
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "finished"
    assert attempt.reason_code == "FORM_CHANGED"
    assert attempt.final_action_at is None
    assert attempt.command.state == "completed"
    assert attempt.form_plan.invalidated_at is not None
    assert attempt.form_plan.invalidation_reason == "FORM_CHANGED"
    assert executor.committed is False

    monkeypatch.setattr(
        applications_route,
        "_validate_selected_cv",
        lambda _application: None,
    )
    with pytest.raises(HTTPException) as retry_exc:
        await applications_route.retry_application(reviewed.application_id, db=db)
    assert retry_exc.value.status_code == 409
    assert retry_exc.value.detail["code"] == "FORM_PLAN_REQUIRED"
    db.close()

    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed, key="retry-with-stale-plan")
    assert exc.value.reason_code == "APPLICATION_NOT_ELIGIBLE"

    verify = factory()
    assert verify.query(Submission).count() == 1
    assert verify.query(SubmissionCommand).count() == 1
    verify.close()


def test_only_typed_bound_evidence_can_finish_confirmed_submitted(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(_ConfirmedExecutor()),
        settings=_live_settings(),
        governor=_AllowGovernor(),
    )

    assert result == "confirmed_submitted"
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "finished"
    assert attempt.outcome == "confirmed_submitted"
    assert attempt.final_action_at is not None
    assert attempt.final_submit_permit.consumed_at is not None
    assert attempt.submitted_at is not None
    assert len(attempt.evidence) == 1
    assert is_employer_verified(attempt)
    db.close()


@pytest.mark.asyncio
async def test_preflight_commit_and_cleanup_share_one_event_loop(tmp_path):
    """Browser-bound state remains valid while sync safety gates run between calls."""

    class _ThreadRecordingGovernor(_AllowGovernor):
        thread_id = None

        def reserve_final_action(self, *, reservation_id, platform):
            self.thread_id = threading.get_ident()
            return super().reserve_final_action(
                reservation_id=reservation_id,
                platform=platform,
            )

    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _SameLoopExecutor()
    governor = _ThreadRecordingGovernor()
    caller_thread = threading.get_ident()

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=_live_settings(),
        governor=governor,
    )

    assert result == "confirmed_submitted"
    assert [name for name, _loop, _thread in executor.async_calls] == [
        "preflight",
        "commit",
        "cleanup",
    ]
    assert len({loop for _name, loop, _thread in executor.async_calls}) == 1
    assert len({thread for _name, _loop, thread in executor.async_calls}) == 1
    assert executor.async_calls[0][2] != caller_thread
    assert governor.thread_id == caller_thread
    assert executor.cleaned_action is not None
    assert executor.cleaned_action.attempt_id == created.attempt_id
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.outcome == "confirmed_submitted"
    assert attempt.final_action_at is not None
    db.close()


def test_context_aware_preflight_receives_current_private_cv_binding(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    cv_path = (tmp_path / "selected-private-cv.pdf").resolve()
    cv_path.write_bytes(b"synthetic test CV")
    selected = SimpleNamespace(
        cv_id="cv-ai",
        pdf_sha256=reviewed.cv_hash,
        resolved_path=str(cv_path),
    )
    resolver_calls = []

    def resolve_selected(cv_id, *, cv_routing_path, cv_directory):
        resolver_calls.append((cv_id, cv_routing_path, cv_directory))
        return selected

    def require_current(candidate, *, expected_sha256):
        assert candidate is selected
        assert expected_sha256 == reviewed.cv_hash
        return candidate

    monkeypatch.setattr(
        "profile.cv_content_cache.get_selected_cv_artifact_by_id",
        resolve_selected,
    )
    monkeypatch.setattr(
        "profile.cv_content_cache.require_current_selected_cv_artifact",
        require_current,
    )
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _ContextAwareSameLoopExecutor()
    settings = _live_settings()

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=settings,
        governor=_AllowGovernor(),
    )

    assert result == "confirmed_submitted"
    assert resolver_calls == [
        (
            "cv-ai",
            settings.cv_routing_path,
            settings.cv_directory,
        )
    ]
    context = executor.preflight_context
    assert context.normalized_job_url == "https://boards.greenhouse.io/acme/jobs/123"
    assert context.selected_cv_id == "cv-ai"
    assert context.selected_cv_hash == reviewed.cv_hash
    assert context.resume_path == str(cv_path)
    context_repr = repr(context)
    assert context_repr == "AdapterPreflightContext(<private>)"
    assert context.normalized_job_url not in context_repr
    assert context.resume_path not in context_repr
    assert [name for name, _loop, _thread in executor.async_calls] == [
        "preflight",
        "commit",
        "cleanup",
    ]
    assert len({loop for _name, loop, _thread in executor.async_calls}) == 1
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.outcome == "confirmed_submitted"
    assert not hasattr(attempt, "resume_path")
    db.close()


@pytest.mark.parametrize("failure_mode", ["missing", "changed"])
def test_context_aware_preflight_blocks_unavailable_or_changed_cv(
    tmp_path,
    monkeypatch,
    failure_mode,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    cv_path = (tmp_path / "selected-private-cv.pdf").resolve()
    cv_path.write_bytes(b"synthetic test CV")
    selected = SimpleNamespace(
        cv_id="cv-ai",
        pdf_sha256=reviewed.cv_hash,
        resolved_path=str(cv_path),
    )
    monkeypatch.setattr(
        "profile.cv_content_cache.get_selected_cv_artifact_by_id",
        lambda *_args, **_kwargs: None if failure_mode == "missing" else selected,
    )

    def require_current(_candidate, *, expected_sha256):
        del expected_sha256
        if failure_mode == "changed":
            raise RuntimeError("synthetic CV binding change")
        raise AssertionError("missing CV must stop before current-byte verification")

    monkeypatch.setattr(
        "profile.cv_content_cache.require_current_selected_cv_artifact",
        require_current,
    )
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _ContextAwareSameLoopExecutor()

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=_live_settings(),
        governor=_AllowGovernor(),
    )

    assert result == "failed_before_commit"
    assert executor.preflight_context is None
    assert executor.async_calls == []
    assert executor.committed is False
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.outcome == "failed_before_commit"
    assert attempt.reason_code == "ATTACHMENT_UNVERIFIED"
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    db.close()


def test_boundary_rejection_cleans_prepared_state_without_commit(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _SameLoopExecutor()

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=_live_settings(),
        governor=_DenyGovernor("kill switch active"),
    )

    assert result == "failed_before_commit"
    assert [name for name, _loop, _thread in executor.async_calls] == [
        "preflight",
        "cleanup",
    ]
    assert len({loop for _name, loop, _thread in executor.async_calls}) == 1
    assert executor.committed is False
    assert executor.cleaned_action is not None
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.outcome == "failed_before_commit"
    assert attempt.final_action_at is None
    db.close()


def test_same_loop_commit_exception_is_unknown_then_cleans_up(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _SameLoopExecutor(crash_after_boundary=True)

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(executor),
        settings=_live_settings(),
        governor=_AllowGovernor(),
    )

    assert result == "unknown"
    assert [name for name, _loop, _thread in executor.async_calls] == [
        "preflight",
        "commit",
        "cleanup",
    ]
    assert len({loop for _name, loop, _thread in executor.async_calls}) == 1
    assert executor.cleaned_action is not None
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.outcome == "unknown"
    assert attempt.final_action_at is not None
    assert attempt.final_submit_permit.consumed_at is not None
    db.close()


def test_final_execution_prohibits_all_llm_calls_before_transport(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _LLMCallingExecutor()

    with patch(
        "llm.ollama_runtime.httpx.AsyncClient",
        side_effect=AssertionError("Ollama transport must not be constructed"),
    ) as transport:
        result = execute_claimed_submission_command(
            db,
            created.command_id,
            registry=_registry(executor),
            settings=_live_settings(),
            governor=_AllowGovernor(),
        )

    assert result == "failed_before_commit"
    assert executor.committed is False
    transport.assert_not_called()
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.reason_code == "INTERNAL_ERROR"
    assert attempt.final_action_at is None
    db.close()


@pytest.mark.asyncio
async def test_eager_async_execution_preserves_final_stage_llm_prohibition(tmp_path):
    """The ContextVar guard must survive the lifecycle worker-thread boundary."""

    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    executor = _LLMCallingExecutor()

    with patch(
        "llm.ollama_runtime.httpx.AsyncClient",
        side_effect=AssertionError("Ollama transport must not be constructed"),
    ) as transport:
        result = execute_claimed_submission_command(
            db,
            created.command_id,
            registry=_registry(executor),
            settings=_live_settings(),
            governor=_AllowGovernor(),
        )

    assert result == "failed_before_commit"
    assert executor.committed is False
    transport.assert_not_called()
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.reason_code == "INTERNAL_ERROR"
    assert attempt.final_action_at is None
    db.close()


def test_mismatched_post_action_evidence_finishes_unknown_without_stale_wait(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(_MismatchedEvidenceExecutor()),
        settings=_live_settings(),
        governor=_AllowGovernor(),
    )

    assert result == "unknown"
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    command = db.get(SubmissionCommand, created.command_id)
    assert attempt.stage == "finished"
    assert attempt.outcome == "unknown"
    assert attempt.reason_code == "EVIDENCE_INVALID"
    assert command.state == "completed"
    assert db.query(SubmissionEvidence).count() == 0
    db.close()
    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed, key="duplicate-after-confirmed")
    assert exc.value.reason_code == "APPLICATION_NOT_ELIGIBLE"


def test_exception_after_commit_boundary_becomes_unknown_and_cannot_retry(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    governor = _AllowGovernor()

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(_CrashingExecutor()),
        settings=_live_settings(),
        governor=governor,
    )

    assert result == "unknown"
    assert governor.reservations == 1
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.outcome == "unknown"
    assert attempt.final_action_at is not None
    assert attempt.final_submit_permit.consumed_at is not None
    assert attempt.submitted_at is None
    assert attempt.application.status == JobStatus.NEEDS_REVIEW
    assert not is_employer_verified(attempt)
    assert claim_submission_command(db, command_id=created.command_id) is None
    db.close()
    with pytest.raises(SubmissionAdmissionError) as exc:
        _admit(factory, reviewed, key="duplicate-after-unknown")
    assert exc.value.reason_code == "APPLICATION_NOT_ELIGIBLE"


def test_pre_action_evidence_can_never_produce_a_green_outcome(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id

    result = execute_claimed_submission_command(
        db,
        created.command_id,
        registry=_registry(_PreActionEvidenceExecutor()),
        settings=_live_settings(),
        governor=_AllowGovernor(),
    )

    assert result == "unknown"
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.outcome == "unknown"
    assert attempt.reason_code == "EVIDENCE_INVALID"
    assert attempt.submitted_at is None
    assert db.query(SubmissionEvidence).count() == 0
    assert not is_employer_verified(attempt)
    db.close()


def test_stale_precommit_claim_is_requeued_but_commit_boundary_is_quarantined(tmp_path):
    factory = _factory(tmp_path)
    now = datetime.now(UTC).replace(tzinfo=None)

    safe = _reviewed_application(factory, now=now)
    safe_created = _admit(factory, safe, key="safe-command")
    safe_db = factory()
    claim_submission_command(
        safe_db,
        command_id=safe_created.command_id,
        now=now + timedelta(seconds=2),
    )
    safe_db.close()

    assert (
        reconcile_stale_submission_commands(
            factory(),
            now=now + timedelta(minutes=20),
            stale_seconds=60,
        )
        == 1
    )
    db = factory()
    safe_attempt = db.get(Submission, safe_created.attempt_id)
    assert safe_attempt.stage == "queued"
    assert safe_attempt.command.state == "pending"
    db.close()

    indeterminate = _reviewed_application(factory, now=now + timedelta(hours=1))
    indeterminate_created = _admit(
        factory,
        indeterminate,
        key="indeterminate-command",
    )
    db = factory()
    claim_submission_command(
        db,
        command_id=indeterminate_created.command_id,
        now=now + timedelta(hours=1, seconds=2),
    )
    attempt = db.get(Submission, indeterminate_created.attempt_id)
    attempt.stage = "committing"
    attempt.final_action_at = now + timedelta(hours=1, seconds=3)
    attempt.final_submit_permit.consumed_at = attempt.final_action_at
    db.commit()
    db.close()

    assert (
        reconcile_stale_submission_commands(
            factory(),
            now=now + timedelta(hours=1, minutes=20),
            stale_seconds=60,
        )
        == 1
    )
    db = factory()
    quarantined = db.get(Submission, indeterminate_created.attempt_id)
    assert quarantined.stage == "finished"
    assert quarantined.outcome == "unknown"
    assert quarantined.submitted_at is None
    assert quarantined.application.status == JobStatus.NEEDS_REVIEW
    db.close()


def test_superseded_worker_cannot_consume_permit_or_enter_commit_boundary(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    claimed_at = reviewed.now + timedelta(seconds=1)
    assert (
        claim_submission_command(
            db,
            command_id=created.command_id,
            now=claimed_at,
        )
        == created.command_id
    )
    old_token = db.get(SubmissionCommand, created.command_id).claim_token
    assert old_token

    assert (
        reconcile_stale_submission_commands(
            db,
            now=claimed_at + timedelta(minutes=2),
            stale_seconds=60,
        )
        == 1
    )
    assert (
        claim_submission_command(
            db,
            command_id=created.command_id,
            now=claimed_at + timedelta(minutes=2, seconds=1),
        )
        == created.command_id
    )
    command = db.get(SubmissionCommand, created.command_id)
    new_token = command.claim_token
    assert new_token and new_token != old_token
    command.attempt.stage = "ready"
    command.attempt.status = SubmissionStatus.PENDING
    db.commit()
    job_url_hash = command.attempt.final_submit_permit.job_url_hash
    action = _action_for_attempt(command.attempt, prepared_at=reviewed.now)

    assert (
        _enter_commit_boundary(
            db,
            command_id=created.command_id,
            expected_claim_token=old_token,
            job_url_hash=job_url_hash,
            action=action,
            now=claimed_at + timedelta(minutes=2, seconds=2),
        )
        is None
    )
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "ready"
    assert attempt.final_submit_permit.consumed_at is None

    assert (
        _enter_commit_boundary(
            db,
            command_id=created.command_id,
            expected_claim_token=new_token,
            job_url_hash=job_url_hash,
            action=action,
            now=claimed_at + timedelta(minutes=2, seconds=3),
        )
        is not None
    )
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "committing"
    assert attempt.final_submit_permit.consumed_at is not None
    db.close()


def test_superseded_precommit_failure_cannot_overwrite_new_claim(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    claimed_at = reviewed.now + timedelta(seconds=1)
    assert claim_submission_command(
        db,
        command_id=created.command_id,
        now=claimed_at,
    )
    old_token = db.get(SubmissionCommand, created.command_id).claim_token
    reconcile_stale_submission_commands(
        db,
        now=claimed_at + timedelta(minutes=2),
        stale_seconds=60,
    )
    assert claim_submission_command(
        db,
        command_id=created.command_id,
        now=claimed_at + timedelta(minutes=2, seconds=1),
    )
    command = db.get(SubmissionCommand, created.command_id)
    new_token = command.claim_token
    assert new_token and new_token != old_token

    result = _finish_claimed_before_commit(
        db,
        command_id=created.command_id,
        expected_claim_token=old_token,
        reason=ReasonCode.RUNTIME_NOT_READY,
        now=claimed_at + timedelta(minutes=2, seconds=2),
    )

    assert result == "superseded"
    db.expire_all()
    command = db.get(SubmissionCommand, created.command_id)
    assert command.state == "claimed"
    assert command.claim_token == new_token
    assert command.attempt.stage == "inspecting"
    assert command.attempt.outcome is None
    db.close()


def test_permit_expiry_at_fresh_boundary_finishes_without_stale_wait(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    command = db.get(SubmissionCommand, created.command_id)
    command.attempt.stage = "ready"
    command.attempt.status = SubmissionStatus.PENDING
    db.commit()
    claim_token = command.claim_token
    assert claim_token
    action = _action_for_attempt(command.attempt, prepared_at=reviewed.now)
    expired_at = command.attempt.final_submit_permit.expires_at

    with pytest.raises(_CommitBoundaryRejectedError, match="PERMIT_EXPIRED"):
        _enter_commit_boundary(
            db,
            command_id=created.command_id,
            expected_claim_token=claim_token,
            job_url_hash=command.attempt.final_submit_permit.job_url_hash,
            action=action,
            now=expired_at,
        )

    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "finished"
    assert attempt.outcome == "failed_before_commit"
    assert attempt.reason_code == "PERMIT_EXPIRED"
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    assert attempt.command.state == "completed"
    db.close()


def test_permit_expiring_during_governor_gate_never_reaches_commit(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    command = db.get(SubmissionCommand, created.command_id)
    command.attempt.stage = "ready"
    command.attempt.status = SubmissionStatus.PENDING
    db.commit()
    claim_token = command.claim_token
    assert claim_token
    action = _action_for_attempt(command.attempt, prepared_at=reviewed.now)
    expires_at = command.attempt.final_submit_permit.expires_at
    clock = iter((expires_at - timedelta(microseconds=1), expires_at))
    monkeypatch.setattr("worker.submission_commands._now", lambda: next(clock))
    governor_calls: list[str] = []

    def delayed_governor():
        governor_calls.append("reserved")
        return True, "reserved"

    with pytest.raises(_CommitBoundaryRejectedError, match="PERMIT_EXPIRED"):
        _enter_commit_boundary(
            db,
            command_id=created.command_id,
            expected_claim_token=claim_token,
            job_url_hash=command.attempt.final_submit_permit.job_url_hash,
            action=action,
            governor_gate=delayed_governor,
        )

    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert governor_calls == ["reserved"]
    assert attempt.stage == "finished"
    assert attempt.outcome == "failed_before_commit"
    assert attempt.reason_code == "PERMIT_EXPIRED"
    assert attempt.final_action_at is None
    assert attempt.final_submit_permit.consumed_at is None
    db.close()


@pytest.mark.asyncio
async def test_operator_reject_atomically_cancels_only_precommit_work(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    command = db.get(SubmissionCommand, created.command_id)
    old_token = command.claim_token
    assert old_token
    action = _action_for_attempt(command.attempt, prepared_at=reviewed.now)

    result = await applications_route.reject_application(
        reviewed.application_id,
        reason="Operator rejected fixture",
        db=db,
    )

    assert result["application_id"] == reviewed.application_id
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.application.status == JobStatus.SKIPPED
    assert attempt.stage == "finished"
    assert attempt.outcome == "failed_before_commit"
    assert attempt.reason_code == "OPERATOR_CANCELLED"
    assert attempt.command.state == "cancelled"
    assert attempt.command.claim_token is None
    assert attempt.final_submit_permit.consumed_at is None
    assert attempt.form_plan.invalidated_at is not None
    assert (
        _enter_commit_boundary(
            db,
            command_id=created.command_id,
            expected_claim_token=old_token,
            job_url_hash=attempt.final_submit_permit.job_url_hash,
            action=action,
            now=datetime.now(UTC).replace(tzinfo=None),
        )
        is None
    )
    db.close()


@pytest.mark.asyncio
async def test_operator_reject_is_blocked_after_commit_boundary(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    assert claim_submission_command(db, command_id=created.command_id) == created.command_id
    command = db.get(SubmissionCommand, created.command_id)
    command.attempt.stage = "ready"
    command.attempt.status = SubmissionStatus.PENDING
    db.commit()
    claim_token = command.claim_token
    assert claim_token
    action = _action_for_attempt(command.attempt, prepared_at=reviewed.now)
    assert (
        _enter_commit_boundary(
            db,
            command_id=created.command_id,
            expected_claim_token=claim_token,
            job_url_hash=command.attempt.final_submit_permit.job_url_hash,
            action=action,
            now=datetime.now(UTC).replace(tzinfo=None),
        )
        is not None
    )

    with pytest.raises(HTTPException) as exc:
        await applications_route.reject_application(
            reviewed.application_id,
            reason="Too late",
            db=db,
        )

    assert exc.value.status_code == 409
    db.expire_all()
    attempt = db.get(Submission, created.attempt_id)
    assert attempt.stage == "committing"
    assert attempt.application.status == JobStatus.DRAFT
    db.close()


def test_legacy_stale_reconciler_excludes_command_backed_attempts(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    claim_submission_command(
        db,
        command_id=created.command_id,
        now=reviewed.now + timedelta(seconds=1),
    )

    assert (
        mark_stale_attempts_unknown(
            db,
            now=reviewed.now + timedelta(hours=1),
            stale_minutes=15,
        )
        == 0
    )
    db.expire_all()
    assert db.get(Submission, created.attempt_id).stage == "inspecting"
    db.close()


def test_evidence_row_without_confirmed_attempt_never_becomes_green(tmp_path):
    factory = _factory(tmp_path)
    reviewed = _reviewed_application(factory)
    created = _admit(factory, reviewed)
    db = factory()
    attempt = db.get(Submission, created.attempt_id)
    db.add(
        SubmissionEvidence(
            attempt_id=attempt.id,
            evidence_type="api_receipt",
            evidence_digest="d" * 64,
            receipt_ref="opaque-only",
            form_fingerprint=attempt.form_plan_fingerprint,
            cv_hash=attempt.attached_cv_hash,
        )
    )
    db.commit()

    assert attempt.status == SubmissionStatus.PENDING
    assert not is_employer_verified(attempt)
    db.close()
