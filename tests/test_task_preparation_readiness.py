"""Execution-time preparation readiness regression coverage."""

from profile.models import UserProfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Application, Base, Job, JobStatus
from match.scoring import Action


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'task-preparation-readiness.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.parametrize(
    ("auto_apply", "canonical_ready", "expected_status", "queues_generation"),
    [
        (False, True, JobStatus.SCORED, False),
        (True, False, JobStatus.SCORED, False),
        (True, True, JobStatus.DRAFT, True),
    ],
)
def test_scoring_rechecks_preparation_readiness_at_execution(
    tmp_path,
    auto_apply,
    canonical_ready,
    expected_status,
    queues_generation,
):
    factory = _factory(tmp_path)
    db = factory()
    job = Job(
        title="Execution-time readiness",
        company="Example",
        location="Israel",
        description="Validate the current preparation gate.",
        requirements="Python",
        source_url="https://example.test/execution-readiness",
        apply_url="https://example.test/execution-readiness",
        status=JobStatus.EXTRACTED,
    )
    db.add(job)
    db.commit()
    job_id = job.id
    db.close()

    settings = Settings(
        _env_file=None,
        auto_apply=auto_apply,
        draft_only=True,
        tasks_always_eager=False,
    )
    automation = {
        "preparation_ready": canonical_ready,
        "stages": {
            "preparation": {
                "ready": canonical_ready,
                "reason_codes": [] if canonical_ready else ["LLM_NOT_READY"],
            }
        },
    }

    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch("profile.loader.load_profile_snapshot", return_value=UserProfile()),
        patch(
            "worker.tasks.score_job",
            return_value=SimpleNamespace(total=95.0, skip_reason=None),
        ),
        patch("worker.tasks.decide_action", return_value=Action.AUTO_APPLY),
        patch(
            "core.operations.readiness_report",
            return_value={"status": "ready", "checks": {}},
        ) as dependency_probe,
        patch(
            "core.automation_readiness.current_automation_readiness",
            return_value=automation,
        ) as automation_probe,
        patch("worker.tasks.generate_application_task") as queued_generation,
    ):
        from worker.tasks import score_job_task

        # URL and message ingestion use the default serialized request value.
        score_job_task.apply(args=[job_id])

    check = factory()
    assert check.get(Job, job_id).status == expected_status
    assert check.query(Application).filter(Application.job_id == job_id).count() == 0
    check.close()

    if auto_apply:
        dependency_probe.assert_called_once()
        automation_probe.assert_called_once()
    else:
        dependency_probe.assert_not_called()
        automation_probe.assert_not_called()
    if queues_generation:
        queued_generation.delay.assert_called_once_with(job_id)
    else:
        queued_generation.delay.assert_not_called()
    queued_generation.apply.assert_not_called()
