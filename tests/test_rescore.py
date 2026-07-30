from profile.models import UserProfile
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Application, Base, Job, JobStatus, UserProfileVersion
from worker.rescore import (
    auto_prepare_scored_jobs_if_ready,
    requeue_scored_jobs_for_preparation,
    rescore_pending_jobs,
)


class _Job:
    def __init__(self, title):
        self.title = title
        self.company = ""
        self.location = ""
        self.employment_type = ""
        self.seniority = ""
        self.description = ""
        self.requirements = ""
        self.apply_url = ""
        self.source_url = "x"
        self.date_posted = ""
        self.keywords = None
        self.status = None
        self.score = None


class _Query:
    def __init__(self, jobs):
        self._jobs = jobs

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._jobs


class _DB:
    def __init__(self, jobs):
        self._jobs = jobs
        self.committed = False

    def query(self, *a, **k):
        return _Query(self._jobs)

    def commit(self):
        self.committed = True


def test_rescore_updates_scores():
    jobs = [_Job("RF Engineer")]
    jobs[0].status = JobStatus.SCORED
    prof = UserProfile()
    prof.preferences.roles = ["RF Engineer"]
    n = rescore_pending_jobs(_DB(jobs), prof)
    assert n == 1
    assert jobs[0].score is not None


def test_version_bound_rescore_rejects_superseded_profile(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'version-bound-rescore.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    job = Job(
        title="RF Engineer",
        company="Example",
        source_url="https://example.test/version-bound-rescore",
        status=JobStatus.SCORED,
    )
    db.add_all(
        [
            job,
            UserProfileVersion(version=2, profile_yaml="{}"),
        ]
    )
    db.commit()
    job_id = job.id

    stale_profile = UserProfile()
    stale_profile.preferences.roles = ["RF Engineer"]
    assert (
        rescore_pending_jobs(
            db,
            stale_profile,
            expected_profile_version=1,
        )
        == 0
    )
    assert db.get(Job, job_id).score is None

    current_profile = UserProfile()
    current_profile.preferences.roles = ["RF Engineer"]
    assert (
        rescore_pending_jobs(
            db,
            current_profile,
            expected_profile_version=2,
        )
        == 1
    )
    assert db.get(Job, job_id).score is not None
    db.close()
    engine.dispose()


def test_requeue_scored_jobs_excludes_rows_with_an_application(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'requeue.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    eligible = Job(
        title="Eligible",
        company="Example",
        source_url="https://example.test/eligible",
        status=JobStatus.SCORED,
    )
    already_prepared = Job(
        title="Prepared",
        company="Example",
        source_url="https://example.test/prepared",
        status=JobStatus.SCORED,
    )
    already_prepared.application = Application(status=JobStatus.DRAFT)
    deferred = Job(
        title="Deferred",
        company="Example",
        source_url="https://example.test/deferred",
        status=JobStatus.SCORED,
    )
    db.add_all([eligible, already_prepared, deferred])
    db.commit()
    eligible_id = eligible.id

    with patch("worker.tasks.score_job_task") as score_task:
        queued = requeue_scored_jobs_for_preparation(
            db,
            tasks_always_eager=False,
            batch_size=1,
        )

    assert queued == 1
    score_task.delay.assert_called_once_with(eligible_id, True)
    score_task.apply.assert_not_called()
    db.close()
    engine.dispose()


def test_disabled_auto_prepare_does_not_probe_or_requeue():
    db = MagicMock()
    settings = Settings(_env_file=None, auto_apply=False)

    assert auto_prepare_scored_jobs_if_ready(db, settings) == 0
    db.query.assert_not_called()
