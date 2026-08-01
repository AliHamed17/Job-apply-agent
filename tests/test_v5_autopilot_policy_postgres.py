"""PostgreSQL contention coverage for qualified-autopilot inspection leases."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import (
    Application,
    AutomationPolicyRevisionRecord,
    AutopilotInspectionRun,
    Job,
    JobStatus,
)
from worker.autopilot_inspection import (
    _claim_inspection_run,
    enqueue_qualified_autopilot_inspection,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _factory():
    engine = create_engine(os.environ["DATABASE_URL"])
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def test_two_workers_create_and_claim_one_exact_inspection(monkeypatch) -> None:
    engine, factory = _factory()
    now = datetime.now(UTC)
    db = factory()
    suffix = uuid4().hex
    job = Job(
        title="Autopilot contention proof",
        source_url=f"https://example.test/{suffix}",
        apply_url=f"https://example.test/{suffix}",
        status=JobStatus.DRAFT,
    )
    application = Application(job=job, status=JobStatus.DRAFT, revision=1)
    policy = AutomationPolicyRevisionRecord(
        policy_id=str(uuid4()),
        revision=1,
        schema_version="auto-submit-policy.v1",
        payload_json="{}",
        payload_digest="a" * 64,
        signing_key_id=str(uuid4()),
        signature="A" * 86,
        active_slot=1,
        activated_at=now.replace(tzinfo=None),
        expires_at=(now + timedelta(days=30)).replace(tzinfo=None),
    )
    db.add_all([application, policy])
    db.commit()
    application_id = application.id
    application_revision = application.revision
    job_id = job.id
    policy_id = policy.id
    db.close()

    monkeypatch.setattr(
        "worker.autopilot_inspection.validate_automation_inspection_candidate",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "worker.autopilot_inspection.current_signed_policy",
        lambda *_args, **_kwargs: (SimpleNamespace(id=policy_id), object()),
    )
    enqueue_barrier = Barrier(2)

    def enqueue():
        session = factory()
        try:
            enqueue_barrier.wait()
            return enqueue_qualified_autopilot_inspection(
                session,
                application_id=application_id,
                now=now,
            )
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            enqueued = list(pool.map(lambda _index: enqueue(), range(2)))
        assert {result.run_id for result in enqueued} == {enqueued[0].run_id}
        assert sum(result.replayed for result in enqueued) == 1
        assert enqueued[0].run_id is not None
        run_id = enqueued[0].run_id

        claim_barrier = Barrier(2)

        def claim():
            session = factory()
            try:
                claim_barrier.wait()
                return _claim_inspection_run(session, run_id=run_id, now=now)
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _index: claim(), range(2)))
        accepted = [item for item in claims if item is not None]
        assert len(accepted) == 1
        assert accepted[0][0] == application_id

        verify = factory()
        try:
            rows = (
                verify.query(AutopilotInspectionRun)
                .filter(
                    AutopilotInspectionRun.application_id == application_id,
                    AutopilotInspectionRun.application_revision == application_revision,
                    AutopilotInspectionRun.policy_revision_id == policy_id,
                )
                .all()
            )
            assert len(rows) == 1
            assert rows[0].state == "running"
            assert rows[0].claim_token == accepted[0][1]
        finally:
            verify.close()
    finally:
        cleanup = factory()
        try:
            cleanup.query(AutopilotInspectionRun).filter(
                AutopilotInspectionRun.application_id == application_id
            ).delete(synchronize_session=False)
            cleanup.query(Application).filter(Application.id == application_id).delete(
                synchronize_session=False
            )
            cleanup.query(Job).filter(Job.id == job_id).delete(synchronize_session=False)
            cleanup.query(AutomationPolicyRevisionRecord).filter(
                AutomationPolicyRevisionRecord.id == policy_id
            ).delete(synchronize_session=False)
            cleanup.commit()
        finally:
            cleanup.close()
            engine.dispose()
