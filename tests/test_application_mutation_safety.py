"""Regression coverage for app-first locked application mutations."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from profile.models import UserProfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import cv_routing as cv_routing_route
from api.routes import realign as realign_route
from api.routes import webhook as webhook_route
from core.application_mutations import (
    ApplicationMutationBlockedError,
    ApplicationMutationIntent,
    lock_application_for_mutation,
    mark_locked_application_prepared,
    transition_locked_application_to_skipped,
)
from core.config import Settings
from db.models import (
    Application,
    ApplicationEvent,
    Base,
    FinalSubmitPermit,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
    SubmissionStatus,
    UserProfileVersion,
)
from llm.generation import GeneratedApplication
from match.scoring import Action
from worker.drainer import expire_stale_jobs


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mutation-safety.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _application(factory):
    db = factory()
    job = Job(
        title="Mutation safety",
        company="Acme",
        location="Remote",
        employment_type="",
        seniority="",
        description="Safety fixture",
        requirements="Python",
        source_url="https://example.test/mutation",
        apply_url="https://example.test/mutation",
        status=JobStatus.DRAFT,
        created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30),
    )
    application = Application(
        job=job,
        status=JobStatus.DRAFT,
        selected_cv_id="cv-safe",
        profile_version=1,
        revision=1,
    )
    db.add(application)
    db.commit()
    result = (application.id, job.id)
    db.close()
    return result


def _precommit_attempt(factory, application_id: int):
    now = datetime.now(UTC).replace(tzinfo=None)
    db = factory()
    plan = FormPlan(
        plan_id=str(uuid4()),
        application_id=application_id,
        application_revision=1,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        fingerprint="f" * 64,
        selected_cv_id="cv-safe",
        selected_cv_hash="c" * 64,
        attached_cv_id="cv-safe",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
        profile_version=1,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(plan)
    db.flush()
    attempt = Submission(
        application_id=application_id,
        attempt_number=1,
        idempotency_key=f"mutation-{uuid4().hex}",
        submitter_name="greenhouse",
        status=SubmissionStatus.PENDING,
        stage="queued",
        application_revision=1,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_plan_id=plan.id,
        form_plan_fingerprint=plan.fingerprint,
        selected_cv_id="cv-safe",
        requested_cv_id="cv-safe",
        requested_cv_hash="c" * 64,
        attached_cv_id="cv-safe",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
        profile_version=1,
    )
    db.add(attempt)
    db.flush()
    permit = FinalSubmitPermit(
        attempt_id=attempt.id,
        nonce_hash="n" * 64,
        job_url_hash="u" * 64,
        application_revision=1,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_plan_fingerprint=plan.fingerprint,
        cv_hash="c" * 64,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    command = SubmissionCommand(
        attempt_id=attempt.id,
        idempotency_key=attempt.idempotency_key,
        state="claimed",
        available_at=now,
        claimed_at=now,
        claimed_by="test-runner",
        claim_token="t" * 64,
    )
    db.add_all([permit, command])
    db.commit()
    result = (attempt.id, plan.id, command.id, permit.id)
    db.close()
    return result


@pytest.mark.parametrize(
    "intent",
    (
        ApplicationMutationIntent.CONTENT,
        ApplicationMutationIntent.PREPARE,
    ),
)
def test_content_and_prepare_reject_every_unfinished_attempt(tmp_path, intent):
    factory = _factory(tmp_path)
    application_id, _job_id = _application(factory)
    attempt_id, _plan_id, command_id, _permit_id = _precommit_attempt(
        factory,
        application_id,
    )
    db = factory()

    with pytest.raises(
        ApplicationMutationBlockedError,
        match="SUBMISSION_ALREADY_ACTIVE",
    ):
        lock_application_for_mutation(
            db,
            application_id=application_id,
            intent=intent,
        )
    db.rollback()

    assert db.get(Submission, attempt_id).stage == "queued"
    assert db.get(SubmissionCommand, command_id).state == "claimed"
    db.close()


def test_skip_cancels_only_precommit_work_and_invalidates_review(tmp_path):
    factory = _factory(tmp_path)
    application_id, job_id = _application(factory)
    attempt_id, plan_id, command_id, permit_id = _precommit_attempt(
        factory,
        application_id,
    )
    db = factory()
    locked = lock_application_for_mutation(
        db,
        application_id=application_id,
        intent=ApplicationMutationIntent.TERMINAL,
    )
    assert locked is not None

    changed = transition_locked_application_to_skipped(
        db,
        locked,
        actor="operator",
        reason_code="OPERATOR_CANCELLED",
        rejection_reason="Operator skipped test fixture",
    )
    db.commit()

    assert changed is True
    application = db.get(Application, application_id)
    attempt = db.get(Submission, attempt_id)
    assert application.status == JobStatus.SKIPPED
    assert application.revision == 2
    assert application.prepared_revision is None
    assert db.get(Job, job_id).status == JobStatus.SKIPPED
    assert attempt.stage == "finished"
    assert attempt.outcome == "failed_before_commit"
    assert attempt.status == SubmissionStatus.FAILED
    assert attempt.reason_code == "OPERATOR_CANCELLED"
    assert db.get(SubmissionCommand, command_id).state == "cancelled"
    cancelled_command = db.get(SubmissionCommand, command_id)
    assert cancelled_command.claimed_at is None
    assert cancelled_command.claimed_by is None
    assert cancelled_command.claim_token is None
    assert db.get(FinalSubmitPermit, permit_id).consumed_at is None
    assert db.get(FormPlan, plan_id).invalidated_at is not None
    assert [
        event.event_type
        for event in db.query(ApplicationEvent)
        .filter(ApplicationEvent.application_id == application_id)
        .order_by(ApplicationEvent.id)
    ] == ["submission_attempt_cancelled", "application_rejected"]
    db.close()


def test_consumed_or_committing_attempt_can_never_be_cancelled(tmp_path):
    factory = _factory(tmp_path)
    application_id, _job_id = _application(factory)
    attempt_id, _plan_id, command_id, permit_id = _precommit_attempt(
        factory,
        application_id,
    )
    crossed_at = datetime.now(UTC).replace(tzinfo=None)
    db = factory()
    attempt = db.get(Submission, attempt_id)
    attempt.stage = "committing"
    attempt.status = SubmissionStatus.RUNNING
    attempt.final_action_at = crossed_at
    db.get(FinalSubmitPermit, permit_id).consumed_at = crossed_at
    db.commit()

    with pytest.raises(
        ApplicationMutationBlockedError,
        match="FINAL_ACTION_INDETERMINATE",
    ):
        lock_application_for_mutation(
            db,
            application_id=application_id,
            intent=ApplicationMutationIntent.TERMINAL,
        )
    db.rollback()

    assert db.get(Submission, attempt_id).stage == "committing"
    assert db.get(SubmissionCommand, command_id).state == "claimed"
    assert db.get(FinalSubmitPermit, permit_id).consumed_at is not None
    assert db.get(Application, application_id).status == JobStatus.DRAFT
    db.close()


def test_stale_expiry_uses_the_same_safe_precommit_cancellation(tmp_path):
    factory = _factory(tmp_path)
    application_id, job_id = _application(factory)
    attempt_id, plan_id, command_id, _permit_id = _precommit_attempt(
        factory,
        application_id,
    )
    db = factory()

    assert (
        expire_stale_jobs(
            db,
            now=datetime.now(UTC).replace(tzinfo=None),
            ttl_days=7,
        )
        == 1
    )

    assert db.get(Application, application_id).status == JobStatus.SKIPPED
    assert db.get(Job, job_id).status == JobStatus.SKIPPED
    assert db.get(Submission, attempt_id).outcome == "failed_before_commit"
    assert db.get(Submission, attempt_id).reason_code == "COMMAND_EXPIRED"
    assert db.get(SubmissionCommand, command_id).state == "cancelled"
    assert db.get(FormPlan, plan_id).invalidated_at is not None
    db.close()


def test_stale_expiry_never_overwrites_a_committing_application(tmp_path):
    factory = _factory(tmp_path)
    application_id, job_id = _application(factory)
    attempt_id, _plan_id, command_id, permit_id = _precommit_attempt(
        factory,
        application_id,
    )
    crossed_at = datetime.now(UTC).replace(tzinfo=None)
    db = factory()
    attempt = db.get(Submission, attempt_id)
    attempt.stage = "committing"
    attempt.status = SubmissionStatus.RUNNING
    attempt.final_action_at = crossed_at
    db.get(FinalSubmitPermit, permit_id).consumed_at = crossed_at
    db.commit()

    assert expire_stale_jobs(db, now=crossed_at, ttl_days=7) == 0
    assert db.get(Application, application_id).status == JobStatus.DRAFT
    assert db.get(Job, job_id).status == JobStatus.DRAFT
    assert db.get(Submission, attempt_id).stage == "committing"
    assert db.get(SubmissionCommand, command_id).state == "claimed"
    db.close()


@pytest.mark.parametrize("intent", tuple(ApplicationMutationIntent))
def test_unknown_outcome_blocks_every_mutation_kind(tmp_path, intent):
    factory = _factory(tmp_path)
    application_id, _job_id = _application(factory)
    db = factory()
    db.add(
        Submission(
            application_id=application_id,
            attempt_number=1,
            idempotency_key=f"unknown-{uuid4().hex}",
            submitter_name="greenhouse",
            status=SubmissionStatus.UNKNOWN,
            stage="finished",
            outcome="unknown",
            reason_code="FINAL_ACTION_UNCONFIRMED",
        )
    )
    db.commit()

    with pytest.raises(
        ApplicationMutationBlockedError,
        match="SUBMISSION_OUTCOME_UNKNOWN",
    ):
        lock_application_for_mutation(
            db,
            application_id=application_id,
            intent=intent,
        )
    db.close()


def test_older_unknown_cannot_be_hidden_by_a_later_draft_attempt(tmp_path):
    factory = _factory(tmp_path)
    application_id, _job_id = _application(factory)
    db = factory()
    db.add_all(
        [
            Submission(
                application_id=application_id,
                attempt_number=1,
                idempotency_key="older-unknown",
                submitter_name="greenhouse",
                status=SubmissionStatus.UNKNOWN,
                stage="finished",
                outcome="unknown",
                reason_code="FINAL_ACTION_UNCONFIRMED",
            ),
            Submission(
                application_id=application_id,
                attempt_number=2,
                idempotency_key="newer-draft",
                submitter_name="greenhouse",
                status=SubmissionStatus.DRAFT_ONLY,
                stage="finished",
                outcome="draft_only",
                reason_code="DRY_RUN_DISCARDED",
            ),
        ]
    )
    db.commit()

    with pytest.raises(
        ApplicationMutationBlockedError,
        match="SUBMISSION_OUTCOME_UNKNOWN",
    ):
        lock_application_for_mutation(
            db,
            application_id=application_id,
            intent=ApplicationMutationIntent.PREPARE,
        )
    db.rollback()

    assert db.get(Application, application_id).revision == 1
    assert db.query(Submission).count() == 2
    db.close()


def test_expected_revision_prevents_late_llm_overwrite(tmp_path):
    factory = _factory(tmp_path)
    application_id, _job_id = _application(factory)
    db = factory()
    application = db.get(Application, application_id)
    application.cover_letter = "new operator content"
    application.revision = 2
    db.commit()

    with pytest.raises(
        ApplicationMutationBlockedError,
        match="APPLICATION_REVISION_CHANGED",
    ):
        lock_application_for_mutation(
            db,
            application_id=application_id,
            intent=ApplicationMutationIntent.CONTENT,
            expected_revision=1,
        )
    db.rollback()

    assert db.get(Application, application_id).cover_letter == "new operator content"
    db.close()


def test_populate_existing_prevents_stale_prepare_from_resurrecting_skip(tmp_path):
    factory = _factory(tmp_path)
    application_id, _job_id = _application(factory)
    stale = factory()
    assert stale.get(Application, application_id).status == JobStatus.DRAFT

    reject = factory()
    locked = lock_application_for_mutation(
        reject,
        application_id=application_id,
        intent=ApplicationMutationIntent.TERMINAL,
    )
    assert locked is not None
    transition_locked_application_to_skipped(
        reject,
        locked,
        actor="operator",
        reason_code="OPERATOR_CANCELLED",
        rejection_reason="Concurrent rejection",
    )
    reject.commit()
    reject.close()

    with pytest.raises(ApplicationMutationBlockedError, match="APPLICATION_TERMINAL"):
        lock_application_for_mutation(
            stale,
            application_id=application_id,
            intent=ApplicationMutationIntent.PREPARE,
        )
    stale.rollback()
    assert stale.get(Application, application_id).status == JobStatus.SKIPPED
    stale.close()


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL integration test",
)
def test_postgres_stale_prepare_cannot_resurrect_concurrent_skip():
    engine = create_engine(os.environ["DATABASE_URL"])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    suffix = uuid4().hex
    seed = factory()
    job = Job(
        title="Postgres mutation race",
        source_url=f"https://example.test/{suffix}",
        status=JobStatus.DRAFT,
    )
    application = Application(
        job=job,
        status=JobStatus.DRAFT,
        selected_cv_id="cv-safe",
        revision=1,
    )
    seed.add(application)
    seed.commit()
    application_id, job_id = application.id, job.id
    seed.close()

    stale = factory()
    stale.get(Application, application_id)
    reject = factory()
    try:
        locked = lock_application_for_mutation(
            reject,
            application_id=application_id,
            intent=ApplicationMutationIntent.TERMINAL,
        )
        assert locked is not None
        transition_locked_application_to_skipped(
            reject,
            locked,
            actor="operator",
            reason_code="OPERATOR_CANCELLED",
            rejection_reason="Concurrent PostgreSQL rejection",
        )
        reject.commit()

        with pytest.raises(
            ApplicationMutationBlockedError,
            match="APPLICATION_TERMINAL",
        ):
            lock_application_for_mutation(
                stale,
                application_id=application_id,
                intent=ApplicationMutationIntent.PREPARE,
            )
        stale.rollback()
        assert stale.get(Application, application_id).status == JobStatus.SKIPPED
    finally:
        stale.close()
        reject.close()
        cleanup = factory()
        saved = cleanup.get(Application, application_id)
        if saved is not None:
            cleanup.delete(saved)
            cleanup.flush()
        saved_job = cleanup.get(Job, job_id)
        if saved_job is not None:
            cleanup.delete(saved_job)
        cleanup.commit()
        cleanup.close()


def test_prepare_transition_requires_the_locked_current_revision(tmp_path):
    factory = _factory(tmp_path)
    application_id, job_id = _application(factory)
    db = factory()
    locked = lock_application_for_mutation(
        db,
        application_id=application_id,
        intent=ApplicationMutationIntent.PREPARE,
    )
    assert locked is not None
    changed = mark_locked_application_prepared(
        db,
        locked,
        actor="operator",
        source="manual_prepare",
    )
    db.commit()

    assert changed is True
    application = db.get(Application, application_id)
    assert application.status == JobStatus.DRAFT
    assert application.prepared_revision == application.revision == 1
    assert db.get(Job, job_id).status == JobStatus.DRAFT
    db.close()


@pytest.mark.asyncio
async def test_cv_preview_and_override_refuse_an_active_attempt(tmp_path, monkeypatch):
    factory = _factory(tmp_path)
    application_id, _job_id = _application(factory)
    _attempt_id, _plan_id, command_id, _permit_id = _precommit_attempt(
        factory,
        application_id,
    )
    monkeypatch.setattr(
        cv_routing_route,
        "_config",
        lambda: SimpleNamespace(cvs=[SimpleNamespace(id="cv-other")]),
    )
    db = factory()

    with pytest.raises(HTTPException) as preview_error:
        await cv_routing_route.preview_cv_routing(
            cv_routing_route.RoutingPreviewRequest(
                title="Engineer",
                application_id=application_id,
            ),
            db,
        )
    with pytest.raises(HTTPException) as override_error:
        await cv_routing_route.override_application_cv(
            application_id,
            cv_routing_route.CVOverrideRequest(cv_id="cv-other"),
            db,
        )

    assert preview_error.value.status_code == 409
    assert override_error.value.status_code == 409
    assert db.get(Application, application_id).selected_cv_id == "cv-safe"
    assert db.get(SubmissionCommand, command_id).state == "claimed"
    db.close()


@pytest.mark.asyncio
async def test_realign_rechecks_revision_after_llm_without_holding_a_lock(tmp_path):
    factory = _factory(tmp_path)
    application_id, _job_id = _application(factory)
    seed = factory()
    seed.get(Application, application_id).cover_letter = "operator-owned original"
    seed.commit()
    seed.close()

    async def reject_during_generation(*_args, **_kwargs):
        concurrent = factory()
        locked = lock_application_for_mutation(
            concurrent,
            application_id=application_id,
            intent=ApplicationMutationIntent.TERMINAL,
        )
        assert locked is not None
        transition_locked_application_to_skipped(
            concurrent,
            locked,
            actor="operator",
            reason_code="OPERATOR_CANCELLED",
            rejection_reason="Rejected while LLM was running",
        )
        concurrent.commit()
        concurrent.close()
        return GeneratedApplication(
            cover_letter="stale generated replacement",
            recruiter_message="stale",
            qa_answers={},
        )

    db = factory()
    with (
        patch.object(
            realign_route,
            "get_settings",
            return_value=Settings(
                _env_file=None,
                cv_routing_path=str(tmp_path / "missing-routing.yaml"),
                cv_directory=str(tmp_path),
            ),
        ),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch.object(
            realign_route,
            "generate_full_application",
            new=AsyncMock(side_effect=reject_during_generation),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await realign_route.realign_application(
                application_id,
                realign_route.RealignRequest(),
                db,
            )

    assert exc.value.status_code == 409
    db.rollback()
    application = db.get(Application, application_id)
    assert application.status == JobStatus.SKIPPED
    assert application.cover_letter == "operator-owned original"
    db.close()


@pytest.mark.asyncio
async def test_realign_binds_generated_content_to_latest_profile_version(
    tmp_path,
):
    factory = _factory(tmp_path)
    application_id, _job_id = _application(factory)
    seed = factory()
    seed.add(UserProfileVersion(profile_yaml="personal:\n  name: Profile v2\n", version=2))
    seed.commit()
    seed.close()
    generated = GeneratedApplication(
        cover_letter="profile v2 content",
        recruiter_message="profile v2 message",
        qa_answers={},
    )

    db = factory()
    with (
        patch.object(
            realign_route,
            "get_settings",
            return_value=Settings(
                _env_file=None,
                cv_routing_path=str(tmp_path / "missing-routing.yaml"),
                cv_directory=str(tmp_path),
            ),
        ),
        patch.object(
            realign_route,
            "generate_full_application",
            new=AsyncMock(return_value=generated),
        ),
    ):
        await realign_route.realign_application(
            application_id,
            realign_route.RealignRequest(),
            db,
        )

    application = db.get(Application, application_id)
    assert application.profile_version == 2
    assert application.cover_letter == "profile v2 content"
    assert application.revision == 2
    event = (
        db.query(ApplicationEvent)
        .filter(
            ApplicationEvent.application_id == application_id,
            ApplicationEvent.event_type == "application_realigned",
        )
        .one()
    )
    assert '"profile_version":2' in (event.details or "")
    db.close()


@pytest.mark.asyncio
async def test_realign_rejects_profile_change_during_generation(tmp_path):
    factory = _factory(tmp_path)
    application_id, _job_id = _application(factory)
    seed = factory()
    application = seed.get(Application, application_id)
    application.cover_letter = "profile v1 content"
    seed.add(UserProfileVersion(profile_yaml="personal:\n  name: Profile v1\n", version=1))
    seed.commit()
    seed.close()

    async def update_profile_during_generation(*_args, **_kwargs):
        concurrent = factory()
        concurrent.add(
            UserProfileVersion(profile_yaml="personal:\n  name: Profile v2\n", version=2)
        )
        concurrent.commit()
        concurrent.close()
        return GeneratedApplication(
            cover_letter="stale profile v1 replacement",
            recruiter_message="stale",
            qa_answers={},
        )

    db = factory()
    with (
        patch.object(
            realign_route,
            "get_settings",
            return_value=Settings(
                _env_file=None,
                cv_routing_path=str(tmp_path / "missing-routing.yaml"),
                cv_directory=str(tmp_path),
            ),
        ),
        patch.object(
            realign_route,
            "generate_full_application",
            new=AsyncMock(side_effect=update_profile_during_generation),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await realign_route.realign_application(
                application_id,
                realign_route.RealignRequest(),
                db,
            )

    assert exc.value.status_code == 409
    assert exc.value.detail == "PROFILE_VERSION_CHANGED"
    db.rollback()
    application = db.get(Application, application_id)
    assert application.profile_version == 1
    assert application.cover_letter == "profile v1 content"
    assert application.revision == 1
    db.close()


@pytest.mark.asyncio
async def test_generation_rechecks_terminal_state_after_llm(tmp_path):
    factory = _factory(tmp_path)
    application_id, job_id = _application(factory)
    seed = factory()
    seed.get(Application, application_id).cover_letter = "existing protected content"
    seed.commit()
    seed.close()

    async def reject_during_generation(*_args, **_kwargs):
        concurrent = factory()
        locked = lock_application_for_mutation(
            concurrent,
            application_id=application_id,
            intent=ApplicationMutationIntent.TERMINAL,
        )
        assert locked is not None
        transition_locked_application_to_skipped(
            concurrent,
            locked,
            actor="operator",
            reason_code="OPERATOR_CANCELLED",
            rejection_reason="Rejected during generated draft",
        )
        concurrent.commit()
        concurrent.close()
        return GeneratedApplication(
            cover_letter="stale worker replacement",
            recruiter_message="stale",
            qa_answers={},
        )

    settings = Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=False,
        cv_routing_path="does-not-exist.yaml",
    )
    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch(
            "llm.generation.generate_full_application",
            new=AsyncMock(side_effect=reject_during_generation),
        ),
    ):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id])

    db = factory()
    application = db.get(Application, application_id)
    assert application.status == JobStatus.SKIPPED
    assert application.cover_letter == "existing protected content"
    db.close()


def test_generation_rejects_profile_change_during_llm(tmp_path):
    factory = _factory(tmp_path)
    application_id, job_id = _application(factory)
    seed = factory()
    application = seed.get(Application, application_id)
    application.cover_letter = "profile v1 generated content"
    seed.add(UserProfileVersion(profile_yaml="personal:\n  name: Profile v1\n", version=1))
    seed.commit()
    seed.close()

    async def update_profile_during_generation(*_args, **_kwargs):
        concurrent = factory()
        concurrent.add(
            UserProfileVersion(profile_yaml="personal:\n  name: Profile v2\n", version=2)
        )
        concurrent.commit()
        concurrent.close()
        return GeneratedApplication(
            cover_letter="stale profile v1 replacement",
            recruiter_message="stale",
            qa_answers={},
        )

    settings = Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=False,
        cv_routing_path="does-not-exist.yaml",
    )
    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch(
            "llm.generation.generate_full_application",
            new=AsyncMock(side_effect=update_profile_during_generation),
        ),
    ):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id])

    db = factory()
    application = db.get(Application, application_id)
    assert application.profile_version == 1
    assert application.cover_letter == "profile v1 generated content"
    assert application.revision == 1
    db.close()


def test_generation_uses_exact_versioned_profile_not_stale_process_cache(
    tmp_path,
    monkeypatch,
):
    import profile.loader as profile_loader

    factory = _factory(tmp_path)
    _application_id, job_id = _application(factory)
    seed = factory()
    seed.add(
        UserProfileVersion(
            profile_yaml="personal:\n  name: Authoritative Profile v2\n",
            version=2,
        )
    )
    seed.commit()
    seed.close()

    monkeypatch.setattr(
        profile_loader,
        "_profile",
        UserProfile(personal={"name": "Stale Cached Profile v1"}),
    )
    observed_names: list[str] = []

    async def generate_from_snapshot(_job, profile, **_kwargs):
        observed_names.append(profile.personal.name)
        return GeneratedApplication(
            cover_letter=f"generated for {profile.personal.name}",
            recruiter_message="version-bound",
            qa_answers={},
        )

    settings = Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=False,
        cv_routing_path="does-not-exist.yaml",
    )
    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch(
            "llm.generation.generate_full_application",
            new=AsyncMock(side_effect=generate_from_snapshot),
        ),
    ):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id])

    db = factory()
    application = db.query(Application).filter(Application.job_id == job_id).one()
    assert observed_names == ["Authoritative Profile v2"]
    assert application.profile_version == 2
    assert application.cover_letter == "generated for Authoritative Profile v2"
    db.close()


def test_scoring_rechecks_application_revision_before_job_write(tmp_path):
    factory = _factory(tmp_path)
    application_id, job_id = _application(factory)

    def reject_during_scoring(*_args, **_kwargs):
        concurrent = factory()
        locked = lock_application_for_mutation(
            concurrent,
            application_id=application_id,
            intent=ApplicationMutationIntent.TERMINAL,
        )
        assert locked is not None
        transition_locked_application_to_skipped(
            concurrent,
            locked,
            actor="operator",
            reason_code="OPERATOR_CANCELLED",
            rejection_reason="Rejected while scoring was in progress",
        )
        concurrent.commit()
        concurrent.close()
        return SimpleNamespace(total=92.0, skip_reason=None)

    settings = Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=False,
        tasks_always_eager=False,
    )
    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch("worker.tasks.score_job", side_effect=reject_during_scoring),
        patch("worker.tasks.generate_application_task") as queued_generation,
    ):
        from worker.tasks import score_job_task

        score_job_task.apply(args=[job_id])

    db = factory()
    application = db.get(Application, application_id)
    job = db.get(Job, job_id)
    assert application.status == JobStatus.SKIPPED
    assert application.revision == 2
    assert job.status == JobStatus.SKIPPED
    assert job.score is None
    queued_generation.delay.assert_not_called()
    db.close()


def test_scoring_cannot_overwrite_application_created_during_scoring(tmp_path):
    factory = _factory(tmp_path)
    seed = factory()
    job = Job(
        title="Concurrent generation",
        company="Acme",
        location="Remote",
        description="Generation wins the race",
        requirements="Python",
        source_url="https://example.test/concurrent-generation",
        apply_url="https://example.test/concurrent-generation",
        status=JobStatus.EXTRACTED,
    )
    seed.add(job)
    seed.commit()
    job_id = job.id
    seed.close()

    def generate_application_during_scoring(*_args, **_kwargs):
        concurrent = factory()
        current_job = concurrent.get(Job, job_id)
        current_job.status = JobStatus.DRAFT
        concurrent.add(
            Application(
                job_id=job_id,
                cover_letter="newly generated content",
                status=JobStatus.DRAFT,
                revision=1,
            )
        )
        concurrent.commit()
        concurrent.close()
        # A stale low score used to overwrite the generated draft with SKIPPED.
        return SimpleNamespace(total=0.0, skip_reason="stale scoring decision")

    settings = Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=False,
        tasks_always_eager=False,
    )
    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch(
            "worker.tasks.score_job",
            side_effect=generate_application_during_scoring,
        ),
        patch("worker.tasks.generate_application_task") as queued_generation,
    ):
        from worker.tasks import score_job_task

        score_job_task.apply(args=[job_id])

    db = factory()
    application = db.query(Application).filter(Application.job_id == job_id).one()
    job = db.get(Job, job_id)
    assert application.status == JobStatus.DRAFT
    assert application.cover_letter == "newly generated content"
    assert job.status == JobStatus.DRAFT
    assert job.score is None
    queued_generation.delay.assert_not_called()
    db.close()


def test_discovery_scoring_stops_before_generation_when_preparation_is_blocked(
    tmp_path,
):
    factory = _factory(tmp_path)
    db = factory()
    job = Job(
        title="Discovery-only role",
        company="Acme",
        location="Israel",
        description="Score this job without preparing private materials.",
        requirements="Python",
        source_url="https://example.test/discovery-only",
        apply_url="https://example.test/discovery-only",
        status=JobStatus.EXTRACTED,
    )
    db.add(job)
    db.commit()
    job_id = job.id
    db.close()
    settings = Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=True,
        tasks_always_eager=False,
    )

    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch(
            "worker.tasks.score_job",
            return_value=SimpleNamespace(total=90.0, skip_reason=None),
        ),
        patch("worker.tasks.decide_action", return_value=Action.AUTO_APPLY),
        patch("worker.tasks.generate_application_task") as queued_generation,
    ):
        from worker.tasks import score_job_task

        score_job_task.apply(args=[job_id, False])

    db = factory()
    assert db.get(Job, job_id).status == JobStatus.SCORED
    assert db.query(Application).filter(Application.job_id == job_id).count() == 0
    queued_generation.delay.assert_not_called()
    queued_generation.apply.assert_not_called()
    db.close()


@pytest.mark.asyncio
async def test_whatsapp_prepare_blocks_active_work_and_skip_cancels_it(
    tmp_path,
    monkeypatch,
):
    factory = _factory(tmp_path)
    application_id, job_id = _application(factory)
    attempt_id, plan_id, command_id, _permit_id = _precommit_attempt(
        factory,
        application_id,
    )
    send = AsyncMock()
    monkeypatch.setattr(webhook_route, "_send_whatsapp_message", send)
    db = factory()

    await webhook_route._handle_approve(
        job_id,
        "operator",
        db,
        Settings(_env_file=None),
    )
    assert db.get(Application, application_id).approved_at is None
    assert db.get(SubmissionCommand, command_id).state == "claimed"

    await webhook_route._handle_skip(
        job_id,
        "operator",
        db,
        Settings(_env_file=None),
    )

    assert db.get(Application, application_id).status == JobStatus.SKIPPED
    assert db.get(Submission, attempt_id).outcome == "failed_before_commit"
    assert db.get(SubmissionCommand, command_id).state == "cancelled"
    assert db.get(FormPlan, plan_id).invalidated_at is not None
    messages = [call.args[1] for call in send.await_args_list]
    assert any("dashboard review or reconciliation" in message for message in messages)
    db.close()
