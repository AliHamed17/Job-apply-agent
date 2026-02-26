from uuid import uuid4

from fastapi.testclient import TestClient

from api.main import app
from db.models import Application, ExtractedURL, Job, JobStatus, Message, Submission, SubmissionStatus
from db.session import get_session_factory, init_db


def test_dashboard_counts_success_submissions_with_enum_status():
    init_db()
    db = get_session_factory()()
    try:
        msg = Message(
            whatsapp_message_id=f"msg-dashboard-{uuid4().hex[:8]}",
            sender_phone="15550002222",
            body="https://example.com/jobs/dashboard",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://example.com/jobs/dashboard",
            normalized_url="https://example.com/jobs/dashboard",
            url_hash=f"hash-dashboard-{uuid4().hex[:8]}",
        )
        db.add(extracted)
        db.flush()

        job = Job(
            extracted_url_id=extracted.id,
            title="Dashboard Test Role",
            company="Acme",
            source_url="https://example.com/jobs/dashboard",
            apply_url="https://example.com/jobs/dashboard/apply",
            status=JobStatus.APPROVED,
            score=90,
        )
        db.add(job)
        db.flush()

        app_row = Application(job_id=job.id, status=JobStatus.APPROVED)
        db.add(app_row)
        db.flush()

        db.add(
            Submission(
                application_id=app_row.id,
                submitter_name="lever",
                status=SubmissionStatus.SUCCESS,
            )
        )
        db.commit()

        with TestClient(app) as client:
            resp = client.get("/api/dashboard")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["submissions_total"] >= 1
        assert payload["submissions_success"] >= 1
    finally:
        db.close()
