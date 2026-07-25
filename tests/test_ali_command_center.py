from profile.models import UserProfile
from profile.spotlight_matcher import match_portfolio_spotlight

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import command_center as command_center_route
from core.config import get_settings
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from jobs.models import JobData
from notifications.dispatcher import dispatch_high_match_alert


@pytest.fixture
def client(auth_headers):
    return TestClient(app, headers=auth_headers)


def test_portfolio_spotlight_matcher():
    profile = UserProfile()
    profile.resume.text = "Python PyTorch RAG LLM Docker Kubernetes AWS CI/CD"

    job_ai = JobData(
        title="AI Engineer", company="AI Corp", description="Python, PyTorch, RAG, LLMs"
    )
    match_ai = match_portfolio_spotlight(job_ai, profile)
    assert set(match_ai.relevant_keywords) >= {"PyTorch", "RAG", "LLM", "Python"}
    assert "No additional achievement" in match_ai.showcase_text

    job_devops = JobData(
        title="DevOps Engineer", company="CloudCorp", description="Docker, Kubernetes, AWS, CI/CD"
    )
    match_devops = match_portfolio_spotlight(job_devops, profile)
    assert set(match_devops.relevant_keywords) >= {"Docker", "Kubernetes", "AWS", "CI/CD"}
    assert "75%" not in match_devops.showcase_text


def test_alert_dispatcher(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_RECIPIENT_EMAIL", "operator@example.com")
    monkeypatch.setenv("NOTIFICATION_RECIPIENT_PHONE", "+10000000000")
    get_settings.cache_clear()
    res = dispatch_high_match_alert("Senior AI Engineer", "TechCorp", 92.5, "auto_applied")
    assert res.dispatched is False
    assert res.recipient_email == "operator@example.com"
    assert res.recipient_phone == "+10000000000"
    get_settings.cache_clear()


def test_command_center_endpoint(client, monkeypatch):
    profile = UserProfile()
    profile.personal.name = "Test Candidate"
    monkeypatch.setattr(command_center_route, "get_profile", lambda: profile)
    resp = client.get("/api/command-center/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["candidate_name"] == "Test Candidate"
    assert data["auto_apply_active"] is get_settings().auto_apply
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


def test_dispatch_endpoint(client, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_RECIPIENT_EMAIL", "operator@example.com")
    monkeypatch.setenv("NOTIFICATION_RECIPIENT_PHONE", "+10000000000")
    get_settings.cache_clear()
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

        resp = client.post(
            "/api/notifications/dispatch",
            json={"application_id": app_rec.id, "event_type": "auto_applied"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["dispatched"] is False
        assert data["recipient_email"] == "operator@example.com"
    finally:
        db_session.close()
        get_settings.cache_clear()
