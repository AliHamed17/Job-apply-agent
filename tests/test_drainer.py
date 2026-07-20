from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db.models import Base, Job, Application, JobStatus
from worker.drainer import select_next_application, expire_stale_jobs


def _db(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path/'d.db'}")
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)()


def _job(db, score, status=JobStatus.APPROVED, created=None):
    j = Job(title="t", source_url="x", status=status, score=score)
    if created:
        j.created_at = created
    db.add(j); db.flush()
    return j


def test_select_highest_score_first(tmp_path):
    db = _db(tmp_path)
    j1 = _job(db, 50); j2 = _job(db, 90)
    for j in (j1, j2):
        db.add(Application(job_id=j.id, status=JobStatus.APPROVED))
    db.commit()
    app_id = select_next_application(db)
    picked = db.query(Application).filter(Application.id == app_id).one()
    assert picked.job_id == j2.id  # score 90 wins


def test_expire_stale(tmp_path):
    db = _db(tmp_path)
    old = datetime(2026, 7, 1); now = datetime(2026, 7, 20)
    _job(db, 30, status=JobStatus.SCORED, created=old)
    db.commit()
    n = expire_stale_jobs(db, now=now, ttl_days=7)
    assert n == 1
    assert db.query(Job).first().status == JobStatus.SKIPPED
