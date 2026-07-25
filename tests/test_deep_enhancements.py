import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from api.main import app
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from profile.skill_gaps import analyze_skill_gaps


@pytest.fixture
def client():
    return TestClient(app)


def test_analyze_skill_gaps():
    analysis = analyze_skill_gaps(
        job_description="Looking for a Python AI Engineer with PyTorch, Docker, and Kubernetes",
        job_requirements="Experience with RAG, FAISS, and LangChain",
        cv_text="Software Engineer with Python, Docker, PyTorch, C++, and Linux experience",
    )
    assert "python" in analysis.matched_skills
    assert "docker" in analysis.matched_skills
    assert len(analysis.missing_skills) > 0
    assert len(analysis.recommendations) > 0


def test_outreach_endpoint(client, auth_headers):
    db_session = get_session_factory()()
    try:
        job = Job(
            title="Senior DevOps Engineer",
            company="CloudScale",
            location="Tel Aviv",
            description="Docker, Kubernetes, AWS, Terraform, Jenkins",
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

        with patch("api.routes.outreach.generate_outreach", new_callable=AsyncMock) as mock_outreach:
            mock_outreach.return_value.linkedin_note = "Hi! Interested in the DevOps role."
            mock_outreach.return_value.email_subject = "DevOps Engineer Application - Ali Hamed"
            mock_outreach.return_value.email_body = "Dear Hiring Manager,\n\nI am writing to express my interest."

            resp = client.post(f"/api/applications/{app_rec.id}/outreach", headers=auth_headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["application_id"] == app_rec.id
            assert "DevOps" in data["linkedin_note"]
    finally:
        db_session.close()


def test_export_applications_endpoint(client, auth_headers):
    resp_json = client.get("/api/export/applications?format=json", headers=auth_headers)
    assert resp_json.status_code == 200
    assert isinstance(resp_json.json(), list)

    resp_csv = client.get("/api/export/applications?format=csv", headers=auth_headers)
    assert resp_csv.status_code == 200
    assert "text/csv" in resp_csv.headers["content-type"]
    assert "application_id,job_id" in resp_csv.text
