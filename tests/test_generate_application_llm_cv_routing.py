"""generate_application_task's LLM-based CV routing fallback.

The deterministic keyword matcher (profile/cv_routing.py::route_cv) only
has signal to score when the job posting was actually scraped. It should
be left alone when confident, and only handed off to the LLM reader
(profile/cv_routing_llm.py) when it couldn't confidently pick a CV.
"""

from __future__ import annotations

from profile.cv_routing import RoutingDecision
from profile.models import UserProfile
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Application, Base, Job, JobStatus
from llm.generation import GeneratedApplication


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gen.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _routing_config_path(tmp_path):
    config = {
        "cvs": [
            {"id": "cv_a", "file": "a.pdf", "title_terms": ["backend"], "skills": ["python"]},
            {"id": "cv_b", "file": "b.pdf", "title_terms": ["ai"], "skills": ["pytorch"]},
        ],
        "fallback_cv_id": "cv_a",
        "minimum_confidence": 0.9,  # unreachable — forces every job into fallback
    }
    path = tmp_path / "cv_routing.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _make_job(db):
    job = Job(
        title="AI Engineer", company="Gitlab", source_url="https://x",
        status=JobStatus.SCORED, score=57.5,
        location="", employment_type="", seniority="", description="",
        requirements="", apply_url="",
    )
    db.add(job)
    db.flush()
    db.commit()
    return job.id


@pytest.mark.asyncio
async def test_llm_routing_used_when_deterministic_falls_back(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    job_id = _make_job(db)
    db.close()

    settings = Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=False,
        cv_routing_path=str(_routing_config_path(tmp_path)),
        cv_directory=str(tmp_path),
    )
    generated = GeneratedApplication(
        cover_letter="letter", recruiter_message="msg", qa_answers={},
        has_placeholders=False, placeholder_fields=[],
    )
    llm_decision = RoutingDecision(
        selected_cv_id="cv_b", selected_file="b.pdf", confidence=0.9,
        matched_evidence=["llm:matched AI/ML background"],
    )

    gen_mock = AsyncMock(return_value=generated)
    excerpts = {"cv_a": "x", "cv_b": "y"}
    llm_mock = AsyncMock(return_value=llm_decision)

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=settings), \
         patch("profile.loader.get_profile", return_value=UserProfile()), \
         patch("llm.generation.generate_full_application", new=gen_mock), \
         patch("profile.cv_routing_llm.load_cv_excerpts", return_value=excerpts), \
         patch("profile.cv_routing_llm.select_cv_via_llm", new=llm_mock) as mock_llm:
        from worker.tasks import generate_application_task
        generate_application_task.apply(args=[job_id])

    mock_llm.assert_called_once()
    db = factory()
    app = db.query(Application).filter(Application.job_id == job_id).first()
    assert app.selected_cv_id == "cv_b"
    assert app.cv_routing_confidence == 0.9
    db.close()


@pytest.mark.asyncio
async def test_llm_routing_skipped_when_deterministic_is_confident(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    job_id = _make_job(db)
    db.close()

    config = {
        "cvs": [
            {"id": "cv_a", "file": "a.pdf", "title_terms": ["backend"], "skills": ["python"]},
            {
                "id": "cv_b", "file": "b.pdf",
                "title_terms": ["ai", "engineer"], "skills": ["pytorch"],
            },
        ],
        "fallback_cv_id": "cv_a",
        "minimum_confidence": 0.1,  # easily cleared by the "ai engineer" title match
    }
    path = tmp_path / "cv_routing.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    settings = Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=False,
        cv_routing_path=str(path),
        cv_directory=str(tmp_path),
    )
    generated = GeneratedApplication(
        cover_letter="letter", recruiter_message="msg", qa_answers={},
        has_placeholders=False, placeholder_fields=[],
    )

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=settings), \
         patch("profile.loader.get_profile", return_value=UserProfile()), \
         patch("llm.generation.generate_full_application", new=AsyncMock(return_value=generated)), \
         patch("profile.cv_routing_llm.select_cv_via_llm", new=AsyncMock()) as mock_llm:
        from worker.tasks import generate_application_task
        generate_application_task.apply(args=[job_id])

    mock_llm.assert_not_called()
    db = factory()
    app = db.query(Application).filter(Application.job_id == job_id).first()
    assert app.selected_cv_id == "cv_b"
    db.close()
