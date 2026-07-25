import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from api.main import app
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from match.ab_testing import compute_ab_test_analytics


@pytest.fixture
def client(auth_headers):
    return TestClient(app, headers=auth_headers)


def test_ab_testing_analytics():
    db_session = get_session_factory()()
    try:
        report = compute_ab_test_analytics(db_session)
        assert report.total_analyzed >= 0
        assert isinstance(report.variants, list)
    finally:
        db_session.close()


def test_ab_testing_endpoint(client):
    resp = client.get("/api/analytics/ab-testing")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_analyzed" in data
    assert "variants" in data


def test_culture_fit_endpoint(client):
    db_session = get_session_factory()()
    try:
        job = Job(
            title="Senior MLOps Engineer",
            company="DeepTech AI",
            location="Tel Aviv",
            description="High ownership culture, remote friendly, cutting-edge AI stack",
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

        with patch("api.routes.culture_fit.evaluate_culture_fit", new_callable=AsyncMock) as mock_eval:
            mock_eval.return_value.culture_fit_score = 92
            mock_eval.return_value.cultural_highlights = ["Autonomous engineering team"]
            mock_eval.return_value.behavioral_talking_points = ["Proactive ownership"]
            mock_eval.return_value.caution_flags = []

            resp = client.get(f"/api/applications/{app_rec.id}/culture-fit")
            assert resp.status_code == 200
            data = resp.json()
            assert data["application_id"] == app_rec.id
            assert data["culture_fit_score"] == 92
    finally:
        db_session.close()


def test_stream_endpoint(client):
    db_session = get_session_factory()()
    try:
        job = Job(
            title="DevOps Engineer",
            company="CloudCo",
            location="Haifa",
            description="AWS Docker Kubernetes",
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
            selected_cv_id="devops",
        )
        db_session.add(app_rec)
        db_session.commit()
        db_session.refresh(app_rec)

        resp = client.get(f"/api/applications/{app_rec.id}/stream")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
    finally:
        db_session.close()
