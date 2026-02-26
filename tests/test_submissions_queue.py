from uuid import uuid4

from fastapi.testclient import TestClient

from api.main import app
from db.models import Application, ExtractedURL, Job, JobStatus, Message, Submission, SubmissionStatus
from db.session import get_session_factory, init_db


def test_submissions_list_and_retry(monkeypatch):
    init_db()
    db = get_session_factory()()
    try:
        msg = Message(
            whatsapp_message_id=f"msg-sub-{uuid4().hex[:8]}",
            sender_phone="15550001111",
            body="https://example.com/jobs/1",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://example.com/jobs/1",
            normalized_url="https://example.com/jobs/1",
            url_hash=f"hash-sub-{uuid4().hex[:8]}",
        )
        db.add(extracted)
        db.flush()

        job = Job(
            extracted_url_id=extracted.id,
            title="Backend Engineer",
            company="Acme",
            source_url="https://example.com/jobs/1",
            apply_url="https://jobs.lever.co/acme/123",
            status=JobStatus.APPROVED,
            score=82,
        )
        db.add(job)
        db.flush()

        app_row = Application(job_id=job.id, status=JobStatus.APPROVED)
        db.add(app_row)
        db.flush()

        sub = Submission(
            application_id=app_row.id,
            submitter_name="lever",
            status=SubmissionStatus.FAILED,
            error_message="timeout",
        )
        db.add(sub)
        db.commit()

        from worker.tasks import submit_application_task

        queued = []
        monkeypatch.setattr(submit_application_task, "delay", lambda app_id: queued.append(app_id))

        with TestClient(app) as client:
            rows = client.get("/api/submissions")
            assert rows.status_code == 200
            assert any(r["application_id"] == app_row.id for r in rows.json())

            retry = client.post(f"/api/applications/{app_row.id}/retry-submit")
            assert retry.status_code == 200
            assert queued == [app_row.id]
    finally:
        db.close()


def test_retry_submit_force_allows_override(monkeypatch):
    init_db()
    db = get_session_factory()()
    try:
        msg = Message(
            whatsapp_message_id=f"msg-sub-force-{uuid4().hex[:8]}",
            sender_phone="15550001111",
            body="https://example.com/jobs/force",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://example.com/jobs/force",
            normalized_url="https://example.com/jobs/force",
            url_hash=f"hash-sub-force-{uuid4().hex[:8]}",
        )
        db.add(extracted)
        db.flush()

        job = Job(
            extracted_url_id=extracted.id,
            title="Ops Engineer",
            company="Acme",
            source_url="https://example.com/jobs/force",
            apply_url="https://jobs.lever.co/acme/force",
            status=JobStatus.DRAFT,
            score=60,
        )
        db.add(job)
        db.flush()

        app_row = Application(job_id=job.id, status=JobStatus.DRAFT)
        db.add(app_row)
        db.flush()

        sub = Submission(
            application_id=app_row.id,
            submitter_name="lever",
            status=SubmissionStatus.NEEDS_HUMAN_CONFIRMATION,
            error_message="needs manual",
        )
        db.add(sub)
        db.commit()

        from worker.tasks import submit_application_task

        queued = []
        monkeypatch.setattr(submit_application_task, "delay", lambda app_id: queued.append(app_id))

        with TestClient(app) as client:
            retry = client.post(f"/api/applications/{app_row.id}/retry-submit?force=true")
            assert retry.status_code == 200
            assert queued == [app_row.id]
    finally:
        db.close()
