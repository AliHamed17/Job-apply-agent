"""Privacy and truth contracts for the local runner's cloud heartbeat summary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import control_plane_status
from core.control_plane_status import build_control_plane_status
from db.models import (
    Application,
    Base,
    DiscoveryRun,
    DiscoverySourceState,
    Job,
    JobFitDecisionRecord,
    JobSourceOccurrenceRecord,
    JobStatus,
)
from submitters.platforms import adapter_for_platform


def test_control_plane_status_is_finite_redacted_and_truthful(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    db = factory()
    try:
        db.add_all(
            [
                DiscoverySourceState(
                    source_key="greenhouse:private-employer@example.test",
                    source_type="greenhouse",
                    descriptor_version="1.0.0",
                    configuration_digest="1" * 64,
                    transport="public_api",
                    authentication_mode="none",
                    host="private-employer.example.test",
                    cadence_seconds=600,
                    enabled=True,
                    health_status="healthy",
                ),
                DiscoverySourceState(
                    source_key="private-source-with-candidate-name",
                    source_type="private_email_candidate_name",
                    descriptor_version="1.0.0",
                    configuration_digest="2" * 64,
                    transport="public_api",
                    authentication_mode="none",
                    host="private.example.test",
                    cadence_seconds=600,
                    enabled=True,
                    health_status="healthy",
                ),
                DiscoveryRun(
                    source="greenhouse:private-employer@example.test",
                    status="success",
                    inserted=1,
                    duplicates=3,
                    started_at=now.replace(tzinfo=None),
                    finished_at=now.replace(tzinfo=None),
                ),
            ]
        )
        prepared_job = Job(
            title="Private AI role",
            company="Private employer",
            source_url="https://private.example.test/jobs/1",
            status=JobStatus.DRAFT,
        )
        quarantined_job = Job(
            title="Private review role",
            company="Another private employer",
            source_url="https://private.example.test/jobs/2",
            status=JobStatus.NEEDS_REVIEW,
        )
        db.add_all([prepared_job, quarantined_job])
        db.flush()
        db.add(
            JobSourceOccurrenceRecord(
                occurrence_key="3" * 64,
                job_id=prepared_job.id,
                source_key="greenhouse:private-employer@example.test",
                external_posting_id="private-posting-id",
                normalized_url="https://private.example.test/jobs/1",
                normalized_url_hash="4" * 64,
                revision_digest="5" * 64,
                first_seen_at=now.replace(tzinfo=None),
                last_seen_at=now.replace(tzinfo=None),
                active=True,
            )
        )
        fit = JobFitDecisionRecord(
            job_id=prepared_job.id,
            decision_digest="6" * 64,
            job_digest="7" * 64,
            profile_version=1,
            routing_config_digest="8" * 64,
            cv_manifest_digest="9" * 64,
            selected_cv_id="private_candidate_ai_cv",
            selected_cv_hash="a" * 64,
            routing_confidence=0.97,
            routing_margin=0.2,
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
            qualification_digest="b" * 64,
        )
        db.add(fit)
        db.add_all(
            [
                Application(
                    job=prepared_job,
                    status=JobStatus.DRAFT,
                    approved_at=now.replace(tzinfo=None),
                    revision=1,
                    prepared_revision=1,
                    material_eligible=False,
                ),
                Application(
                    job=quarantined_job,
                    status=JobStatus.NEEDS_REVIEW,
                    needs_review_reason="candidate private answer unavailable",
                ),
            ]
        )
        db.commit()

        monkeypatch.setattr(
            control_plane_status,
            "policy_usage_status",
            lambda _db, **_kwargs: {
                "active": True,
                "revision": 2,
                "expires_at": (now + timedelta(days=7)).isoformat(),
                "daily_remaining": 23,
                "hourly_remaining": 4,
                "kill_switch_active": False,
                "role_families": ["private candidate preference"],
            },
        )
        descriptor = adapter_for_platform("greenhouse")
        assert descriptor is not None
        monkeypatch.setattr(
            control_plane_status,
            "effective_registered_descriptors",
            lambda _db: (descriptor,),
        )

        status = build_control_plane_status(db, now=now)

        assert status["pipeline"] == {
            "discovered": 2,
            "source_occurrences": 1,
            "deduplicated": 3,
            "eligible": 1,
            "prepared": 1,
            "quarantined": 1,
            "employer_confirmed": 0,
        }
        assert status["policy"] == {
            "state": "active",
            "revision": 2,
            "expires_at": now + timedelta(days=7),
            "daily_remaining": 23,
            "hourly_remaining": 4,
            "kill_switch_active": False,
        }
        assert status["sources"] == [
            {
                "source": "greenhouse",
                "status": "healthy",
                "enabled_count": 1,
                "source_count": 1,
            }
        ]
        assert status["adapters"] == [
            {
                "adapter": "greenhouse",
                "qualification_tier": "fixture_qualified",
                "final_execution_enabled": False,
                "qualified_form_scope_count": 0,
            }
        ]
        serialized = json.dumps(status, default=str)
        for forbidden in (
            "private",
            "candidate",
            "example.test",
            "posting-id",
            "cv_hash",
            "source_key",
            "host",
        ):
            assert forbidden not in serialized.lower()
    finally:
        db.close()
        engine.dispose()


def test_control_plane_policy_status_fails_closed_without_error_text(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        monkeypatch.setattr(
            control_plane_status,
            "policy_usage_status",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("candidate@example.com private policy failure")
            ),
        )
        monkeypatch.setattr(
            control_plane_status,
            "effective_registered_descriptors",
            lambda _db: (),
        )

        status = build_control_plane_status(db)

        assert status["policy"] == {
            "state": "blocked",
            "revision": 0,
            "expires_at": None,
            "daily_remaining": 0,
            "hourly_remaining": 0,
            "kill_switch_active": False,
        }
        assert "candidate@example.com" not in json.dumps(status)
    finally:
        db.close()
        engine.dispose()
