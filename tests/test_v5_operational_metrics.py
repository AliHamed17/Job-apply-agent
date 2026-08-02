"""Authoritative v5 metric coverage, cardinality, and privacy tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from prometheus_client import CollectorRegistry, generate_latest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.v5_operational_metrics import (
    V5OperationalCollector,
    register_v5_operational_collector,
)
from db.models import (
    Application,
    ApplicationPolicyDecision,
    AutomationPolicyRevisionRecord,
    Base,
    DiscoveryRun,
    DiscoverySourceState,
    FormPlan,
    Job,
    JobFitDecisionRecord,
    JobStatus,
    OperationalMetricRollup,
    Submission,
    SubmissionEvidence,
    SubmissionStatus,
)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'v5-metrics.db'}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _fit(job_id: int, *, selected_cv_id: str) -> JobFitDecisionRecord:
    return JobFitDecisionRecord(
        job_id=job_id,
        decision_digest="1" * 64,
        job_digest="2" * 64,
        profile_version=1,
        routing_config_digest="3" * 64,
        cv_manifest_digest="4" * 64,
        selected_cv_id=selected_cv_id,
        selected_cv_hash="5" * 64,
        routing_confidence=0.97,
        routing_margin=0.22,
        fit_score=96,
        disposition="eligible",
        quality_eligible=True,
        hard_exclusions_json="[]",
        uncertainty_json="[]",
        unsupported_skills_json="[]",
        evidence_json="[]",
        thresholds_json="{}",
        policy_version="job-fit-policy.v1",
        model_identity="deterministic:job-fit-v1",
        qualification_digest="6" * 64,
    )


def _qualification(path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "fit-qualification.v1",
                "algorithm_version": "job-fit.v1",
                "routing_config_digest": "a" * 64,
                "cv_manifest_digest": "b" * 64,
                "dataset_digest": "c" * 64,
                "thresholds": {},
                "labeled_cases": 240,
                "holdout_cases": 48,
                "holdout_precision": 0.96,
                "holdout_coverage": 0.5,
                "qualified": True,
                "created_at": "2026-08-01T00:00:00Z",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _seed(factory) -> tuple[str, ...]:
    now = datetime.now(UTC).replace(tzinfo=None)
    private_source = "greenhouse:private-employer@example.test"
    forbidden = (
        "private-employer@example.test",
        "candidate@example.test",
        "+972501234567",
        "https://private.example",
        "Private employer",
        "Private AI role",
        "private-cv-name",
    )
    db = factory()
    try:
        db.add_all(
            [
                DiscoverySourceState(
                    source_key=private_source,
                    source_type="greenhouse",
                    descriptor_version="discovery-source.v1",
                    configuration_digest="7" * 64,
                    transport="public_api",
                    authentication_mode="none",
                    host="private.example",
                    cadence_seconds=600,
                    enabled=True,
                    health_status="degraded",
                    last_success_at=now - timedelta(minutes=15),
                    last_error_code="candidate@example.test",
                ),
                DiscoverySourceState(
                    source_key="hostile-private-source",
                    source_type="candidate@example.test",
                    descriptor_version="discovery-source.v1",
                    configuration_digest="8" * 64,
                    transport="public_api",
                    authentication_mode="none",
                    host="private.example",
                    cadence_seconds=600,
                    enabled=True,
                    health_status="unknown",
                ),
                DiscoveryRun(
                    source=private_source[:64],
                    status="failed",
                    inserted=2,
                    updated=3,
                    duplicates=4,
                    closed=1,
                    reason_code="candidate@example.test",
                    started_at=now - timedelta(minutes=10),
                    finished_at=now - timedelta(minutes=9),
                ),
            ]
        )
        job = Job(
            title="Private AI role",
            company="Private employer",
            source_url="https://private.example/jobs/one",
            apply_url="https://private.example/jobs/one/apply",
            status=JobStatus.NEEDS_REVIEW,
        )
        db.add(job)
        db.flush()
        fit = _fit(job.id, selected_cv_id="private-cv-name")
        db.add(fit)
        db.flush()
        application = Application(
            job=job,
            status=JobStatus.NEEDS_REVIEW,
            selected_cv_id="private-cv-name",
            selected_cv_hash="5" * 64,
            profile_version=1,
            job_fit_decision_id=fit.id,
        )
        db.add(application)
        db.flush()
        plan = FormPlan(
            application_id=application.id,
            application_revision=1,
            adapter_name="greenhouse",
            adapter_version="1.0.0",
            selector_version="greenhouse-candidate-v9",
            fingerprint="9" * 64,
            selected_cv_id="private-cv-name",
            selected_cv_hash="5" * 64,
            attached_cv_id="private-cv-name",
            attached_cv_hash="5" * 64,
            attachment_verified=True,
            attachment_verification_source="browser_upload_receipt",
            attachment_verified_at=now,
            profile_version=1,
            session_verified_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        db.add(plan)
        db.flush()
        attempt = Submission(
            application_id=application.id,
            attempt_number=1,
            idempotency_key="verified-metric-attempt",
            submitter_name="greenhouse",
            status=SubmissionStatus.SUCCESS,
            stage="finished",
            outcome="confirmed_submitted",
            application_revision=1,
            adapter_name="greenhouse",
            adapter_version="1.0.0",
            selector_version="greenhouse-candidate-v9",
            form_plan_id=plan.id,
            submitted_at=now,
            final_action_at=now,
            reason_code="EMPLOYER_VERIFIED",
            selected_cv_id="private-cv-name",
            requested_cv_id="private-cv-name",
            requested_cv_hash="5" * 64,
            attached_cv_id="private-cv-name",
            attached_cv_hash="5" * 64,
            attachment_verified=True,
            form_plan_fingerprint="9" * 64,
            profile_version=1,
            verification_kind="employer_application_id",
            evidence_digest="e" * 64,
            runner_release="test-release",
        )
        attempt.evidence.append(
            SubmissionEvidence(
                evidence_type="employer_application_id",
                evidence_digest="e" * 64,
                employer_application_ref="private-provider-reference",
                form_fingerprint="9" * 64,
                cv_hash="5" * 64,
                observed_at=now,
            )
        )
        db.add(attempt)

        unknown_job = Job(
            title="Private unknown role",
            source_url="https://private.example/jobs/two",
            status=JobStatus.NEEDS_REVIEW,
        )
        unknown_application = Application(job=unknown_job, status=JobStatus.NEEDS_REVIEW)
        db.add(unknown_application)
        db.flush()
        db.add(
            Submission(
                application_id=unknown_application.id,
                attempt_number=1,
                idempotency_key="unknown-metric-attempt",
                submitter_name="candidate@example.test",
                adapter_name="candidate@example.test",
                status=SubmissionStatus.UNKNOWN,
                stage="finished",
                outcome="unknown",
                reason_code="FINAL_ACTION_UNCONFIRMED",
            )
        )

        policy = AutomationPolicyRevisionRecord(
            policy_id=str(uuid4()),
            revision=1,
            schema_version="auto-submit-policy.v1",
            payload_json="{}",
            payload_digest="a" * 64,
            signing_key_id=str(uuid4()),
            signature="s" * 86,
            active_slot=1,
            activated_at=now - timedelta(minutes=5),
            expires_at=now + timedelta(days=30),
        )
        db.add(policy)
        db.flush()
        db.add(
            ApplicationPolicyDecision(
                policy_revision_id=policy.id,
                application_id=application.id,
                application_revision=1,
                fit_decision_id=fit.id,
                form_plan_id=plan.id,
                decision_digest="b" * 64,
                policy_digest="a" * 64,
                job_digest="2" * 64,
                company_digest="c" * 64,
                fit_decision_digest="1" * 64,
                form_plan_public_id=plan.plan_id,
                form_fingerprint="9" * 64,
                form_contract_digest="d" * 64,
                selected_cv_hash="5" * 64,
                profile_version=1,
                confirmed_answer_revision="f" * 64,
                adapter_name="greenhouse",
                adapter_version="1.0.0",
                selector_version="greenhouse-candidate-v9",
                fit_score=96,
                allowed=False,
                reason_codes_json=json.dumps(
                    ["candidate@example.test", "AUTOMATION_DAILY_LIMIT_REACHED"],
                    separators=(",", ":"),
                ),
                authority_expires_at=None,
                evaluated_at=now,
            )
        )
        db.add(
            OperationalMetricRollup(
                metric_name="attempt_stage",
                ats="+972501234567",
                adapter_version="private-version",
                selector_version="private-selector",
                stage="preparing",
                outcome="none",
                reason_code="NONE",
                field_type="none",
                resolver="none",
                attachment_result="none",
                evidence_type="none",
                event_count=1,
                duration_count=1,
                duration_sum_ms=2_000,
                duration_le_1s=0,
                duration_le_5s=1,
                duration_le_15s=1,
                duration_le_60s=1,
                duration_le_300s=1,
                duration_le_900s=1,
                duration_le_inf=1,
            )
        )
        db.commit()
        return forbidden
    finally:
        db.close()


def test_v5_collector_reports_exact_bounded_metrics_without_private_data(
    tmp_path,
    monkeypatch,
):
    engine, factory = _factory(tmp_path)
    forbidden = _seed(factory)
    qualification_path = tmp_path / "fit-qualification.json"
    _qualification(qualification_path)
    monkeypatch.setenv("FIT_ROUTING_QUALIFICATION_PATH", str(qualification_path))
    monkeypatch.setattr("db.session.get_session_factory", lambda: factory)
    registry = CollectorRegistry()
    registry.register(V5OperationalCollector())
    exposition = generate_latest(registry).decode()
    try:
        assert "job_agent_v5_operational_snapshot_available 1.0" in exposition
        assert (
            'job_agent_discovery_failures_total{reason_code="OTHER",source="greenhouse"} 1.0'
            in exposition
        )
        assert (
            'job_agent_discovery_postings_total{result="duplicate",source="greenhouse"} 4.0'
            in exposition
        )
        assert (
            'job_agent_fit_current_jobs{auto_eligible="true",disposition="eligible"} 1.0'
            in exposition
        )
        assert 'job_agent_fit_qualification_ratio{metric="precision"} 0.96' in exposition
        assert 'job_agent_fit_qualification_ratio{metric="abstention"} 0.5' in exposition
        assert (
            "job_agent_automation_policy_denials_total"
            '{reason_code="AUTOMATION_DAILY_LIMIT_REACHED"} 1.0' in exposition
        )
        assert 'job_agent_automation_policy_denials_total{reason_code="OTHER"} 1.0' in exposition
        assert (
            'job_agent_submission_attempts_total{ats="other",outcome="unknown"} 1.0' in exposition
        )
        assert 'job_agent_employer_confirmed_applications_total{ats="greenhouse"} 1.0' in exposition
        assert 'job_agent_preparation_duration_seconds_count{ats="other"} 1.0' in exposition
        for value in forbidden:
            assert value not in exposition
    finally:
        engine.dispose()


def test_v5_collector_fails_closed_when_database_and_qualification_are_unavailable(
    monkeypatch,
):
    def unavailable():
        raise ConnectionError("candidate@example.test https://private.example")

    monkeypatch.delenv("FIT_ROUTING_QUALIFICATION_PATH", raising=False)
    monkeypatch.setattr("db.session.get_session_factory", unavailable)
    registry = CollectorRegistry()
    registry.register(V5OperationalCollector())
    exposition = generate_latest(registry).decode()
    assert "job_agent_v5_operational_snapshot_available 0.0" in exposition
    assert "job_agent_fit_qualification_available 0.0" in exposition
    assert "candidate@example.test" not in exposition
    assert "private.example" not in exposition


def test_v5_collector_registration_is_idempotent():
    registry = CollectorRegistry(auto_describe=True)
    first = register_v5_operational_collector(registry)
    second = register_v5_operational_collector(registry)
    assert first is second
    exposition = generate_latest(registry).decode()
    assert exposition.count("# HELP job_agent_v5_operational_snapshot_available") == 1
