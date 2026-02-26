"""Tests for URL visibility and URL-level auto-apply APIs."""

from uuid import uuid4

from fastapi.testclient import TestClient

from api.main import app, settings
from core.config import get_settings
from db.models import Application, ExtractedURL, Job, JobStatus, Message, URLStatus
from db.session import get_session_factory, init_db
from worker.tasks import submit_application_task


def test_list_urls_shows_pipeline_visibility():
    init_db()
    db = get_session_factory()()
    try:
        msg = Message(
            whatsapp_message_id=f"msg-auto-1-{uuid4().hex[:8]}",
            sender_phone="15550001111",
            body="https://example.com/jobs/1",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://example.com/jobs/1",
            normalized_url="https://example.com/jobs/1",
            url_hash="hash-1",
        )
        db.add(extracted)
        db.flush()

        job = Job(
            extracted_url_id=extracted.id,
            title="Backend Engineer",
            company="Acme",
            source_url="https://example.com/jobs/1",
            score=90,
            status=JobStatus.DRAFT,
        )
        db.add(job)
        db.flush()

        app_row = Application(job_id=job.id, status=JobStatus.DRAFT)
        db.add(app_row)
        db.commit()

        with TestClient(app) as client:
            resp = client.get("/api/urls")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["total"] >= 1
        assert any(item["auto_apply_candidates"] >= 1 for item in payload["items"])
        assert all("requires_auth" in item for item in payload["items"])
    finally:
        db.close()


def test_auto_apply_for_url_approves_and_queues(monkeypatch):
    init_db()
    db = get_session_factory()()
    try:
        msg = Message(
            whatsapp_message_id=f"msg-auto-1-{uuid4().hex[:8]}",
            sender_phone="15550001111",
            body="https://example.com/jobs/1",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://example.com/jobs/2",
            normalized_url="https://example.com/jobs/2",
            url_hash="hash-2",
        )
        db.add(extracted)
        db.flush()

        job = Job(
            extracted_url_id=extracted.id,
            title="Senior Engineer",
            company="Acme",
            source_url="https://example.com/jobs/2",
            score=95,
            status=JobStatus.DRAFT,
        )
        db.add(job)
        db.flush()

        app_row = Application(job_id=job.id, status=JobStatus.DRAFT)
        db.add(app_row)
        db.commit()

        queued = []
        monkeypatch.setattr(submit_application_task, "delay", lambda app_id: queued.append(app_id))

        with TestClient(app) as client:
            resp = client.post(f"/api/urls/{extracted.id}/auto-apply")
        assert resp.status_code == 200
        body = resp.json()
        assert body["approved_count"] == 1
        assert body["queued_submission_count"] == 1
        assert len(queued) == 1
    finally:
        db.close()


def test_generation_task_auto_apply(monkeypatch):
    from worker.tasks import generate_application_task

    init_db()
    db = get_session_factory()()
    runtime_settings = get_settings()
    original_auto_apply = runtime_settings.auto_apply
    original_draft_only = runtime_settings.draft_only
    try:
        settings.auto_apply = True
        settings.draft_only = False
        runtime_settings.auto_apply = True
        runtime_settings.draft_only = False

        msg = Message(
            whatsapp_message_id=f"msg-auto-1-{uuid4().hex[:8]}",
            sender_phone="15550001111",
            body="https://example.com/jobs/1",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://example.com/jobs/3",
            normalized_url="https://example.com/jobs/3",
            url_hash="hash-3",
        )
        db.add(extracted)
        db.flush()

        job = Job(
            extracted_url_id=extracted.id,
            title="Staff Engineer",
            company="Acme",
            location="Remote",
            employment_type="full-time",
            seniority="senior",
            source_url="https://example.com/jobs/3",
            apply_url="https://example.com/jobs/3/apply",
            score=92,
            status=JobStatus.DRAFT,
            description="python fastapi",
            requirements="python",
        )
        db.add(job)
        db.commit()

        class _Generated:
            cover_letter = "CL"
            recruiter_message = "RM"
            qa_answers = {}
            has_placeholders = False

        from llm import generation as generation_module

        async def _fake_generate(*_args, **_kwargs):
            return _Generated()

        monkeypatch.setattr(generation_module, "generate_full_application", _fake_generate)

        queued = []
        monkeypatch.setattr(submit_application_task, "delay", lambda app_id: queued.append(app_id))

        class _Profile:
            resume = type("R", (), {"pdf_path": None})()
            def model_dump(self):
                return {}

        from profile import loader as loader_module
        monkeypatch.setattr(loader_module, "get_profile", lambda: _Profile())

        generate_application_task.run(job.id)

        db.expire_all()
        refreshed_job = db.query(Job).filter(Job.id == job.id).first()
        refreshed_app = db.query(Application).filter(Application.job_id == job.id).first()
        assert refreshed_job.status == JobStatus.APPROVED
        assert refreshed_app.status == JobStatus.APPROVED
        assert len(queued) == 1
    finally:
        settings.auto_apply = original_auto_apply
        settings.draft_only = original_draft_only
        runtime_settings.auto_apply = original_auto_apply
        runtime_settings.draft_only = original_draft_only
        db.close()


def test_list_urls_shows_auth_provider_hint():
    init_db()
    db = get_session_factory()()
    try:
        msg = Message(
            whatsapp_message_id=f"msg-auto-auth-{uuid4().hex[:8]}",
            sender_phone="15550001111",
            body="https://careers.example.com/jobs/1",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://careers.example.com/jobs/1",
            normalized_url="https://careers.example.com/jobs/1",
            url_hash=f"hash-auth-{uuid4().hex[:8]}",
            status=URLStatus.FAILED,
            fetch_error="Redirected to accounts.google.com sign in",
        )
        db.add(extracted)
        db.commit()

        with TestClient(app) as client:
            resp = client.get("/api/urls")
        assert resp.status_code == 200
        payload = resp.json()

        matched = [item for item in payload["items"] if item["url_id"] == extracted.id][0]
        assert matched["requires_auth"] is True
        assert matched["auth_provider_hint"] == "google"
    finally:
        db.close()


def test_resolve_auth_for_url_requeues(monkeypatch):
    init_db()
    db = get_session_factory()()
    try:
        msg = Message(
            whatsapp_message_id=f"msg-auto-auth2-{uuid4().hex[:8]}",
            sender_phone="15550001111",
            body="https://careers.example.com/jobs/2",
        )
        db.add(msg)
        db.flush()

        extracted = ExtractedURL(
            message_id=msg.id,
            original_url="https://careers.example.com/jobs/2",
            normalized_url="https://careers.example.com/jobs/2",
            url_hash=f"hash-auth2-{uuid4().hex[:8]}",
            fetch_error="Authentication wall detected: sign in",
            status=URLStatus.BLOCKED,
        )
        db.add(extracted)
        db.commit()

        from worker.tasks import process_url_task
        queued = []
        monkeypatch.setattr(process_url_task, "delay", lambda url_id: queued.append(url_id))

        with TestClient(app) as client:
            resp = client.post(
                f"/api/urls/{extracted.id}/resolve-auth",
                json={"authenticated_url": "https://careers.example.com/jobs/2?session=ok"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["url_id"] == extracted.id
        assert len(queued) == 1

        db.expire_all()
        refreshed = db.query(ExtractedURL).filter(ExtractedURL.id == extracted.id).first()
        assert refreshed.fetch_error is None
    finally:
        db.close()
