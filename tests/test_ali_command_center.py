import pytest

from fastapi.testclient import TestClient

from api.main import app
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from notifications.dispatcher import dispatch_high_match_alert
from profile.loader import get_profile
from profile.spotlight_matcher import match_portfolio_spotlight
from jobs.models import JobData


@pytest.fixture
def client():
    return TestClient(app)


def test_portfolio_spotlight_matcher():
    profile = get_profile()

    job_ai = JobData(title="AI Engineer", company="AI Corp", description="Python, PyTorch, RAG, LLMs")
    match_ai = match_portfolio_spotlight(job_ai, profile)
    assert "AI Agent" in match_ai.spotlight_title

    job_devops = JobData(title="DevOps Engineer", company="CloudCorp", description="Docker, Kubernetes, AWS, CI/CD")
    match_devops = match_portfolio_spotlight(job_devops, profile)
    assert "75% Build-to-Deploy" in match_devops.spotlight_title


def test_alert_dispatcher():
    res = dispatch_high_match_alert("Senior AI Engineer", "TechCorp", 92.5, "auto_applied")
    assert res.dispatched is True
    assert res.recipient_email == "ali.h.10j@gmail.com"
    assert "+972-53-339-2826" in res.recipient_phone


def test_command_center_endpoint(client):
    resp = client.get("/api/command-center/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["candidate_name"] == "Ali Hamed"
    assert data["auto_apply_active"] is True
    assert "total_jobs_scanned" in data


def test_spotlight_endpoint(client):
    db_session = get_session_factory()()
    try:
        job = Job(
            title="Senior MLOps Engineer",
            company="DeepAI",
            location="Tel Aviv",
            description="PyTorch, Docker, Kubernetes, RAG, Python",
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

        resp = client.get(f"/api/applications/{app_rec.id}/portfolio-spotlight")
        assert resp.status_code == 200
        data = resp.json()
        assert data["application_id"] == app_rec.id
        assert len(data["showcase_text"]) > 0
    finally:
        db_session.close()


def test_dispatch_endpoint(client):
    db_session = get_session_factory()()
    try:
        job = Job(
            title="Software Architect",
            company="InnovateTech",
            location="Haifa",
            description="Python, C++, Docker",
            apply_url="https://example.com/apply",
            source_url="https://example.com/job",
            status=JobStatus.DRAFT,
            score=91.0,
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        app_rec = Application(
            job_id=job.id,
            cover_letter="Cover letter",
            recruiter_message="Message",
            status=JobStatus.DRAFT,
            selected_cv_id="software-engineer",
        )
        db_session.add(app_rec)
        db_session.commit()
        db_session.refresh(app_rec)

        resp = client.post("/api/notifications/dispatch", json={"application_id": app_rec.id, "event_type": "auto_applied"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["dispatched"] is True
        assert "ali.h.10j@gmail.com" in data["recipient_email"]
    finally:
        db_session.close()
