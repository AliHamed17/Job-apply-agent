import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.main import app
from core.audit import record_audit_event
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from match.analytics import compute_match_analytics


@pytest.fixture
def client():
    return TestClient(app)


def test_compute_match_analytics():
    db_session = get_session_factory()()
    try:
        summary = compute_match_analytics(db_session)
        assert summary.total_jobs >= 0
        assert isinstance(summary.average_score, float)
        assert len(summary.top_matched_skills) > 0
    finally:
        db_session.close()


def test_audit_event_buffer():
    rec = record_audit_event("test_event", level="info", details={"foo": "bar"})
    assert rec["event_name"] == "test_event"
    assert rec["level"] == "info"


def test_analytics_endpoint(client):
    resp = client.get("/api/analytics/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_jobs" in data
    assert "top_matched_skills" in data


def test_audit_endpoint(client):
    resp = client.get("/api/audit/logs")
    assert resp.status_code == 200
    data = resp.json()
    assert "total" in data
    assert isinstance(data["logs"], list)


def test_followup_plan_endpoint(client):
    db_session = get_session_factory()()
    try:
        job = Job(
            title="Principal AI Architect",
            company="NeuralLabs",
            location="Remote",
            description="Leading production LLM systems",
            apply_url="https://example.com/apply",
            source_url="https://example.com/job",
            status=JobStatus.DRAFT,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        app_rec = Application(
            job_id=job.id,
            cover_letter="Cover letter",
            recruiter_message="Message",
            status=JobStatus.DRAFT,
            selected_cv_id="ai-engineer",
        )
        db_session.add(app_rec)
        db_session.commit()
        db_session.refresh(app_rec)

        with patch("api.routes.followup.generate_followup_plan", new_callable=AsyncMock) as mock_plan:
            mock_plan.return_value.stage1_day3_checkin = "Stage 1 message"
            mock_plan.return_value.stage2_day7_value_add = "Stage 2 message"
            mock_plan.return_value.stage3_day14_inquiry = "Stage 3 message"

            resp = client.get(f"/api/applications/{app_rec.id}/followup-plan")
            assert resp.status_code == 200
            data = resp.json()
            assert data["application_id"] == app_rec.id
            assert "Stage 1" in data["stage1_day3_checkin"]
    finally:
        db_session.close()
