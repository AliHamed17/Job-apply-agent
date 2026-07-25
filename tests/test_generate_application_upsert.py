"""Regression: generate_application_task used to unconditionally INSERT a
new Application row. Application.job_id is UNIQUE, so re-running the task
for a job that already has one (a regenerate action, or Celery's own retry
landing after a transient error on a later line) raised IntegrityError on
every attempt — burning a full, real LLM generation each time without ever
persisting the result. Fixed to update the existing row in place.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Application, Base, Job, JobStatus
from llm.generation import GeneratedApplication
from profile.models import UserProfile


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gen.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.mark.asyncio
async def test_regenerating_updates_existing_application_not_insert(tmp_path):
    factory = _db(tmp_path)
    db = factory()
    job = Job(
        title="AI Engineer", company="Gitlab", source_url="https://x",
        status=JobStatus.SCORED, score=57.5,
        location="", employment_type="", seniority="", description="",
        requirements="", apply_url="",
    )
    db.add(job)
    db.flush()
    existing = Application(job_id=job.id, cover_letter="OLD mock text", status=JobStatus.DRAFT)
    db.add(existing)
    db.commit()
    existing_id = existing.id
    job_id = job.id
    db.close()

    settings = Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=False,
        cv_routing_path="does-not-exist.yaml",  # skip CV routing entirely
    )
    fresh_result = GeneratedApplication(
        cover_letter="NEW real cover letter from the LLM",
        recruiter_message="NEW recruiter message",
        qa_answers={"q1": "a1"},
        has_placeholders=False,
        placeholder_fields=[],
    )

    with patch("worker.tasks.get_session_factory", return_value=factory), \
         patch("worker.tasks.get_settings", return_value=settings), \
         patch("profile.loader.get_profile", return_value=UserProfile()), \
         patch("llm.generation.generate_full_application", new=AsyncMock(return_value=fresh_result)):
        from worker.tasks import generate_application_task
        generate_application_task.apply(args=[job_id])

    db = factory()
    apps = db.query(Application).filter(Application.job_id == job_id).all()
    assert len(apps) == 1, "must update the existing row, not insert a second one"
    assert apps[0].id == existing_id
    assert apps[0].cover_letter == "NEW real cover letter from the LLM"
    db.close()
