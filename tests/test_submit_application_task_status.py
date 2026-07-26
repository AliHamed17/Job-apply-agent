"""The legacy application-ID task is a strict database-command no-op."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Application, Base, Job, JobStatus, Submission
from worker import tasks as tasks_module


def _make_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'd.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_submit_application_task_requires_database_command_without_mutation(tmp_path):
    factory = _make_factory(tmp_path)

    db = factory()
    job = Job(
        title="RF Engineer",
        company="Acme",
        location="Remote",
        apply_url="https://example.com/jobs/1",
        source_url="https://example.com/jobs/1",
        status=JobStatus.APPROVED,
        score=90.0,
    )
    db.add(job)
    db.flush()
    app = Application(
        job_id=job.id,
        status=JobStatus.APPROVED,
        cover_letter="x",
        recruiter_message="y",
        qa_answers="{}",
    )
    db.add(app)
    db.commit()
    app_id, job_id = app.id, job.id
    db.close()

    result = tasks_module.submit_application_task.apply(args=[app_id]).get()

    assert result == {
        "state": "blocked",
        "reason_code": "DATABASE_COMMAND_REQUIRED",
    }

    check = factory()
    try:
        refreshed_app = check.get(Application, app_id)
        refreshed_job = check.get(Job, job_id)
        assert refreshed_app.status == JobStatus.APPROVED
        assert refreshed_job.status == JobStatus.APPROVED
        assert check.query(Submission).filter(Submission.application_id == app_id).count() == 0
    finally:
        check.close()
