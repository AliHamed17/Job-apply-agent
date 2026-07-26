"""Focused database-contract tests for the v4 submission persistence kernel."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from db.models import (
    Application,
    Base,
    FinalSubmitPermit,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
    SubmissionEvidence,
    SubmissionStatus,
)


def _session():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _application(db) -> Application:
    job = Job(
        title="Evidence Engineer",
        source_url="https://example.invalid/jobs/1",
        status=JobStatus.DRAFT,
    )
    application = Application(job=job, status=JobStatus.DRAFT)
    db.add(application)
    db.flush()
    return application


def _verified_form_plan(db, application_id: int, now: datetime) -> FormPlan:
    form_plan = FormPlan(
        application_id=application_id,
        application_revision=1,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-v1",
        fingerprint="f" * 64,
        selected_cv_id="cv-ai",
        selected_cv_hash="c" * 64,
        attached_cv_id="cv-ai",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
        profile_version=3,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(form_plan)
    db.flush()
    return form_plan


def _finished_attempt(
    application_id: int,
    attempt_number: int,
    *,
    idempotency_key: str,
) -> Submission:
    return Submission(
        application_id=application_id,
        attempt_number=attempt_number,
        idempotency_key=idempotency_key,
        submitter_name="greenhouse",
        status=SubmissionStatus.DRAFT_ONLY,
        stage="finished",
        outcome="draft_only",
    )


def test_persistence_kernel_roundtrip_keeps_only_redacted_authority():
    db = _session()
    application = _application(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    form_plan = FormPlan(
        application_id=application.id,
        application_revision=application.revision,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-v1",
        fingerprint="f" * 64,
        selected_cv_id="cv-ai",
        selected_cv_hash="c" * 64,
        attached_cv_id="cv-ai",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
        profile_version=3,
        fields_json="[]",
        decisions_json="[]",
        blockers_json="[]",
        session_verified_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(form_plan)
    db.flush()
    attempt = Submission(
        application_id=application.id,
        attempt_number=1,
        idempotency_key="client-key-" + ("x" * 80),
        submitter_name="greenhouse",
        status=SubmissionStatus.PENDING,
        stage="ready",
        application_revision=application.revision,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-v1",
        form_plan_id=form_plan.id,
        form_plan_fingerprint=form_plan.fingerprint,
        requested_cv_id="cv-ai",
        requested_cv_hash="c" * 64,
        attached_cv_id="cv-ai",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
    )
    db.add(attempt)
    db.flush()
    permit = FinalSubmitPermit(
        attempt_id=attempt.id,
        nonce_hash="n" * 64,
        job_url_hash="j" * 64,
        application_revision=application.revision,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-v1",
        form_plan_fingerprint=form_plan.fingerprint,
        cv_hash="c" * 64,
        expires_at=now + timedelta(minutes=5),
    )
    command = SubmissionCommand(
        attempt_id=attempt.id,
        idempotency_key="command-key-" + ("y" * 60),
    )
    evidence = SubmissionEvidence(
        attempt_id=attempt.id,
        evidence_type="api_receipt",
        evidence_digest="e" * 64,
        receipt_ref="opaque-receipt-1",
        form_fingerprint=form_plan.fingerprint,
        cv_hash="c" * 64,
    )
    db.add_all([permit, command, evidence])
    db.commit()

    saved = db.query(Submission).one()
    assert saved.form_plan is form_plan
    assert saved.final_submit_permit is permit
    assert saved.command is command
    assert saved.evidence == [evidence]
    assert len(saved.idempotency_key) > 36
    assert "nonce" not in FinalSubmitPermit.__table__.c
    assert "nonce_hash" in FinalSubmitPermit.__table__.c


def test_form_plan_history_uses_id_to_break_equal_timestamp_ties():
    db = _session()
    application = _application(db)
    now = datetime(2026, 7, 26, 12, 0)
    first = _verified_form_plan(db, application.id, now)
    second = FormPlan(
        application_id=application.id,
        application_revision=1,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-v1",
        fingerprint="d" * 64,
        selected_cv_id="cv-ai",
        selected_cv_hash="c" * 64,
        attached_cv_id="cv-ai",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
        profile_version=3,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(second)
    db.commit()
    db.expire(application, ["form_plans"])

    assert [plan.id for plan in application.form_plans] == [first.id, second.id]
    assert application.form_plans[-1].fingerprint == "d" * 64


@pytest.mark.parametrize(
    "command_values",
    [
        {"state": "pending", "claimed_at": datetime(2026, 7, 26)},
        {"state": "pending", "completed_at": datetime(2026, 7, 26)},
        {
            "state": "claimed",
            "claimed_at": datetime(2026, 7, 26),
            "claim_token": "t" * 64,
        },
        {
            "state": "claimed",
            "claimed_at": datetime(2026, 7, 26),
            "claimed_by": "runner",
            "claim_token": "t" * 64,
            "completed_at": datetime(2026, 7, 26),
        },
        {"state": "completed"},
        {
            "state": "completed",
            "completed_at": datetime(2026, 7, 26),
            "claimed_at": datetime(2026, 7, 26),
            "claimed_by": "runner",
            "claim_token": "t" * 64,
        },
        {"state": "cancelled"},
    ],
)
def test_submission_command_state_rejects_contradictory_lease_metadata(
    command_values,
):
    db = _session()
    application = _application(db)
    attempt = _finished_attempt(
        application.id,
        1,
        idempotency_key="command-parent",
    )
    db.add(attempt)
    db.flush()
    db.add(
        SubmissionCommand(
            attempt_id=attempt.id,
            idempotency_key="invalid-command",
            **command_values,
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()


def test_only_one_unfinished_attempt_per_application():
    db = _session()
    application = _application(db)
    first = Submission(
        application_id=application.id,
        attempt_number=1,
        idempotency_key="first",
        submitter_name="greenhouse",
        status=SubmissionStatus.PENDING,
        stage="queued",
    )
    db.add(first)
    db.commit()

    db.add(
        Submission(
            application_id=application.id,
            attempt_number=2,
            idempotency_key="second",
            submitter_name="greenhouse",
            status=SubmissionStatus.PENDING,
            stage="ready",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    first = db.query(Submission).filter_by(id=first.id).one()
    first.stage = "finished"
    first.outcome = "draft_only"
    first.status = SubmissionStatus.DRAFT_ONLY
    db.commit()
    db.add(
        Submission(
            application_id=application.id,
            attempt_number=2,
            idempotency_key="second",
            submitter_name="greenhouse",
            status=SubmissionStatus.PENDING,
            stage="queued",
        )
    )
    db.commit()
    assert db.query(Submission).count() == 2


@pytest.mark.parametrize(
    ("stage", "outcome"),
    [
        ("finished", None),
        ("queued", "draft_only"),
        ("not-a-stage", None),
        ("finished", "not-an-outcome"),
    ],
)
def test_stage_and_outcome_cannot_contradict(stage, outcome):
    db = _session()
    application = _application(db)
    db.add(
        Submission(
            application_id=application.id,
            attempt_number=1,
            idempotency_key=f"{stage}-{outcome}",
            submitter_name="greenhouse",
            status=SubmissionStatus.PENDING,
            stage=stage,
            outcome=outcome,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()


def test_evidence_digest_is_unique_per_attempt_not_globally():
    db = _session()
    application = _application(db)
    first = _finished_attempt(application.id, 1, idempotency_key="first")
    second = _finished_attempt(application.id, 2, idempotency_key="second")
    db.add_all([first, second])
    db.flush()
    common = {
        "evidence_type": "candidate_portal_record",
        "evidence_digest": "d" * 64,
        "portal_record_ref": "opaque-record",
        "form_fingerprint": "f" * 64,
        "cv_hash": "c" * 64,
    }
    db.add_all(
        [
            SubmissionEvidence(attempt_id=first.id, **common),
            SubmissionEvidence(attempt_id=second.id, **common),
        ]
    )
    db.commit()
    assert db.query(SubmissionEvidence).count() == 2

    db.add(SubmissionEvidence(attempt_id=first.id, **common))
    with pytest.raises(IntegrityError):
        db.commit()


def _confirmed_attempt(
    application_id: int,
    now: datetime,
    form_plan: FormPlan,
) -> Submission:
    return Submission(
        application_id=application_id,
        attempt_number=1,
        idempotency_key="confirmed-attempt",
        submitter_name="greenhouse",
        status=SubmissionStatus.SUCCESS,
        stage="finished",
        outcome="confirmed_submitted",
        profile_version=3,
        submitted_at=now,
        final_action_at=now,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-v1",
        attachment_verified=True,
        selected_cv_id="cv-ai",
        requested_cv_id="cv-ai",
        requested_cv_hash="c" * 64,
        attached_cv_id="cv-ai",
        attached_cv_hash="c" * 64,
        form_plan_id=form_plan.id,
        form_plan_fingerprint="f" * 64,
        verification_kind="api_receipt",
        evidence_digest="e" * 64,
        runner_release="test-release",
    )


def test_confirmed_submission_requires_exact_evidence_row():
    db = _session()
    application = _application(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    form_plan = _verified_form_plan(db, application.id, now)
    db.add(_confirmed_attempt(application.id, now, form_plan))

    with pytest.raises(IntegrityError):
        db.commit()


def test_confirmed_submission_and_matching_evidence_commit_together():
    db = _session()
    application = _application(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    form_plan = _verified_form_plan(db, application.id, now)
    attempt = _confirmed_attempt(application.id, now, form_plan)
    db.add(attempt)
    db.flush()
    db.add(
        SubmissionEvidence(
            attempt_id=attempt.id,
            evidence_type="api_receipt",
            evidence_digest="e" * 64,
            receipt_ref="opaque-receipt",
            form_fingerprint="f" * 64,
            cv_hash="c" * 64,
        )
    )
    db.commit()

    assert db.query(Submission).one().outcome == "confirmed_submitted"


@pytest.mark.parametrize(
    "overrides",
    [
        {"form_plan_id": None},
        {"requested_cv_id": None},
        {"attached_cv_id": "cv-other"},
        {"requested_cv_hash": "d" * 64},
    ],
)
def test_confirmed_submission_requires_exact_cv_identity(overrides):
    db = _session()
    application = _application(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    form_plan = _verified_form_plan(db, application.id, now)
    attempt = _confirmed_attempt(application.id, now, form_plan)
    for field, value in overrides.items():
        setattr(attempt, field, value)
    db.add(attempt)

    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize(
    "overrides",
    [
        {"application_revision": 2},
        {"adapter_name": "lever"},
        {"adapter_version": "2.0.0"},
        {"selector_version": "greenhouse-v2"},
        {"profile_version": 4},
    ],
)
def test_confirmed_submission_requires_exact_form_plan_binding(overrides):
    db = _session()
    application = _application(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    form_plan = _verified_form_plan(db, application.id, now)
    attempt = _confirmed_attempt(application.id, now, form_plan)
    for field, value in overrides.items():
        setattr(attempt, field, value)
    db.add(attempt)

    with pytest.raises(IntegrityError):
        db.flush()


@pytest.mark.parametrize("runner_release", [None, "", " ", "r" * 65])
def test_confirmed_submission_requires_bounded_runner_release(runner_release):
    db = _session()
    application = _application(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    form_plan = _verified_form_plan(db, application.id, now)
    attempt = _confirmed_attempt(application.id, now, form_plan)
    attempt.runner_release = runner_release
    db.add(attempt)

    with pytest.raises(IntegrityError):
        db.flush()


def test_confirmed_submission_rejects_pre_action_submitted_timestamp():
    db = _session()
    application = _application(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    form_plan = _verified_form_plan(db, application.id, now)
    attempt = _confirmed_attempt(application.id, now, form_plan)
    attempt.submitted_at = now - timedelta(seconds=1)
    db.add(attempt)

    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize(
    ("evidence_type", "form_fingerprint", "cv_hash", "receipt_ref", "portal_ref"),
    [
        ("candidate_portal_record", "f" * 64, "c" * 64, None, "portal-record"),
        ("api_receipt", "0" * 64, "c" * 64, "opaque-receipt", None),
        ("api_receipt", "f" * 64, "1" * 64, "opaque-receipt", None),
    ],
)
def test_confirmed_submission_requires_composite_evidence_binding(
    evidence_type,
    form_fingerprint,
    cv_hash,
    receipt_ref,
    portal_ref,
):
    db = _session()
    application = _application(db)
    now = datetime.now(UTC).replace(tzinfo=None)
    form_plan = _verified_form_plan(db, application.id, now)
    attempt = _confirmed_attempt(application.id, now, form_plan)
    db.add(attempt)
    db.flush()
    db.add(
        SubmissionEvidence(
            attempt_id=attempt.id,
            evidence_type=evidence_type,
            evidence_digest="e" * 64,
            receipt_ref=receipt_ref,
            portal_record_ref=portal_ref,
            form_fingerprint=form_fingerprint,
            cv_hash=cv_hash,
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence_type": "invented"},
        {"evidence_digest": ""},
        {"evidence_digest": "z" * 64},
        {"form_fingerprint": ""},
        {"cv_hash": ""},
        {"receipt_ref": None},
        {"receipt_ref": " ", "employer_application_ref": None},
        {
            "receipt_ref": None,
            "employer_application_ref": "wrong-reference-kind",
        },
    ],
)
def test_evidence_rows_reject_untyped_or_empty_proof(overrides):
    db = _session()
    application = _application(db)
    attempt = _finished_attempt(application.id, 1, idempotency_key="evidence-parent")
    db.add(attempt)
    db.flush()
    values = {
        "attempt_id": attempt.id,
        "evidence_type": "api_receipt",
        "evidence_digest": "e" * 64,
        "receipt_ref": "opaque-receipt",
        "employer_application_ref": None,
        "portal_record_ref": None,
        "form_fingerprint": "f" * 64,
        "cv_hash": "c" * 64,
    }
    values.update(overrides)
    db.add(SubmissionEvidence(**values))

    with pytest.raises(IntegrityError):
        db.commit()


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL integration test",
)
def test_postgres_partial_index_allows_only_one_concurrent_unfinished_attempt():
    engine = create_engine(os.environ["DATABASE_URL"])
    factory = sessionmaker(bind=engine)
    token = uuid.uuid4().hex
    setup = factory()
    job = Job(
        title=f"Persistence guard {token}",
        source_url=f"https://example.invalid/{token}",
        status=JobStatus.DRAFT,
    )
    application = Application(job=job, status=JobStatus.DRAFT)
    setup.add(application)
    setup.commit()
    application_id = application.id
    job_id = job.id
    setup.close()
    barrier = Barrier(2)

    def _insert(attempt_number: int) -> bool:
        db = factory()
        try:
            db.add(
                Submission(
                    application_id=application_id,
                    attempt_number=attempt_number,
                    idempotency_key=f"{token}-{attempt_number}",
                    submitter_name="greenhouse",
                    status=SubmissionStatus.PENDING,
                    stage="queued",
                )
            )
            barrier.wait()
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            inserted = list(pool.map(_insert, (1, 2)))
        assert sum(inserted) == 1
        check = factory()
        assert check.query(Submission).filter_by(application_id=application_id).count() == 1
        check.close()
    finally:
        cleanup = factory()
        cleanup.query(Submission).filter_by(application_id=application_id).delete()
        cleanup.query(Application).filter_by(id=application_id).delete()
        cleanup.query(Job).filter_by(id=job_id).delete()
        cleanup.commit()
        cleanup.close()
