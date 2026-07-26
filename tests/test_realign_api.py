import hashlib
import json
from profile.models import CVArtifact, SelectedCVArtifact
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.config import Settings
from db.models import Application, Job, JobStatus
from db.session import get_session_factory
from llm.generation import GeneratedApplication


@pytest.fixture
def client():
    return TestClient(app)


def test_realign_application_endpoint(client, auth_headers, tmp_path):
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

        generated = GeneratedApplication(
            cover_letter="Tailored AI Engineer Cover Letter with 75% speedup metric",
            recruiter_message="Hi, interested in AI role",
            qa_answers={"why_this_role": "Experienced with RAG and LLMs"},
        )
        cv_bytes = b"%PDF-1.4\nsynthetic test-only CV artifact\n"
        cv_path = tmp_path / "ai-engineer.pdf"
        cv_path.write_bytes(cv_bytes)
        cv_digest = hashlib.sha256(cv_bytes).hexdigest()
        selected_cv = SelectedCVArtifact(
            cv_id="ai-engineer",
            resolved_path=str(cv_path),
            artifact=CVArtifact(
                pdf_sha256=cv_digest,
                byte_size=len(cv_bytes),
                extracted_text="Relevant experience: Built synthetic test systems.",
            ),
        )
        routing_path = tmp_path / "cv-routing-fixture.yaml"
        routing_path.write_text("# loader is patched with a sanitized fixture\n", encoding="utf-8")
        settings = Settings(
            _env_file=None,
            cv_routing_path=str(routing_path),
            cv_directory=str(tmp_path),
            llm_cv_alignment=False,
        )
        with (
            patch("api.routes.realign.get_settings", return_value=settings),
            patch("api.routes.realign.load_routing_config", return_value=object()),
            patch(
                "api.routes.realign.load_configured_cv_artifacts",
                return_value={"ai-engineer": selected_cv},
            ),
            patch(
                "api.routes.realign.generate_full_application",
                new=AsyncMock(return_value=generated),
            ),
        ):
            response = client.post(
                f"/api/applications/{app_rec.id}/realign",
                json={"forced_cv_id": "ai-engineer"},
                headers=auth_headers,
            )

            assert response.status_code == 200
            data = response.json()
            assert data["id"] == app_rec.id
            assert data["selected_cv_id"] == "ai-engineer"
            assert data["selected_cv_hash"] == cv_digest
            assert "75% speedup" in data["cover_letter"]
            assert data["qa_answers"]["why_this_role"] == "Experienced with RAG and LLMs"
    finally:
        db_session.close()
