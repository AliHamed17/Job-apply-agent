import threading
from contextlib import contextmanager
from profile.models import UserProfile
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Application, Base, Job, JobStatus, UserProfileVersion
from worker.rescore import (
    _drain_eager_pending_job_rescore,
    _start_eager_pending_job_rescore,
    auto_prepare_scored_jobs_if_ready,
    enqueue_pending_job_rescore,
    recover_eager_pending_job_rescore,
    requeue_scored_jobs_for_preparation,
    rescore_pending_jobs,
    rescore_pending_jobs_batch,
    wait_for_eager_pending_job_rescores,
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


def test_version_bound_rescore_is_bounded_and_rejects_superseded_profile(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'version-bound-rescore.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    first = Job(
        title="RF Engineer",
        company="Example",
        source_url="https://example.test/version-bound-rescore-1",
        status=JobStatus.SCORED,
    )
    second = Job(
        title="RAN Engineer",
        company="Example",
        source_url="https://example.test/version-bound-rescore-2",
        status=JobStatus.DRAFT,
    )
    db.add_all(
        [
            first,
            second,
            UserProfileVersion(version=2, profile_yaml="{}"),
        ]
    )
    db.commit()
    first_id = first.id
    second_id = second.id

    stale_profile = UserProfile()
    stale_profile.preferences.roles = ["RF Engineer"]
    stale = rescore_pending_jobs_batch(
        db,
        stale_profile,
        expected_profile_version=1,
        after_job_id=0,
        batch_size=1,
    )
    assert stale.superseded is True
    assert stale.updated == 0
    assert db.get(Job, first_id).score is None

    lock_state = {"held": False}

    @contextmanager
    def observed_profile_transaction(_db):
        lock_state["held"] = True
        try:
            yield
        finally:
            lock_state["held"] = False

    def score_outside_profile_lock(*_args, **_kwargs):
        assert lock_state["held"] is False
        return MagicMock(total=87.0)

    current_profile = UserProfile()
    current_profile.preferences.roles = ["RF Engineer"]
    with (
        patch(
            "profile.writer.profile_write_transaction",
            side_effect=observed_profile_transaction,
        ),
        patch("worker.rescore.score_job", side_effect=score_outside_profile_lock),
    ):
        first_batch = rescore_pending_jobs_batch(
            db,
            current_profile,
            expected_profile_version=2,
            after_job_id=0,
            batch_size=1,
        )
        second_batch = rescore_pending_jobs_batch(
            db,
            current_profile,
            expected_profile_version=2,
            after_job_id=first_batch.last_job_id,
            batch_size=1,
        )

    assert first_batch.updated == 1
    assert first_batch.has_more is True
    assert second_batch.updated == 1
    assert second_batch.has_more is False
    assert db.get(Job, first_id).score == 87.0
    assert db.get(Job, second_id).score == 87.0
    assert lock_state["held"] is False
    db.close()
    engine.dispose()


def test_pending_rescore_enqueue_dispatches_one_bounded_controller(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rescore-enqueue.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    db.add(
        Job(
            title="Queued rescore",
            company="Example",
            source_url="https://example.test/rescore-enqueue",
            status=JobStatus.SCORED,
        )
    )
    db.commit()
    settings = Settings(_env_file=None, tasks_always_eager=False)

    with patch("worker.tasks.rescore_pending_jobs_task") as task:
        queued = enqueue_pending_job_rescore(
            db,
            settings,
            expected_profile_version=3,
        )

    assert queued == 1
    task.delay.assert_called_once_with(3, 0)
    task.apply.assert_not_called()
    db.close()
    engine.dispose()


def test_eager_enqueue_starts_background_drainer_without_task_apply(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'eager-rescore-enqueue.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    db.add(
        Job(
            title="Eager queued rescore",
            company="Example",
            source_url="https://example.test/eager-rescore-enqueue",
            status=JobStatus.SCORED,
        )
    )
    db.commit()
    settings = Settings(
        _env_file=None,
        tasks_always_eager=True,
        preparation_requeue_batch_size=7,
    )

    with (
        patch("worker.rescore._start_eager_pending_job_rescore", return_value=True) as start,
        patch("worker.tasks.rescore_pending_jobs_task") as task,
    ):
        queued = enqueue_pending_job_rescore(
            db,
            settings,
            expected_profile_version=5,
        )

    assert queued == 1
    start.assert_called_once_with(
        expected_profile_version=5,
        batch_size=7,
    )
    task.apply.assert_not_called()
    task.delay.assert_not_called()
    db.close()
    engine.dispose()


def test_eager_celery_rescore_task_processes_only_one_page(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'eager-rescore-task.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    db.add_all(
        [
            Job(
                title="First eager task row",
                company="Example",
                source_url="https://example.test/eager-rescore-task-1",
                status=JobStatus.SCORED,
            ),
            Job(
                title="Second eager task row",
                company="Example",
                source_url="https://example.test/eager-rescore-task-2",
                status=JobStatus.SCORED,
            ),
            UserProfileVersion(version=6, profile_yaml="{}"),
        ]
    )
    db.commit()
    db.close()
    settings = Settings(
        _env_file=None,
        tasks_always_eager=True,
        preparation_requeue_batch_size=1,
    )

    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
    ):
        from worker.tasks import rescore_pending_jobs_task

        result = rescore_pending_jobs_task.run(6, 0)

    assert result["updated"] == 1
    assert result["has_more"] is True
    check = factory()
    scores = [row.score for row in check.query(Job).order_by(Job.id).all()]
    assert sum(score is not None for score in scores) == 1
    check.close()
    engine.dispose()


def test_eager_rescore_drainer_iterates_through_entire_backlog(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rescore-controller.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    db.add_all(
        [
            Job(
                title="First queued rescore",
                company="Example",
                source_url="https://example.test/rescore-controller-1",
                status=JobStatus.SCORED,
            ),
            Job(
                title="Second queued rescore",
                company="Example",
                source_url="https://example.test/rescore-controller-2",
                status=JobStatus.DRAFT,
            ),
            UserProfileVersion(version=4, profile_yaml="{}"),
        ]
    )
    db.commit()
    db.close()
    settings = Settings(
        _env_file=None,
        tasks_always_eager=True,
        preparation_requeue_batch_size=1,
    )

    with patch("db.session.get_session_factory", return_value=factory):
        updated = _drain_eager_pending_job_rescore(
            expected_profile_version=4,
            batch_size=settings.preparation_requeue_batch_size,
        )

    assert updated == 2
    check = factory()
    rows = check.query(Job).order_by(Job.id).all()
    assert all(job.score is not None for job in rows)
    check.close()
    engine.dispose()


def test_eager_rescore_is_non_daemon_and_joined_at_shutdown():
    entered = threading.Event()
    release = threading.Event()

    def block_until_shutdown(**_kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return 0

    with patch(
        "worker.rescore._drain_eager_pending_job_rescore",
        side_effect=block_until_shutdown,
    ):
        assert _start_eager_pending_job_rescore(
            expected_profile_version=91,
            batch_size=1,
        )
        assert entered.wait(timeout=5)
        managed = next(
            thread for thread in threading.enumerate() if thread.name == "profile-rescore-v91"
        )
        assert managed.daemon is False
        assert wait_for_eager_pending_job_rescores(timeout=0) is False
        release.set()
        assert wait_for_eager_pending_job_rescores(timeout=5) is True


def test_eager_rescore_replays_latest_profile_revision_on_api_startup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'rescore-recovery.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    db.add_all(
        [
            Job(
                title="Interrupted rescore",
                company="Example",
                source_url="https://example.test/interrupted-rescore",
                status=JobStatus.SCORED,
            ),
            UserProfileVersion(version=12, profile_yaml="{}"),
        ]
    )
    db.commit()
    db.close()
    settings = Settings(
        _env_file=None,
        tasks_always_eager=True,
        preparation_requeue_batch_size=9,
    )

    with (
        patch("db.session.get_session_factory", return_value=factory),
        patch("worker.rescore._start_eager_pending_job_rescore", return_value=True) as start,
    ):
        recovered = recover_eager_pending_job_rescore(settings)

    assert recovered == 1
    start.assert_called_once_with(
        expected_profile_version=12,
        batch_size=9,
    )
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
