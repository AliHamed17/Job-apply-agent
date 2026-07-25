import pytest
from fastapi.testclient import TestClient

from api.main import app
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from submitters.captcha_detector import detect_security_challenges


@pytest.fixture
def client():
    return TestClient(app)


def test_captcha_detector():
    normal_html = "<html><body><h1>Careers Page</h1></body></html>"
    rep_normal = detect_security_challenges(normal_html, "https://example.com/apply")
    assert rep_normal.captcha_detected is False

    cf_html = "<html><body>Just a moment... cf-turnstile</body></html>"
    rep_cf = detect_security_challenges(cf_html, "https://example.com/apply")
    assert rep_cf.captcha_detected is True
    assert rep_cf.challenge_type == "cloudflare"


def test_dry_run_endpoint(client, auth_headers):
    db_session = get_session_factory()()
    try:
        job = Job(
            title="Senior DevOps Engineer",
            company="TechScale",
            location="Haifa",
            description="Kubernetes, Terraform, AWS, Docker",
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

        resp = client.post(f"/api/applications/{app_rec.id}/dry-run", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["application_id"] == app_rec.id
        assert data["is_ready_for_submission"] is True
    finally:
        db_session.close()


def test_batch_apply_endpoint(client, auth_headers):
    resp = client.post(
        "/api/control/batch-apply",
        json={"min_score": 80.0, "max_batch_size": 5},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "triggered_count" in data
    assert "job_ids" in data
