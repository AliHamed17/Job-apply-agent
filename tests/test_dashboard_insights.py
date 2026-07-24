from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from api.main import app
from db.models import (
    Application,
    ExtractedURL,
    Job,
    JobStatus,
    Message,
    Submission,
    SubmissionStatus,
    URLStatus,
)
from db.session import get_session_factory, init_db

client = TestClient(app)


def _auth():
    from core.config import get_settings

    return {"Authorization": f"Bearer {get_settings().secret_key}"}


def test_dashboard_insights_highlights_backlog_and_opportunities(tmp_path, monkeypatch):
    db_path = tmp_path / "insights.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import core.config as config_module
    import db.session as session_module

    config_module.get_settings.cache_clear()
    session_module._engine = None
    session_module._SessionLocal = None
    init_db()

    session = get_session_factory()()
    try:
        old = datetime.utcnow() - timedelta(days=2)
        message = Message(
            whatsapp_message_id="insights-1",
            sender_phone="api",
            body="https://example.test/job",
        )
        session.add(message)
        session.flush()
        url = ExtractedURL(
            message_id=message.id,
            original_url="https://example.test/job",
            normalized_url="https://example.test/job",
            url_hash="insights-hash",
            status=URLStatus.PENDING,
            created_at=old,
        )
        session.add(url)
        job = Job(
            title="Senior ML Engineer",
            company="ExampleCo",
            source_url="https://example.test/job",
            apply_url="https://example.test/apply",
            status=JobStatus.SCORED,
            score=91.5,
            created_at=datetime.utcnow(),
        )
        session.add(job)
        session.flush()
        app_row = Application(job_id=job.id, status=JobStatus.DRAFT)
        session.add(app_row)
        session.flush()
        session.add(
            Submission(
                application_id=app_row.id,
                attempt_number=1,
                submitter_name="lever",
                status=SubmissionStatus.UNKNOWN,
            )
        )
        session.commit()
    finally:
        session.close()

    response = client.get(
        "/api/dashboard/insights?stale_hours=1&limit=3", headers=_auth()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["queue_depth"]["urls_pending"] == 1
    assert body["stale"]["urls_pending"] == 1
    assert body["top_opportunities"][0]["title"] == "Senior ML Engineer"
    assert any(
        item["name"] == "Unknown submission outcomes" for item in body["bottlenecks"]
    )
