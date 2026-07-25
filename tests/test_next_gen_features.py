import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from api.main import app
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from llm.language import detect_language


@pytest.fixture
def client():
    return TestClient(app)


def test_detect_language():
    assert detect_language("Senior Software Engineer with Python and Docker experience") == "en"
    assert detect_language("דרוש מהנדס תוכנה בעל ניסיון ב-Python ופיתוח אלגוריתמים") == "he"


def test_interview_prep_endpoint(client):
    db_session = get_session_factory()()
    try:
        job = Job(
            title="Senior AI Engineer",
            company="TechCorp",
            location="Remote",
            description="Building production AI agents and RAG pipelines",
            requirements="Python, PyTorch, LangChain, FAISS",
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

        with patch("api.routes.interview_prep.generate_interview_prep", new_callable=AsyncMock) as mock_prep:
            mock_prep.return_value.predicted_questions = ["Explain FAISS indexing mechanisms"]
            mock_prep.return_value.star_story_talking_points = ["75% deployment speedup using Jenkins"]
            mock_prep.return_value.interviewer_questions = ["What is the AI roadmap?"]

            resp = client.get(f"/api/applications/{app_rec.id}/interview-prep")
            assert resp.status_code == 200
            data = resp.json()
            assert data["application_id"] == app_rec.id
            assert "FAISS indexing" in data["predicted_questions"][0]
            assert "75% deployment speedup" in data["star_story_talking_points"][0]
    finally:
        db_session.close()


def test_widget_summary_endpoint(client):
    resp = client.get("/api/widgets/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_applications" in data
    assert "approved_count" in data
