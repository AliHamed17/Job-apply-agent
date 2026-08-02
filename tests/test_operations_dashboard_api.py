"""Protected operations dashboard API truth and privacy coverage."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app
from api.routes import operations as operations_route
from api.routes.applications import _attempt_response
from core import dashboard_operations
from core.dashboard_operations import build_operations_snapshot
from db.models import (
    Application,
    Base,
    BrowserQualificationRun,
    DiscoveryRun,
    DiscoverySourceState,
    Job,
    JobFitDecisionRecord,
    JobSourceOccurrenceRecord,
    JobStatus,
    OperationalMetricEvent,
    OperationalMetricReceipt,
    Submission,
    SubmissionEvidence,
    SubmissionStatus,
)
from db.session import get_db


def _factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _readiness():
    return {
        "status": "degraded",
        "checks": {
            "database": {"ok": True},
            "migration": {"ok": False},
            "redis": {"ok": True},
            "worker": {
                "ok": False,
                "detail": "missing",
            },
            "beat": {"ok": False, "detail": "invalid"},
            "shared_storage": {"ok": True},
            "browser": {"ok": False, "age_seconds": 999.0},
            "llm": {
                "ok": False,
                "reason_code": "LLM_UNAVAILABLE",
            },
        },
    }


def _seed(factory, now: datetime) -> None:
    db = factory()
    try:
        rows = [
            DiscoveryRun(
                source="remotive",
                status="success",
                inserted=1,
                started_at=now - timedelta(hours=4),
                finished_at=now - timedelta(hours=3),
            ),
            DiscoveryRun(
                source="linkedin_search",
                status="failed",
                inserted=0,
                reason_code="SOURCE_UNAVAILABLE",
                started_at=now - timedelta(hours=2),
                finished_at=now - timedelta(hours=1),
            ),
            BrowserQualificationRun(
                adapter_name="greenhouse",
                adapter_version="retired-version",
                selector_version="candidate@example.com",
                terminal_reason="https://private.example/person",
                qualified=False,
                trace_json="{}",
                qualification_tier="disabled",
                fixture_digest="f" * 64,
                created_at=now - timedelta(minutes=30),
            ),
            OperationalMetricEvent(
                event_key="a" * 64,
                entity_key="b" * 64,
                metric_name="form_resolution",
                ats="candidate@example.com",
                adapter_version="+972501234567",
                selector_version="https://private.example/form",
                stage="none",
                outcome="none",
                reason_code="candidate@example.com",
                field_type="passport number",
                resolver="private answer",
                attachment_result="none",
                evidence_type="none",
                occurred_at=now - timedelta(minutes=20),
            ),
        ]
        for index in range(1, 251):
            token = hashlib.sha256(f"hostile-{index}".encode()).hexdigest()
            rows.append(
                OperationalMetricEvent(
                    event_key=token,
                    entity_key=hashlib.sha256(f"entity-{index}".encode()).hexdigest(),
                    metric_name="form_resolution",
                    ats=f"person{index}@x.test",
                    adapter_version=f"private-{index}",
                    selector_version=f"selector-private-{index}",
                    stage="none",
                    outcome="none",
                    reason_code=f"person{index}@x.test",
                    field_type=f"field-{index}",
                    resolver=f"answer-{index}",
                    attachment_result="none",
                    evidence_type="none",
                    occurred_at=now - timedelta(minutes=20),
                )
            )
        for index in range(1, 126):
            rows.append(
                BrowserQualificationRun(
                    adapter_name="greenhouse",
                    adapter_version=f"retired-{index}",
                    selector_version=f"private-selector-{index}",
                    terminal_reason=f"private-reason-{index}",
                    qualified=False,
                    trace_json="{}",
                    qualification_tier="disabled",
                    fixture_digest=hashlib.sha256(f"fixture-{index}".encode()).hexdigest(),
                    created_at=now - timedelta(minutes=30),
                )
            )
        db.add_all(rows)
        job = Job(
            title="Private title must not appear",
            company="Private company must not appear",
            source_url="https://private.example/job/123456",
            apply_url="https://private.example/apply/123456",
            status=JobStatus.NEEDS_REVIEW,
            created_at=now,
        )
        application = Application(job=job, status=JobStatus.NEEDS_REVIEW)
        db.add(application)
        db.flush()
        db.add(
            Submission(
                application_id=application.id,
                attempt_number=1,
                idempotency_key="operator-reconciled-attempt",
                submitter_name="greenhouse",
                status=SubmissionStatus.FAILED,
                stage="finished",
                outcome="operator_confirmed",
                reason_code="OPERATOR_CONFIRMED_SUBMITTED",
                started_at=now - timedelta(minutes=10),
                finished_at=now - timedelta(minutes=5),
                reconciled_at=now - timedelta(minutes=5),
                created_at=now - timedelta(minutes=15),
            )
        )
        db.commit()
    finally:
        db.close()


def test_snapshot_uses_successful_discovery_and_normalizes_every_dimension():
    engine, factory = _factory()
    now = datetime(2026, 7, 28, 12, 0, 0)
    _seed(factory, now)
    db = factory()
    try:
        snapshot = build_operations_snapshot(
            db,
            _readiness(),
            now=now,
            window_days=30,
        )
        assert snapshot["last_successful_discovery"] == {
            "finished_at": (now - timedelta(hours=3)).replace(tzinfo=UTC)
        }
        assert snapshot["failure_clusters"][0] == {
            "reason_code": "OTHER",
            "ats": "other",
            "adapter_version": "other",
            "selector_version": "other",
            "count": 251,
            "last_seen_at": (now - timedelta(minutes=20)).replace(tzinfo=UTC),
        }
        assert snapshot["failure_clusters"][1]["count"] == 126
        assert snapshot["failure_clusters"][1]["ats"] == "greenhouse"
        form = snapshot["form_resolution"][0]
        assert {
            form["ats"],
            form["adapter_version"],
            form["selector_version"],
            form["reason_code"],
            form["field_type"],
            form["resolver"],
        } == {"other", "OTHER"}
        assert form["count"] == 251
        assert len(snapshot["form_resolution"]) == 1
        assert len(snapshot["failure_clusters"]) == 2
        assert snapshot["evidence_types"] == []
        assert snapshot["automation_policy"]["active"] is False
        assert snapshot["automation_policy"]["reason_code"] == "AUTOMATION_POLICY_NOT_ACTIVE"
        assert snapshot["pipeline_counts"]["quarantined"] == 1
        assert snapshot["attempt_outcomes"][0]["outcome"] == "operator_confirmed"
        attempt = db.query(Submission).one()
        assert snapshot["recent_attempts"][0]["application_id"] == attempt.application_id
        attempt_response = _attempt_response(attempt)
        assert attempt_response.created_at.endswith("Z")
        assert attempt_response.started_at.endswith("Z")
        assert attempt_response.finished_at.endswith("Z")
        assert attempt_response.reconciled_at.endswith("Z")
        assert [row["queue"] for row in snapshot["queue_depth"]] == [
            "urls_pending",
            "jobs_extracted",
            "jobs_scored",
            "applications_draft",
            "applications_prepared",
            "submission_commands_pending",
            "submission_commands_claimed",
            "submissions_queued",
            "submissions_inspecting",
            "submissions_preparing",
            "submissions_ready",
            "submissions_committing",
            "submissions_verifying",
            "submissions_unknown",
        ]
    finally:
        db.close()
        engine.dispose()


def test_snapshot_excludes_orphan_evidence_from_unknown_attempt():
    engine, factory = _factory()
    now = datetime(2026, 7, 28, 12, 0, 0)
    db = factory()
    try:
        job = Job(
            title="Private title",
            company="Private company",
            source_url="https://private.example/job/orphan",
            apply_url="https://private.example/apply/orphan",
            status=JobStatus.NEEDS_REVIEW,
            created_at=now,
        )
        application = Application(job=job, status=JobStatus.NEEDS_REVIEW)
        db.add(application)
        db.flush()
        attempt = Submission(
            application_id=application.id,
            attempt_number=1,
            idempotency_key="orphan-evidence-attempt",
            submitter_name="greenhouse",
            status=SubmissionStatus.UNKNOWN,
            stage="finished",
            outcome="unknown",
            reason_code="FINAL_ACTION_UNCONFIRMED",
            created_at=now,
        )
        db.add(attempt)
        db.flush()
        db.add(
            SubmissionEvidence(
                attempt_id=attempt.id,
                evidence_type="employer_application_id",
                evidence_digest="a" * 64,
                employer_application_ref="unverified-reference",
                form_fingerprint="b" * 64,
                cv_hash="c" * 64,
                observed_at=now,
            )
        )
        db.commit()

        snapshot = build_operations_snapshot(
            db,
            _readiness(),
            now=now,
            window_days=30,
        )

        assert snapshot["evidence_types"] == []
    finally:
        db.close()
        engine.dispose()


def test_operations_endpoint_is_protected_bounded_and_contains_no_private_text(
    monkeypatch,
):
    engine, factory = _factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    _seed(factory, now)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(operations_route, "readiness_report", lambda _settings: _readiness())
    monkeypatch.setattr(operations_route, "get_settings", lambda: object())
    monkeypatch.setattr("api.main.settings.secret_key", "operations-test-secret")
    monkeypatch.setattr("api.main.settings.app_env", "development")
    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        assert client.get("/api/dashboard/operations").status_code == 401
        response = client.get(
            "/api/dashboard/operations?window_days=30",
            headers={"Authorization": "Bearer operations-test-secret"},
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["dependencies"]) == 8
        assert len(body["adapter_matrix"]) <= 100
        assert len(body["discovery_sources"]) <= 100
        assert len(body["role_cv_matrix"]) <= 100
        assert len(body["recent_fit_decisions"]) <= 25
        assert len(body["recent_attempts"]) <= 25
        assert len(body["failure_clusters"]) <= 100
        assert body["dependencies"][1]["reason_code"] == "MIGRATION_MISMATCH"
        assert body["dependencies"][3]["reason_code"] == "HEARTBEAT_MISSING"
        assert body["dependencies"][4]["reason_code"] == "HEARTBEAT_INVALID"
        assert body["generated_at"].endswith("Z")
        assert body["last_successful_discovery"]["finished_at"].endswith("Z")
        assert body["dependencies"][6]["last_seen_at"].endswith("Z")
        assert body["failure_clusters"][0]["last_seen_at"].endswith("Z")
        serialized = response.text
        for forbidden in (
            "candidate@example.com",
            "+972501234567",
            "private.example",
            "Private title",
            "Private company",
            "private answer",
            "passport number",
            "retired-version",
        ):
            assert forbidden not in serialized
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_snapshot_exposes_redacted_discovery_fit_cv_and_policy_operations(monkeypatch):
    engine, factory = _factory()
    now = datetime(2026, 7, 30, 12, 0, 0)
    db = factory()
    try:
        db.add(
            DiscoverySourceState(
                source_key="greenhouse:private-employer@example.test",
                source_type="greenhouse",
                descriptor_version="1.0.0",
                configuration_digest="1" * 64,
                transport="public_api",
                authentication_mode="none",
                host="private-employer.example.test",
                cadence_seconds=100_000,
                enabled=True,
                health_status="degraded",
                next_poll_at=now + timedelta(minutes=10),
                last_success_at=now - timedelta(minutes=5),
                last_error_code="private-employer@example.test",
            )
        )
        db.add(
            DiscoveryRun(
                source="greenhouse:private-employer@example.test",
                status="success",
                inserted=1,
                duplicates=3,
                started_at=now - timedelta(minutes=2),
                finished_at=now - timedelta(minutes=1),
            )
        )
        job = Job(
            title="Secret employer role",
            company="Secret employer",
            source_url="https://private-employer.example.test/jobs/123",
            status=JobStatus.DRAFT,
            created_at=now - timedelta(minutes=2),
        )
        db.add(job)
        db.flush()
        db.add(
            JobSourceOccurrenceRecord(
                occurrence_key="2" * 64,
                job_id=job.id,
                source_key="greenhouse:private-employer@example.test",
                external_posting_id="private-job-123",
                normalized_url="https://private-employer.example.test/jobs/123",
                normalized_url_hash="3" * 64,
                revision_digest="4" * 64,
                first_seen_at=now - timedelta(minutes=2),
                last_seen_at=now - timedelta(minutes=1),
                active=True,
            )
        )
        evidence = [
            {
                "factor": factor,
                "result": "matched",
                "points": points,
                "maximum_points": points,
                "reason_codes": ["MATCHED"],
            }
            for factor, points in (
                ("role", 25.0),
                ("skills", 25.0),
                ("location", 15.0),
                ("seniority", 10.0),
                ("employment", 5.0),
                ("experience", 10.0),
                ("language_authorization", 10.0),
            )
        ]
        fit = JobFitDecisionRecord(
            job_id=job.id,
            decision_digest="5" * 64,
            job_digest="6" * 64,
            profile_version=1,
            routing_config_digest="7" * 64,
            cv_manifest_digest="8" * 64,
            selected_cv_id="ai_engineer",
            selected_cv_hash="9" * 64,
            routing_confidence=0.97,
            routing_margin=0.22,
            routing_fallback_reason=None,
            fit_score=96.0,
            disposition="eligible",
            quality_eligible=True,
            hard_exclusions_json="[]",
            uncertainty_json="[]",
            unsupported_skills_json="[]",
            evidence_json=json.dumps(evidence),
            thresholds_json=json.dumps(
                {
                    "minimum_fit_score": 85.0,
                    "minimum_routing_confidence": 0.55,
                    "minimum_routing_margin": 0.08,
                }
            ),
            policy_version="job-fit-policy.v1",
            model_identity="deterministic:job-fit-v1",
            qualification_digest="a" * 64,
            created_at=now,
        )
        db.add(fit)
        application = Application(
            job=job,
            status=JobStatus.DRAFT,
            approved_at=now,
            revision=1,
            prepared_revision=1,
            selected_cv_id="ai_engineer",
            selected_cv_hash="9" * 64,
            material_eligible=False,
        )
        db.add(application)
        db.commit()

        monkeypatch.setattr(
            dashboard_operations,
            "policy_usage_status",
            lambda _db, **_kwargs: {
                "active": True,
                "revision": 4,
                "activated_at": now.isoformat(),
                "expires_at": (now + timedelta(days=30)).isoformat(),
                "minimum_fit_score": 85.0,
                "daily_limit": 25,
                "daily_remaining": 21,
                "hourly_limit": 5,
                "hourly_remaining": 4,
                "company_limit": 2,
                "company_window_days": 14,
                "permitted_adapters": ["greenhouse"],
                "geographies": ["israel", "worldwide_remote"],
                "role_families": ["private role name must not appear"],
                "qualified_form_contract_count": 2,
                "kill_switch_active": False,
                "kill_switch_revision": 3,
            },
        )

        snapshot = build_operations_snapshot(db, _readiness(), now=now, window_days=30)

        assert snapshot["discovery_sources"] == [
            {
                "source_type": "greenhouse",
                "status": "degraded",
                "source_count": 1,
                "enabled_count": 1,
                "cadence_seconds": 86_400,
                "next_poll_at": (now + timedelta(minutes=10)).replace(tzinfo=UTC),
                "last_success_at": (now - timedelta(minutes=5)).replace(tzinfo=UTC),
                "last_error_code": "OTHER",
            }
        ]
        assert snapshot["pipeline_counts"] == {
            "discovered": 1,
            "source_occurrences": 1,
            "deduplicated": 3,
            "eligible": 1,
            "prepared": 1,
            "quarantined": 0,
            "employer_confirmed": 0,
        }
        assert snapshot["role_cv_matrix"] == [
            {
                "cv_route": "ai_engineer",
                "total": 1,
                "eligible": 1,
                "needs_review": 0,
                "excluded": 0,
                "average_fit_score": 96.0,
                "average_routing_confidence": 0.97,
            }
        ]
        recent_fit = snapshot["recent_fit_decisions"][0]
        assert recent_fit["cv_route"] == "ai_engineer"
        assert recent_fit["quality_eligible"] is True
        assert len(recent_fit["evidence"]) == 7
        assert snapshot["automation_policy"]["daily_remaining"] == 21
        assert snapshot["automation_policy"]["role_family_count"] == 1
        serialized = json.dumps(snapshot, default=str)
        for forbidden in (
            "private-employer",
            "Secret employer",
            "private-job-123",
            "private role name",
        ):
            assert forbidden not in serialized
    finally:
        db.close()
        engine.dispose()


def test_terminal_window_uses_finished_time_and_reconciliation_is_a_failure():
    engine, factory = _factory()
    now = datetime(2026, 7, 28, 12, 0, 0)
    db = factory()
    try:
        for index, (created_at, finished_at) in enumerate(
            (
                (now - timedelta(days=120), now - timedelta(days=1)),
                (now - timedelta(days=1), now - timedelta(days=31)),
            ),
            start=1,
        ):
            job = Job(
                title=f"Window test {index}",
                source_url=f"https://example.test/window/{index}",
                status=JobStatus.DRAFT,
            )
            application = Application(job=job, status=JobStatus.DRAFT)
            db.add(application)
            db.flush()
            db.add(
                Submission(
                    application_id=application.id,
                    attempt_number=1,
                    idempotency_key=f"window-attempt-{index}",
                    submitter_name="greenhouse",
                    status=SubmissionStatus.DRAFT_ONLY,
                    stage="finished",
                    outcome="draft_only",
                    reason_code="DRAFT_ONLY",
                    created_at=created_at,
                    finished_at=finished_at,
                )
            )

        event_key = hashlib.sha256(b"reconciled-not-submitted").hexdigest()
        db.add(
            OperationalMetricReceipt(
                event_key=event_key,
                recorded_at=now,
            )
        )
        db.add(
            OperationalMetricEvent(
                event_key=event_key,
                entity_key=hashlib.sha256(b"reconciled-entity").hexdigest(),
                metric_name="attempt_outcome",
                ats="greenhouse",
                adapter_version="1.0.0",
                selector_version="greenhouse-candidate-v9",
                stage="finished",
                outcome="failed_before_commit",
                reason_code="RECONCILED_NOT_SUBMITTED",
                field_type="none",
                resolver="none",
                attachment_result="not_applicable",
                evidence_type="operator_confirmed",
                occurred_at=now - timedelta(minutes=1),
            )
        )
        db.commit()

        snapshot = build_operations_snapshot(
            db,
            _readiness(),
            now=now,
            window_days=30,
        )
        assert sum(row["count"] for row in snapshot["attempt_outcomes"]) == 1
        assert snapshot["attempt_outcomes"][0]["outcome"] == "draft_only"
        assert any(
            row["reason_code"] == "RECONCILED_NOT_SUBMITTED" for row in snapshot["failure_clusters"]
        )
    finally:
        db.close()
        engine.dispose()
