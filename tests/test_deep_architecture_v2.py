import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from api.main import app
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from jobs.tracker import generate_executive_digest
from submitters.inspector import inspect_submitter_health


@pytest.fixture
def client():
    return TestClient(app)


def test_submitter_health_inspector():
    report = inspect_submitter_health()
    assert isinstance(report.playwright_installed, bool)
    assert isinstance(report.live_auto_apply_active, bool)
    assert len(report.registered_platforms) > 0


def test_executive_digest():
    db_session = get_session_factory()()
    try:
        digest = generate_executive_digest(db_session)
        assert digest.total_jobs_scanned >= 0
        assert digest.total_applications >= 0
        assert "Executive Job Apply Digest" in digest.summary_text
    finally:
        db_session.close()


def test_digest_endpoint(client):
    resp = client.get("/api/notifications/digest")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_jobs_scanned" in data
    assert "summary_text" in data


def test_health_inspector_endpoint(client):
    resp = client.get("/api/submitters/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "registered_platforms" in data
    assert "playwright_installed" in data


def test_interview_simulate_endpoint(client):
    db_session = get_session_factory()()
    try:
        job = Job(
            title="Senior Python AI Engineer",
            company="InnovateAI",
            location="Haifa",
            description="Developing production RAG pipelines",
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

        with patch("api.routes.interview_simulate.evaluate_interview_answer", new_callable=AsyncMock) as mock_eval:
            mock_eval.return_value.score = 88
            mock_eval.return_value.strengths = ["Strong RAG framing"]
            mock_eval.return_value.missing_points = ["Mention FAISS benchmark numbers"]
            mock_eval.return_value.improved_answer = "Reframed answer with metrics"

            resp = client.post(
                f"/api/applications/{app_rec.id}/interview-simulate",
                json={"question": "Tell me about your RAG experience", "candidate_answer": "I built a RAG agent in Python."},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["application_id"] == app_rec.id
            assert data["score"] == 88
    finally:
        db_session.close()
