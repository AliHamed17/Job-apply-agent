import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from api.main import app
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from jobs.ghost_detector import detect_ghost_posting
from jobs.models import JobData


@pytest.fixture
def client():
    return TestClient(app)


def test_ghost_posting_detector():
    job_normal = JobData(
        title="Senior Software Engineer",
        company="Parallel Wireless",
        description="Full stack software engineer with Python, C++, Docker, and CI/CD experience." * 5,
    )
    res_normal = detect_ghost_posting(job_normal)
    assert res_normal.is_ghost_suspect is False

    job_ghost = JobData(
        title="Software Developer - Reposted",
        company="Staffing Agency",
        description="Confidential client hiring. Short description.",
    )
    res_ghost = detect_ghost_posting(job_ghost)
    assert res_ghost.is_ghost_suspect is True
    assert len(res_ghost.reasons) > 0


def test_salary_brief_endpoint(client, auth_headers):
    db_session = get_session_factory()()
    try:
        job = Job(
            title="Senior AI Engineer",
            company="TechCorp",
            location="Tel Aviv",
            description="Building production AI agents and RAG pipelines",
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

        with patch("api.routes.salary.generate_salary_brief", new_callable=AsyncMock) as mock_brief:
            mock_brief.return_value.currency = "ILS"
            mock_brief.return_value.estimated_percentiles = {"p25": 30000, "p50": 38000, "p75": 45000, "p90": 52000}
            mock_brief.return_value.negotiation_talking_points = ["75% speedup metric"]
            mock_brief.return_value.counter_offer_script = "Thank you for the offer."

            resp = client.get(f"/api/applications/{app_rec.id}/salary-brief", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["application_id"] == app_rec.id
            assert data["currency"] == "ILS"
            assert data["estimated_percentiles"]["p50"] == 38000
    finally:
        db_session.close()


def test_batch_rescore_endpoint(client, auth_headers):
    resp = client.post("/api/jobs/rescore", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "total_evaluated" in data
    assert "updated_count" in data
