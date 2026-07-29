from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from core.restore_safety import quarantine_restored_runtime
from db.models import (
    Application,
    Base,
    ControlPlaneApplicationRef,
    ControlPlaneReviewGrant,
    FinalSubmitPermit,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
    SubmissionEvidence,
    SubmissionStatus,
)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'restore-safety.db'}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _application(db, *, sequence: int, status: JobStatus = JobStatus.APPROVED) -> Application:
    job = Job(
        title=f"Restore safety {sequence}",
        source_url=f"https://example.invalid/jobs/{sequence}",
        status=status,
    )
    application = Application(
        job=job,
        status=status,
        approved_at=datetime(2026, 7, 28, 8, 0),
        approval_source="manual",
        revision=1,
        prepared_revision=1,
        selected_cv_id=f"cv-{sequence}",
        selected_cv_hash=str(sequence) * 64,
        profile_version=3,
    )
    db.add(application)
    db.flush()
    return application


def _form_plan(db, application: Application, *, now: datetime) -> FormPlan:
    plan = FormPlan(
        application_id=application.id,
        application_revision=1,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-v1",
        fingerprint=str(application.id % 10) * 64,
        selected_cv_id=application.selected_cv_id,
        selected_cv_hash=application.selected_cv_hash,
        attached_cv_id=application.selected_cv_id,
        attached_cv_hash=application.selected_cv_hash,
        attachment_verified=True,
        profile_version=3,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(plan)
    db.flush()
    return plan


def _attempt(
    db,
    application: Application,
    plan: FormPlan,
    *,
    stage: str,
    status: SubmissionStatus,
    attempt_number: int = 1,
) -> Submission:
    attempt = Submission(
        application_id=application.id,
        attempt_number=attempt_number,
        idempotency_key=f"restore-attempt-{application.id}-{attempt_number}",
        submitter_name="greenhouse",
        status=status,
        stage=stage,
        application_revision=1,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-v1",
        form_plan_id=plan.id,
        form_plan_fingerprint=plan.fingerprint,
        selected_cv_id=application.selected_cv_id,
        requested_cv_id=application.selected_cv_id,
        requested_cv_hash=application.selected_cv_hash,
        attached_cv_id=application.selected_cv_id,
        attached_cv_hash=application.selected_cv_hash,
        attachment_verified=True,
        profile_version=3,
    )
    db.add(attempt)
    db.flush()
    return attempt


def _permit(
    db,
    attempt: Submission,
    *,
    now: datetime,
    consumed: bool,
) -> FinalSubmitPermit:
    permit = FinalSubmitPermit(
        attempt_id=attempt.id,
        nonce_hash=f"{attempt.id:x}".rjust(64, "a"),
        job_url_hash="b" * 64,
        application_revision=1,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-v1",
        form_plan_fingerprint=attempt.form_plan_fingerprint,
        cv_hash=attempt.requested_cv_hash,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        consumed_at=now + timedelta(seconds=1) if consumed else None,
    )
    db.add(permit)
    return permit


def test_restore_quarantine_is_idempotent_and_preserves_confirmed_evidence(tmp_path) -> None:
    factory = _factory(tmp_path)
    created_at = datetime(2026, 7, 28, 8, 0)
    restored_at = datetime(2026, 7, 28, 8, 2, tzinfo=UTC)
    with factory() as db:
        precommit = _application(db, sequence=1)
        precommit_plan = _form_plan(db, precommit, now=created_at)
        precommit_attempt = _attempt(
            db,
            precommit,
            precommit_plan,
            stage="ready",
            status=SubmissionStatus.PENDING,
        )
        precommit_permit = _permit(
            db,
            precommit_attempt,
            now=created_at,
            consumed=False,
        )
        db.add(
            SubmissionCommand(
                attempt_id=precommit_attempt.id,
                idempotency_key="restore-precommit-command",
                state="claimed",
                claimed_at=created_at,
                claimed_by="restored-runner",
                claim_token="c" * 64,
            )
        )
        application_ref = ControlPlaneApplicationRef(
            application_id=precommit.id,
            remote_ref="r" * 32,
        )
        db.add(application_ref)
        db.flush()
        review_grant = ControlPlaneReviewGrant(
            grant_ref="g" * 32,
            application_id=precommit.id,
            application_ref_id=application_ref.id,
            form_plan_id=precommit_plan.id,
            application_revision=1,
            job_url_hash="b" * 64,
            form_plan_fingerprint=precommit_plan.fingerprint,
            cv_hash=precommit.selected_cv_hash,
            adapter_name="greenhouse",
            adapter_version="1.0.0",
            selector_version="greenhouse-v1",
            runner_release="d" * 64,
            issued_at=created_at,
            expires_at=created_at + timedelta(hours=2),
            projection_available_at=created_at,
        )
        db.add(review_grant)

        postcommit = _application(db, sequence=2)
        postcommit_plan = _form_plan(db, postcommit, now=created_at)
        postcommit_attempt = _attempt(
            db,
            postcommit,
            postcommit_plan,
            stage="committing",
            status=SubmissionStatus.RUNNING,
        )
        postcommit_attempt.final_action_at = created_at + timedelta(minutes=1)
        _permit(db, postcommit_attempt, now=created_at, consumed=True)
        db.add(
            SubmissionCommand(
                attempt_id=postcommit_attempt.id,
                idempotency_key="restore-postcommit-command",
                state="pending",
            )
        )

        confirmed = _application(db, sequence=3, status=JobStatus.SUBMITTED)
        confirmed_plan = _form_plan(db, confirmed, now=created_at)
        confirmed_attempt = _attempt(
            db,
            confirmed,
            confirmed_plan,
            stage="finished",
            status=SubmissionStatus.SUCCESS,
        )
        confirmed_attempt.outcome = "confirmed_submitted"
        confirmed_attempt.final_action_at = created_at + timedelta(minutes=1)
        confirmed_attempt.submitted_at = created_at + timedelta(minutes=2)
        confirmed_attempt.finished_at = created_at + timedelta(minutes=2)
        confirmed_attempt.verification_kind = "api_receipt"
        confirmed_attempt.evidence_digest = "e" * 64
        confirmed_attempt.runner_release = "test-release"
        confirmed_attempt.reason_code = "EMPLOYER_VERIFIED"
        db.add(
            SubmissionEvidence(
                attempt_id=confirmed_attempt.id,
                evidence_type="api_receipt",
                evidence_digest="e" * 64,
                receipt_ref="opaque-confirmation",
                form_fingerprint=confirmed_plan.fingerprint,
                cv_hash=confirmed.selected_cv_hash,
                observed_at=created_at + timedelta(minutes=2),
            )
        )
        confirmed_permit = _permit(
            db,
            confirmed_attempt,
            now=created_at,
            consumed=False,
        )
        confirmed_command = SubmissionCommand(
            attempt_id=confirmed_attempt.id,
            idempotency_key="restore-confirmed-command",
            state="pending",
        )
        confirmed_application_ref = ControlPlaneApplicationRef(
            application_id=confirmed.id,
            remote_ref="s" * 32,
        )
        db.add_all([confirmed_command, confirmed_application_ref])
        db.flush()
        confirmed_review_grant = ControlPlaneReviewGrant(
            grant_ref="h" * 32,
            application_id=confirmed.id,
            application_ref_id=confirmed_application_ref.id,
            form_plan_id=confirmed_plan.id,
            application_revision=1,
            job_url_hash="f" * 64,
            form_plan_fingerprint=confirmed_plan.fingerprint,
            cv_hash=confirmed.selected_cv_hash,
            adapter_name="greenhouse",
            adapter_version="1.0.0",
            selector_version="greenhouse-v1",
            runner_release="d" * 64,
            issued_at=created_at,
            expires_at=created_at + timedelta(hours=2),
            projection_available_at=created_at,
        )
        db.add(confirmed_review_grant)

        mixed_history = _application(db, sequence=4, status=JobStatus.SUBMITTED)
        mixed_history_plan = _form_plan(db, mixed_history, now=created_at)
        mixed_confirmed_attempt = _attempt(
            db,
            mixed_history,
            mixed_history_plan,
            stage="finished",
            status=SubmissionStatus.SUCCESS,
        )
        mixed_confirmed_attempt.outcome = "confirmed_submitted"
        mixed_confirmed_attempt.final_action_at = created_at + timedelta(minutes=1)
        mixed_confirmed_attempt.submitted_at = created_at + timedelta(minutes=2)
        mixed_confirmed_attempt.finished_at = created_at + timedelta(minutes=2)
        mixed_confirmed_attempt.verification_kind = "api_receipt"
        mixed_confirmed_attempt.evidence_digest = "a" * 64
        mixed_confirmed_attempt.runner_release = "test-release"
        mixed_confirmed_attempt.reason_code = "EMPLOYER_VERIFIED"
        db.add(
            SubmissionEvidence(
                attempt_id=mixed_confirmed_attempt.id,
                evidence_type="api_receipt",
                evidence_digest="a" * 64,
                receipt_ref="opaque-mixed-confirmation",
                form_fingerprint=mixed_history_plan.fingerprint,
                cv_hash=mixed_history.selected_cv_hash,
                observed_at=created_at + timedelta(minutes=2),
            )
        )
        later_unfinished_attempt = _attempt(
            db,
            mixed_history,
            mixed_history_plan,
            stage="committing",
            status=SubmissionStatus.RUNNING,
            attempt_number=2,
        )
        later_unfinished_attempt.final_action_at = created_at + timedelta(minutes=3)
        later_unfinished_command = SubmissionCommand(
            attempt_id=later_unfinished_attempt.id,
            idempotency_key="restore-confirmed-later-command",
            state="pending",
        )
        db.add(later_unfinished_command)
        db.commit()
        # Simulate a restored pre-constraint row whose stage metadata is stale.
        # Exact employer evidence must win over that inconsistency.
        db.execute(text("PRAGMA ignore_check_constraints = ON"))
        db.execute(
            text("UPDATE submissions SET stage = 'verifying' WHERE id = :attempt_id"),
            {"attempt_id": confirmed_attempt.id},
        )
        db.commit()
        db.expire(confirmed_attempt)

        summary = quarantine_restored_runtime(db, now=restored_at)
        db.commit()

        assert summary.to_dict() == {
            "form_plans_invalidated": 4,
            "final_permits_expired": 2,
            "review_grants_revoked": 2,
            "review_grant_revocations_rearmed": 2,
            "commands_cancelled": 4,
            "precommit_attempts_cancelled": 1,
            "postcommit_attempts_marked_unknown": 2,
            "applications_moved_to_review": 2,
        }
        assert precommit_plan.invalidation_reason == "RESTORE_QUARANTINE"
        assert precommit_permit.expires_at == restored_at.replace(tzinfo=None)
        assert review_grant.revoked_at == restored_at.replace(tzinfo=None)
        assert review_grant.revocation_state == "pending"
        assert precommit_attempt.stage == "finished"
        assert precommit_attempt.outcome == "failed_before_commit"
        assert precommit_attempt.reason_code == "RUNTIME_NOT_READY"
        assert precommit_attempt.command.state == "cancelled"
        assert precommit.status == JobStatus.NEEDS_REVIEW

        assert postcommit_attempt.stage == "finished"
        assert postcommit_attempt.outcome == "unknown"
        assert postcommit_attempt.status == SubmissionStatus.UNKNOWN
        assert postcommit_attempt.reason_code == "STALE_INDETERMINATE"
        assert postcommit_attempt.submitted_at is None
        assert postcommit.status == JobStatus.NEEDS_REVIEW

        assert confirmed_attempt.outcome == "confirmed_submitted"
        assert confirmed_attempt.stage == "verifying"
        assert confirmed_attempt.status == SubmissionStatus.SUCCESS
        assert confirmed_attempt.submitted_at == created_at + timedelta(minutes=2)
        assert confirmed_attempt.evidence[0].evidence_digest == "e" * 64
        assert confirmed_permit.expires_at == restored_at.replace(tzinfo=None)
        assert confirmed_command.state == "cancelled"
        assert confirmed_review_grant.revoked_at == restored_at.replace(tzinfo=None)
        assert confirmed_review_grant.revocation_state == "pending"
        assert later_unfinished_attempt.stage == "finished"
        assert later_unfinished_attempt.outcome == "unknown"
        assert later_unfinished_attempt.status == SubmissionStatus.UNKNOWN
        assert later_unfinished_attempt.reason_code == "STALE_INDETERMINATE"
        assert later_unfinished_command.state == "cancelled"
        assert mixed_confirmed_attempt.outcome == "confirmed_submitted"
        assert mixed_confirmed_attempt.submitted_at == created_at + timedelta(minutes=2)
        assert mixed_history.status == JobStatus.SUBMITTED
        assert mixed_history.job.status == JobStatus.SUBMITTED
        assert confirmed.status == JobStatus.SUBMITTED
        assert confirmed.job.status == JobStatus.SUBMITTED

        repeated = quarantine_restored_runtime(db, now=restored_at + timedelta(minutes=1))
        db.commit()
        assert repeated.to_dict() == {
            "form_plans_invalidated": 0,
            "final_permits_expired": 0,
            "review_grants_revoked": 0,
            "review_grant_revocations_rearmed": 0,
            "commands_cancelled": 0,
            "precommit_attempts_cancelled": 0,
            "postcommit_attempts_marked_unknown": 0,
            "applications_moved_to_review": 0,
        }


def test_quarantine_script_dry_run_rolls_back(tmp_path, monkeypatch, capsys) -> None:
    from scripts import quarantine_restored_runtime as command

    factory = _factory(tmp_path)
    with factory() as db:
        application = _application(db, sequence=4)
        plan = _form_plan(db, application, now=datetime(2026, 7, 28, 8, 0))
        plan_id = plan.id
        db.commit()

    monkeypatch.setattr(command, "get_session_factory", lambda: factory)
    assert command.main(["--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["committed"] is False
    assert result["restore_quarantine"]["form_plans_invalidated"] == 1

    with factory() as db:
        assert db.get(FormPlan, plan_id).invalidated_at is None
