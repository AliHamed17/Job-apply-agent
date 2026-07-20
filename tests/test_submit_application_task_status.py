"""CRITICAL #1 regression coverage — submit_application_task must move
Application.status off APPROVED for every completed submission outcome
(draft_only, submitted, failed), not just NEEDS_REVIEW. Otherwise the
drainer (worker.drainer.select_next_application) re-selects the same
Application forever.

Runs the real task body end-to-end against an in-memory SQLite DB with
DRAFT_ONLY settings (the default), so no LinkedIn/browser/LLM/network is
exercised — only DraftOnlySubmitter, which is pure Python.
"""

from __future__ import annotations

from profile.models import UserProfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.governor as governor_module
from core.config import Settings
from db.models import Application, Base, Job, JobStatus, Submission
from worker import tasks as tasks_module


class _FakeGovernor:
    """Governor stub — draft_only mode never reaches can_act()/record_application(),
    but submit_application_task always constructs a governor, so provide a
    harmless one instead of touching the real Redis-backed singleton."""

    def can_act(self):
        return True, "ok"

    def record_application(self):
        raise AssertionError("record_application should not run in draft_only mode")


def _make_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path/'d.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_submit_application_task_sets_terminal_status_on_draft_only(tmp_path, monkeypatch):
    factory = _make_factory(tmp_path)

    db = factory()
    job = Job(title="RF Engineer", company="Acme", location="Remote",
              apply_url="https://example.com/jobs/1", source_url="https://example.com/jobs/1",
              status=JobStatus.APPROVED, score=90.0)
    db.add(job)
    db.flush()
    app = Application(job_id=job.id, status=JobStatus.APPROVED,
                       cover_letter="x", recruiter_message="y", qa_answers="{}")
    db.add(app)
    db.commit()
    app_id, job_id = app.id, job.id
    db.close()

    monkeypatch.setattr(tasks_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        tasks_module, "get_settings", lambda: Settings(_env_file=None, draft_only=True)
    )
    monkeypatch.setattr("profile.loader.get_profile", lambda: UserProfile())
    monkeypatch.setattr(governor_module, "get_governor", lambda: _FakeGovernor())

    tasks_module.submit_application_task.apply(args=[app_id])

    check = factory()
    try:
        refreshed_app = check.query(Application).filter(Application.id == app_id).one()
        refreshed_job = check.query(Job).filter(Job.id == job_id).one()
        # The bug: app.status stayed APPROVED here, so the drainer would
        # re-select and re-submit this same application on every tick.
        assert refreshed_app.status == JobStatus.DRAFT
        assert refreshed_job.status == JobStatus.DRAFT

        submission = check.query(Submission).filter(Submission.application_id == app_id).one()
        assert submission.status.value == "draft_only"

        # Belt-and-suspenders: with the fix, the drainer must not re-select
        # this application any more.
        from worker.drainer import select_next_application
        assert select_next_application(check) is None
    finally:
        check.close()
