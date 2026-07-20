"""Tests for daily digest functionality."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Application, Base, Job, JobStatus, Submission, SubmissionStatus
from worker.digest import DigestSummary, build_digest, format_digest


def test_format_digest_readable():
    """Test that format_digest produces readable output with all fields."""
    s = DigestSummary(applied=12, needs_review=3, failed=1, outbound_sent=4)
    text = format_digest(s)
    assert "12" in text and "3" in text and "Needs review" in text


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'d.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_build_digest_counts_by_event_date_not_job_created_at(tmp_path):
    """IMPORTANT #4 — a job CREATED yesterday but SUBMITTED today must
    count in today's digest; a job created today whose Application only
    reaches NEEDS_REVIEW/FAILED tomorrow must NOT count today."""
    db = _db(tmp_path)
    today = datetime(2026, 7, 20).date()
    yesterday = datetime(2026, 7, 19)
    today_dt = datetime(2026, 7, 20, 10, 0, 0)

    # Applied: Job created yesterday, but Submission succeeded today.
    old_job = Job(title="t1", source_url="x", status=JobStatus.SUBMITTED, created_at=yesterday)
    db.add(old_job)
    db.flush()
    old_app = Application(job_id=old_job.id, status=JobStatus.SUBMITTED)
    db.add(old_app)
    db.flush()
    db.add(Submission(application_id=old_app.id, submitter_name="greenhouse",
                       status=SubmissionStatus.SUCCESS, submitted_at=today_dt))

    # Needs review: Job created today, Application.updated_at forced to today.
    nr_job = Job(title="t2", source_url="y", status=JobStatus.NEEDS_REVIEW, created_at=today_dt)
    db.add(nr_job)
    db.flush()
    nr_app = Application(job_id=nr_job.id, status=JobStatus.NEEDS_REVIEW, updated_at=today_dt)
    db.add(nr_app)

    # Failed: Job created today, but its Application was updated yesterday
    # (e.g. backdated data) — must NOT count in today's digest.
    stale_fail_job = Job(title="t3", source_url="z", status=JobStatus.FAILED, created_at=today_dt)
    db.add(stale_fail_job)
    db.flush()
    stale_fail_app = Application(job_id=stale_fail_job.id, status=JobStatus.FAILED,
                                  updated_at=yesterday)
    db.add(stale_fail_app)

    db.commit()

    summary = build_digest(db, today)
    assert summary.applied == 1
    assert summary.needs_review == 1
    assert summary.failed == 0
