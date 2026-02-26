from uuid import uuid4

from fastapi.testclient import TestClient

from api.main import app
from db.models import Application, ExtractedURL, Job, JobStatus, Message
from db.session import get_session_factory, init_db


def test_generate_interview_prep_for_application(monkeypatch):
    init_db()
    db = get_session_factory()()
    try:
        msg = Message(
            whatsapp_message_id=f"msg-prep-{uuid4().hex[:8]}",
            sender_phone="15550003333",
            body="https://example.com/jobs/prep",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://example.com/jobs/prep",
            normalized_url="https://example.com/jobs/prep",
            url_hash=f"hash-prep-{uuid4().hex[:8]}",
        )
        db.add(extracted)
        db.flush()

        job = Job(
            extracted_url_id=extracted.id,
            title="Senior Backend Engineer",
            company="Acme",
            location="Remote",
            source_url="https://example.com/jobs/prep",
            apply_url="https://example.com/jobs/prep/apply",
            description="Build scalable APIs in Python",
            requirements="Python, FastAPI, SQL",
            status=JobStatus.DRAFT,
            score=91,
        )
        db.add(job)
        db.flush()

        app_row = Application(job_id=job.id, status=JobStatus.DRAFT)
        db.add(app_row)
        db.commit()

        async def _fake_generate(*args, **kwargs):
            return "Role Snapshot\n- Build APIs\n\nLikely Technical Questions\n- API design"

        monkeypatch.setattr("llm.generation.generate_interview_prep", _fake_generate)

        with TestClient(app) as client:
            resp = client.post(f"/api/applications/{app_row.id}/interview-prep")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["application_id"] == app_row.id
        assert payload["job_id"] == job.id
        assert "Role Snapshot" in payload["prep"]
    finally:
        db.close()
