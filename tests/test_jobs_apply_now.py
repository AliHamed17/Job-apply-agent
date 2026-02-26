from uuid import uuid4

from fastapi.testclient import TestClient

from api.main import app
from db.models import Application, ExtractedURL, Job, JobStatus, Message, Submission, SubmissionStatus
from db.session import get_session_factory, init_db


def test_apply_now_for_job_approves_and_queues(monkeypatch):
    init_db()
    db = get_session_factory()()
    try:
        msg = Message(
            whatsapp_message_id=f"msg-jobs-{uuid4().hex[:8]}",
            sender_phone="15550001111",
            body="https://example.com/jobs/1",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://example.com/jobs/1",
            normalized_url="https://example.com/jobs/1",
            url_hash=f"hash-jobs-{uuid4().hex[:8]}",
        )
        db.add(extracted)
        db.flush()

        job = Job(
            extracted_url_id=extracted.id,
            title="Backend Engineer",
            company="Acme",
            source_url="https://example.com/jobs/1",
            apply_url="https://example.com/jobs/1/apply",
            status=JobStatus.DRAFT,
            score=88,
        )
        db.add(job)
        db.flush()

        app_row = Application(job_id=job.id, status=JobStatus.DRAFT)
        db.add(app_row)
        db.commit()

        from worker.tasks import submit_application_task

        queued = []
        monkeypatch.setattr(submit_application_task, "delay", lambda app_id: queued.append(app_id))

        with TestClient(app) as client:
            resp = client.post(f"/api/jobs/{job.id}/apply-now")

        assert resp.status_code == 200
        assert queued == [app_row.id]

        db.expire_all()
        refreshed = db.query(Application).filter(Application.id == app_row.id).first()
        assert refreshed.status == JobStatus.APPROVED
    finally:
        db.close()


def test_apply_now_for_job_is_idempotent_for_approved(monkeypatch):
    init_db()
    db = get_session_factory()()
    try:
        msg = Message(
            whatsapp_message_id=f"msg-jobs-approved-{uuid4().hex[:8]}",
            sender_phone="15550001111",
            body="https://example.com/jobs/2",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://example.com/jobs/2",
            normalized_url="https://example.com/jobs/2",
            url_hash=f"hash-jobs-approved-{uuid4().hex[:8]}",
        )
        db.add(extracted)
        db.flush()

        job = Job(
            extracted_url_id=extracted.id,
            title="Platform Engineer",
            company="Acme",
            source_url="https://example.com/jobs/2",
            apply_url="https://example.com/jobs/2/apply",
            status=JobStatus.APPROVED,
            score=90,
        )
        db.add(job)
        db.flush()

        app_row = Application(job_id=job.id, status=JobStatus.APPROVED)
        db.add(app_row)
        db.flush()

        submission = Submission(
            application_id=app_row.id,
            submitter_name="lever",
            status=SubmissionStatus.PENDING,
        )
        db.add(submission)
        db.commit()

        from worker.tasks import submit_application_task

        queued = []
        monkeypatch.setattr(submit_application_task, "delay", lambda app_id: queued.append(app_id))

        with TestClient(app) as client:
            resp = client.post(f"/api/jobs/{job.id}/apply-now")

        assert resp.status_code == 200
        assert resp.json()["message"] == "Submission already pending or completed"
        assert queued == []
    finally:
        db.close()
