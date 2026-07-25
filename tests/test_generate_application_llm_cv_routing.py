"""Integration coverage for the worker's low-confidence CV routing fallback."""

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


def _routing_config_path(tmp_path, minimum_confidence=0.9):
    config = {
        "cvs": [
            {"id": "cv_a", "file": "a.pdf", "title_terms": ["backend"], "skills": ["python"]},
            {"id": "cv_b", "file": "b.pdf", "title_terms": ["ai"], "skills": ["pytorch"]},
        ],
        "fallback_cv_id": "cv_a",
        "minimum_confidence": minimum_confidence,
    }
    path = tmp_path / "cv_routing.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _make_job(db):
    job = Job(
        title="AI Engineer",
        company="Gitlab",
        source_url="https://x",
        status=JobStatus.SCORED,
        score=57.5,
        location="",
        employment_type="",
        seniority="",
        description="",
        requirements="",
        apply_url="",
    )
    db.add(job)
    db.flush()
    db.commit()
    return job.id


def _settings(tmp_path, minimum_confidence=0.9):
    return Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=False,
        cv_routing_path=str(_routing_config_path(tmp_path, minimum_confidence)),
        cv_directory=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_llm_routing_used_when_deterministic_falls_back(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    job_id = _make_job(db)
    db.close()

    generated = GeneratedApplication(
        cover_letter="letter",
        recruiter_message="msg",
        qa_answers={},
        has_placeholders=False,
        placeholder_fields=[],
    )
    llm_decision = RoutingDecision(
        selected_cv_id="cv_b",
        selected_file="b.pdf",
        confidence=0.9,
        matched_evidence=["llm:matched AI/ML background"],
    )

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=_settings(tmp_path)), \
         patch("profile.loader.get_profile", return_value=UserProfile()), \
         patch("llm.generation.generate_full_application", new=AsyncMock(return_value=generated)), \
         patch(
             "profile.cv_routing_llm.load_cv_excerpts",
             return_value={"cv_a": "x", "cv_b": "y"},
         ), \
         patch(
             "profile.cv_routing_llm.select_cv_via_llm",
             new=AsyncMock(return_value=llm_decision),
         ) as mock_llm:
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

    generated = GeneratedApplication(
        cover_letter="letter",
        recruiter_message="msg",
        qa_answers={},
        has_placeholders=False,
        placeholder_fields=[],
    )

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=_settings(tmp_path, 0.1)), \
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


@pytest.mark.asyncio
async def test_low_confidence_fallback_stays_review_required(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    job_id = _make_job(db)
    db.close()

    generated = GeneratedApplication(
        cover_letter="letter",
        recruiter_message="msg",
        qa_answers={},
        has_placeholders=False,
        placeholder_fields=[],
    )
    settings = Settings(
        _env_file=None,
        draft_only=False,
        auto_apply=True,
        auto_apply_threshold=50.0,
        tasks_always_eager=False,
        llm_cv_routing=False,
        llm_cv_alignment=False,
        cv_routing_path=str(_routing_config_path(tmp_path)),
        cv_directory=str(tmp_path),
    )

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=settings), \
         patch("profile.loader.get_profile", return_value=UserProfile()), \
         patch("llm.generation.generate_full_application", new=AsyncMock(return_value=generated)):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id])

    db = factory()
    app = db.query(Application).filter(Application.job_id == job_id).first()
    assert app.status == JobStatus.DRAFT
    assert app.cv_routing_fallback_reason == "confidence_below_threshold"
    assert app.needs_review_reason == "CV_ROUTING_REVIEW_REQUIRED"
    db.close()
