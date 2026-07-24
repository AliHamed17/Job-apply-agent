"""CRITICAL #1 regression coverage — the drainer must never re-select (and
thus re-drive) an Application that has already produced a Submission.

Two layers are covered:
1. The defensive Submission-row guard in ``select_next_application`` itself
   (belt-and-suspenders even if something else left ``status`` wrong).
2. The actual fix: once ``worker.tasks.submit_application_task`` transitions
   ``Application.status`` off ``APPROVED`` for a completed outcome, the
   drainer must stop selecting it.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Application, Base, Job, JobStatus, Submission, SubmissionStatus
from worker.drainer import select_next_application


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'d.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _job(db, score, status=JobStatus.APPROVED):
    j = Job(title="t", source_url="x", status=status, score=score)
    db.add(j)
    db.flush()
    return j


def test_skips_approved_application_with_existing_submission(tmp_path):
    """A stray APPROVED Application that already has a Submission row
    (e.g. left over from the pre-fix bug) must never be re-selected —
    re-selecting it would re-drive a live submit and hit the
    Submission.application_id UNIQUE constraint."""
    db = _db(tmp_path)
    j = _job(db, 90)
    app = Application(job_id=j.id, status=JobStatus.APPROVED)
    db.add(app)
    db.flush()
    db.add(Submission(application_id=app.id, submitter_name="linkedin",
                       status=SubmissionStatus.SUCCESS))
    db.commit()

    assert select_next_application(db) is None


def test_skips_submitted_application_and_picks_next_candidate(tmp_path):
    """Same guard, but proves it doesn't just return None blindly — a
    second, legitimately-pending APPROVED application is still found even
    though the stale one has the higher score."""
    db = _db(tmp_path)
    stale = _job(db, 90)  # higher score, but already has a Submission
    fresh = _job(db, 50)
    app_stale = Application(job_id=stale.id, status=JobStatus.APPROVED)
    app_fresh = Application(job_id=fresh.id, status=JobStatus.APPROVED)
    db.add_all([app_stale, app_fresh])
    db.flush()
    db.add(Submission(application_id=app_stale.id, submitter_name="linkedin",
                       status=SubmissionStatus.SUCCESS))
    db.commit()

    picked_id = select_next_application(db)
    picked = db.query(Application).filter(Application.id == picked_id).one()
    assert picked.job_id == fresh.id


def test_setting_app_status_submitted_removes_it_from_selection(tmp_path):
    """Pins the core livelock fix directly: once app.status leaves
    APPROVED (what submit_application_task now does on every completed
    outcome), the drainer must stop selecting it — independent of
    whether a Submission row exists at all."""
    db = _db(tmp_path)
    j = _job(db, 90)
    app = Application(job_id=j.id, status=JobStatus.APPROVED)
    db.add(app)
    db.commit()

    assert select_next_application(db) == app.id  # selectable before transition

    app.status = JobStatus.SUBMITTED
    db.commit()

    assert select_next_application(db) is None
