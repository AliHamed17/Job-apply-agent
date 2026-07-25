import json
import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from api.main import app
from db.models import Application, Job, JobStatus
from db.session import get_session_factory


@pytest.fixture
def client():
    return TestClient(app)


def test_realign_application_endpoint(client):
    db_session = get_session_factory()()
    try:
        # Create test job & application
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
            cover_letter="Old cover letter",
            recruiter_message="Old message",
            qa_answers=json.dumps({"why_this_role": "Old answer"}),
            status=JobStatus.DRAFT,
        )
        db_session.add(app_rec)
        db_session.commit()
        db_session.refresh(app_rec)

        with patch("api.routes.realign.generate_full_application", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value.cover_letter = "Tailored AI Engineer Cover Letter with 75% speedup metric"
            mock_gen.return_value.recruiter_message = "Hi, interested in AI role"
            mock_gen.return_value.qa_answers = {"why_this_role": "Experienced with RAG and LLMs"}
            mock_gen.return_value.has_placeholders = False
            mock_gen.return_value.placeholder_fields = []

            response = client.post(
                f"/api/applications/{app_rec.id}/realign",
                json={"forced_cv_id": "ai-engineer"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["id"] == app_rec.id
            assert data["selected_cv_id"] == "ai-engineer"
            assert "75% speedup" in data["cover_letter"]
            assert data["qa_answers"]["why_this_role"] == "Experienced with RAG and LLMs"
    finally:
        db_session.close()
