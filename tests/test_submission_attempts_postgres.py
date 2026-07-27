"""PostgreSQL serialization coverage for the v4 submission command path."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from profile.models import UserProfile
from profile.writer import save_profile
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.applications import ReconcileRequest, reconcile_submission_attempt
from core.config import Settings
from core.runtime_identity import get_runtime_identity
from core.submission_domain import (
    PreparedFinalActionV1,
    ReasonCode,
    UnknownOutcome,
)
from core.submission_service import (
    ClientReleaseIdentity,
    SubmissionAdmissionError,
    SubmissionCommandRequest,
    create_submission_commands,
)
from db.models import (
    Application,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
    UserProfileVersion,
)
from llm.qualification_registry import load_qualified_local_model
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    QualificationTier,
    adapter_for_url,
)
from worker.submission_commands import (
    claim_submission_command,
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


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL integration test",
)


class _UnlockedProfileWrites:
    """Let PostgreSQL advisory locking, rather than the process lock, arbitrate."""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _factory():
    engine = create_engine(os.environ["DATABASE_URL"])
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_postgres_serializes_profile_versions_across_runner_sessions(
    tmp_path,
    monkeypatch,
):
    import profile.writer as profile_writer

    factory = _factory()
    barrier = Barrier(2)
    yaml_path = tmp_path / "profile.yaml"

    monkeypatch.setattr(
        profile_writer,
        "_PROFILE_WRITE_LOCK",
        _UnlockedProfileWrites(),
    )

    def write(name: str) -> int:
        profile = UserProfile()
        profile.personal.name = name
        db = factory()
        try:
            barrier.wait()
            return save_profile(profile, yaml_path, db=db)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = list(pool.map(write, ("Postgres one", "Postgres two")))

    assert len(set(versions)) == 2
    assert max(versions) - min(versions) == 1
    db = factory()
    try:
        rows = db.query(UserProfileVersion).filter(UserProfileVersion.version.in_(versions)).all()
        assert len(rows) == 2
        assert {row.version for row in rows} == set(versions)
        db.query(UserProfileVersion).filter(UserProfileVersion.version.in_(versions)).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def _seed_reviewed(factory):
    # Greenhouse candidate job identifiers are numeric. Keep the seed unique
    # without weakening canonical adapter routing with a synthetic hex ID.
    suffix = str(uuid4().int % (10**20))
    now = datetime.now(UTC).replace(tzinfo=None)
    url = f"https://boards.greenhouse.io/acme/jobs/{suffix}"
    descriptor = adapter_for_url(url)
    assert descriptor is not None
    fingerprint = "f" * 64
    live_descriptor = replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(fingerprint,),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    )
    db = factory()
    job = Job(
        title="Concurrent command test",
        company="Acme",
        source_url=url,
        apply_url=url,
        status=JobStatus.DRAFT,
    )
    application = Application(
        job=job,
        status=JobStatus.DRAFT,
        selected_cv_id="cv-test",
        selected_cv_hash="c" * 64,
        profile_version=1,
        material_eligible=True,
        material_blockers_json="[]",
        material_model_provider="ollama",
        material_model_name="qwen2.5:7b",
        material_model_digest=_QUALIFIED_MODEL_DIGEST,
        material_prompt_version="application-materials-v1",
        revision=1,
        prepared_revision=1,
        approved_at=now,
        approval_source="manual_prepare",
    )
    db.add(application)
    db.flush()
    if db.query(UserProfileVersion.id).filter(UserProfileVersion.version == 1).first() is None:
        db.add(
            UserProfileVersion(
                profile_yaml="personal:\n  name: PostgreSQL Candidate\n",
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
        selected_cv_id="cv-test",
        selected_cv_hash="c" * 64,
        attached_cv_id="cv-test",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
        profile_version=1,
        fields_json="[]",
        decisions_json="[]",
        blockers_json="[]",
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(plan)
    db.commit()
    seeded = {
        "application_id": application.id,
        "job_id": job.id,
        "plan_id": plan.plan_id,
        "now": now,
        "descriptor": live_descriptor,
    }
    db.close()
    return seeded


def _cleanup(factory, application_id: int, job_id: int) -> None:
    db = factory()
    try:
        application = db.get(Application, application_id)
        if application is not None:
            db.delete(application)
            db.flush()
        job = db.get(Job, job_id)
        if job is not None:
            db.delete(job)
        db.commit()
    finally:
        db.close()


def _seed_unknown_attempt(factory):
    suffix = uuid4().hex
    db = factory()
    job = Job(
        title="Concurrent reconciliation test",
        source_url=f"https://example.test/{suffix}",
        status=JobStatus.NEEDS_REVIEW,
    )
    application = Application(job=job, status=JobStatus.NEEDS_REVIEW)
    db.add(application)
    db.flush()
    attempt = Submission(
        application_id=application.id,
        attempt_number=1,
        idempotency_key=f"reconcile-{suffix}",
        submitter_name="test",
        status="unknown",
        stage="finished",
        outcome="unknown",
        application_revision=1,
        reason_code="STALE_INDETERMINATE",
    )
    db.add(attempt)
    db.commit()
    seeded = {
        "application_id": application.id,
        "job_id": job.id,
        "attempt_id": attempt.id,
    }
    db.close()
    return seeded


def _admit(factory, seeded, key: str):
    identity = get_runtime_identity()
    db = factory()
    try:
        [created] = create_submission_commands(
            db,
            [
                SubmissionCommandRequest(
                    application_id=seeded["application_id"],
                    client_idempotency_key=key,
                    application_revision=1,
                    form_plan_id=seeded["plan_id"],
                    client_release=ClientReleaseIdentity(
                        build_sha=identity.build_sha,
                        ui_asset_digest=identity.ui_asset_digest,
                        source_digest=identity.source_digest,
                        protocol_version=identity.protocol_version,
                        boot_id=identity.boot_id,
                    ),
                )
            ],
            settings=Settings(
                _env_file=None,
                dry_run=False,
                draft_only=False,
                auto_apply=False,
                portal_final_submit_enabled=True,
                live_automation_acknowledged=True,
                secret_key="operator-auth-test-secret-" + "x" * 32,
            ),
            capabilities={
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
            },
            descriptor_resolver=lambda _url: seeded["descriptor"],
            session_checker=lambda *_args: True,
            now=seeded["now"],
        )
        return created
    finally:
        db.close()


def test_concurrent_same_key_clicks_create_one_durable_command():
    factory = _factory()
    seeded = _seed_reviewed(factory)
    gate = Barrier(2)

    def admit():
        gate.wait()
        return _admit(factory, seeded, "same-operator-click")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _unused: admit(), range(2)))
        assert results[0].attempt_id == results[1].attempt_id
        assert results[0].command_id == results[1].command_id
        assert sum(not result.replayed for result in results) == 1
        db = factory()
        assert (
            db.query(Submission)
            .filter(Submission.application_id == seeded["application_id"])
            .count()
            == 1
        )
        assert db.query(SubmissionCommand).count() >= 1
        assert (
            db.query(SubmissionCommand)
            .join(Submission)
            .filter(Submission.application_id == seeded["application_id"])
            .count()
            == 1
        )
        db.close()
    finally:
        _cleanup(factory, seeded["application_id"], seeded["job_id"])


def test_concurrent_distinct_clicks_allow_one_active_attempt():
    factory = _factory()
    seeded = _seed_reviewed(factory)
    gate = Barrier(2)

    def admit(index: int):
        gate.wait()
        try:
            return _admit(factory, seeded, f"operator-click-{index}")
        except SubmissionAdmissionError as exc:
            return exc.reason_code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(admit, range(2)))
        assert sum(not isinstance(result, str) for result in results) == 1
        assert results.count("SUBMISSION_ALREADY_ACTIVE") == 1
        db = factory()
        assert (
            db.query(Submission)
            .filter(Submission.application_id == seeded["application_id"])
            .count()
            == 1
        )
        db.close()
    finally:
        _cleanup(factory, seeded["application_id"], seeded["job_id"])


def test_two_workers_claim_exactly_one_external_action_command():
    factory = _factory()
    seeded = _seed_reviewed(factory)
    created = _admit(factory, seeded, "claim-once")
    gate = Barrier(2)

    def claim(runner: str):
        db = factory()
        try:
            gate.wait()
            return claim_submission_command(
                db,
                command_id=created.command_id,
                runner_id=runner,
                now=seeded["now"] + timedelta(seconds=1),
            )
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("runner-one", "runner-two")))
        assert results.count(created.command_id) == 1
        assert results.count(None) == 1
        db = factory()
        command = db.get(SubmissionCommand, created.command_id)
        assert command.state == "claimed"
        assert command.claimed_by in {"runner-one", "runner-two"}
        assert command.attempt.stage == "inspecting"
        db.close()
    finally:
        _cleanup(factory, seeded["application_id"], seeded["job_id"])


def test_concurrent_reconciliation_has_one_deterministic_winner():
    factory = _factory()
    seeded = _seed_unknown_attempt(factory)
    gate = Barrier(2)

    def reconcile(outcome: str):
        db = factory()
        try:
            gate.wait()
            return asyncio.run(
                reconcile_submission_attempt(
                    seeded["attempt_id"],
                    ReconcileRequest(
                        outcome=outcome,
                        note=f"Operator checked {outcome}.",
                        reference=f"reference-{outcome}",
                    ),
                    db,
                )
            )
        except HTTPException as exc:
            return exc.status_code
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    reconcile,
                    ("confirmed_submitted", "confirmed_not_submitted"),
                )
            )
        assert sum(isinstance(result, dict) for result in results) == 1
        assert results.count(409) == 1
        winner = next(result for result in results if isinstance(result, dict))

        db = factory()
        attempt = db.get(Submission, seeded["attempt_id"])
        assert attempt.outcome == winner["outcome"]
        assert attempt.reconciliation_evidence_ref == (
            f"reference-{winner['reconciliation_result']}"
        )
        db.close()
    finally:
        _cleanup(factory, seeded["application_id"], seeded["job_id"])


def test_stale_reconciler_skips_app_locked_by_worker_without_deadlock():
    factory = _factory()
    seeded = _seed_reviewed(factory)
    created = _admit(factory, seeded, "stale-lock-order")
    claimed_at = seeded["now"] + timedelta(seconds=1)
    claimed = factory()
    assert (
        claim_submission_command(
            claimed,
            command_id=created.command_id,
            now=claimed_at,
        )
        == created.command_id
    )
    claimed.close()

    app_owner = factory()
    app_owner.query(Application).filter(
        Application.id == seeded["application_id"],
    ).with_for_update().one()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: _reconcile_once(
                    factory,
                    now=claimed_at + timedelta(minutes=20),
                )
            )
            assert future.result(timeout=5) == 0
    finally:
        app_owner.rollback()
        app_owner.close()

    try:
        assert (
            _reconcile_once(
                factory,
                now=claimed_at + timedelta(minutes=20),
            )
            == 1
        )
        db = factory()
        command = db.get(SubmissionCommand, created.command_id)
        assert command.state == "pending"
        assert command.attempt.stage == "queued"
        db.close()
    finally:
        _cleanup(factory, seeded["application_id"], seeded["job_id"])


def test_stale_reconciler_cannot_quarantine_a_live_final_action():
    factory = _factory()
    seeded = _seed_reviewed(factory)
    created = _admit(factory, seeded, "blocking-final-action")
    claimed = factory()
    assert (
        claim_submission_command(
            claimed,
            command_id=created.command_id,
            now=seeded["now"] + timedelta(seconds=1),
        )
        == created.command_id
    )
    claimed.close()

    commit_entered = Event()
    release_commit = Event()
    commit_calls: list[int] = []

    class BlockingExecutor:
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
                action_nonce="a" * 64,
            )

        async def commit(self, *, action, permit):
            del action, permit
            commit_calls.append(1)
            commit_entered.set()
            if not release_commit.wait(timeout=15):
                raise TimeoutError("test did not release the final action")
            return UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)

    class Registry:
        def resolve_final_executor(self, *_args, **_kwargs):
            return BlockingExecutor()

    class Governor:
        def reserve_final_action(self, **_kwargs):
            return True, "reserved"

    def execute():
        db = factory()
        try:
            return execute_claimed_submission_command(
                db,
                created.command_id,
                registry=Registry(),
                settings=Settings(
                    _env_file=None,
                    dry_run=False,
                    draft_only=False,
                    auto_apply=False,
                    portal_final_submit_enabled=True,
                    live_automation_acknowledged=True,
                ),
                governor=Governor(),
            )
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute)
            assert commit_entered.wait(timeout=10)

            stale_db = factory()
            try:
                assert (
                    reconcile_stale_submission_commands(
                        stale_db,
                        now=seeded["now"] + timedelta(hours=1),
                        stale_seconds=1,
                    )
                    == 0
                )
            finally:
                stale_db.close()

            second_claim = factory()
            try:
                assert (
                    claim_submission_command(
                        second_claim,
                        command_id=created.command_id,
                        now=seeded["now"] + timedelta(hours=1),
                    )
                    is None
                )
            finally:
                second_claim.close()

            release_commit.set()
            assert future.result(timeout=10) == "unknown"

        db = factory()
        assert commit_calls == [1]
        assert db.get(Submission, created.attempt_id).outcome == "unknown"
        db.close()
    finally:
        release_commit.set()
        _cleanup(factory, seeded["application_id"], seeded["job_id"])


def _reconcile_once(factory, *, now: datetime) -> int:
    db = factory()
    try:
        return reconcile_stale_submission_commands(
            db,
            now=now,
            stale_seconds=60,
        )
    finally:
        db.close()
