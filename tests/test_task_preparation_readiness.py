"""Execution-time preparation readiness regression coverage."""

from profile.models import UserProfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Application, Base, Job, JobStatus, UserProfileVersion
from llm.generation import GeneratedApplication
from match.scoring import Action


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'task-preparation-readiness.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.mark.parametrize(
    (
        "auto_apply",
        "canonical_ready",
        "action",
        "expected_status",
        "queues_generation",
    ),
    [
        (False, True, Action.SKIP, JobStatus.SCORED, False),
        (True, False, Action.SKIP, JobStatus.SCORED, False),
        (True, True, Action.AUTO_APPLY, JobStatus.DRAFT, True),
        (True, True, Action.SKIP, JobStatus.SKIPPED, False),
    ],
)
def test_scoring_rechecks_preparation_readiness_at_execution(
    tmp_path,
    auto_apply,
    canonical_ready,
    action,
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
    db.add_all(
        [
            job,
            UserProfileVersion(
                version=1,
                profile_yaml="{}",
            ),
        ]
    )
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
        patch(
            "profile.loader.load_profile_snapshot",
            return_value=UserProfile(),
        ) as mutable_profile_loader,
        patch(
            "worker.tasks.score_job",
            return_value=SimpleNamespace(total=95.0, skip_reason=None),
        ),
        patch("worker.tasks.decide_action", return_value=action),
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
    mutable_profile_loader.assert_not_called()
    if queues_generation:
        queued_generation.delay.assert_called_once_with(job_id, 1, True)
    else:
        queued_generation.delay.assert_not_called()
    queued_generation.apply.assert_not_called()


def test_scoring_profile_change_keeps_job_eligible_for_rescore(tmp_path):
    factory = _factory(tmp_path)
    db = factory()
    job = Job(
        title="Profile revision race",
        company="Example",
        location="Israel",
        description="Do not bind a stale profile score.",
        requirements="Python",
        source_url="https://example.test/profile-revision-race",
        apply_url="https://example.test/profile-revision-race",
        status=JobStatus.EXTRACTED,
    )
    db.add_all([job, UserProfileVersion(version=1, profile_yaml="{}")])
    db.commit()
    job_id = job.id
    db.close()

    def change_profile_during_scoring(*_args, **_kwargs):
        concurrent = factory()
        concurrent.add(UserProfileVersion(version=2, profile_yaml="{}"))
        concurrent.commit()
        concurrent.close()
        return SimpleNamespace(total=95.0, skip_reason=None)

    settings = Settings(
        _env_file=None,
        auto_apply=True,
        draft_only=True,
        tasks_always_eager=False,
    )
    automation = {
        "preparation_ready": True,
        "stages": {"preparation": {"ready": True, "reason_codes": []}},
    }
    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch("worker.tasks.score_job", side_effect=change_profile_during_scoring),
        patch("worker.tasks.decide_action", return_value=Action.AUTO_APPLY),
        patch(
            "core.operations.readiness_report",
            return_value={"status": "ready", "checks": {}},
        ),
        patch(
            "core.automation_readiness.current_automation_readiness",
            return_value=automation,
        ),
        patch("worker.tasks._dispatch_exact_rescore") as queued_rescore,
        patch("worker.tasks.generate_application_task") as queued_generation,
    ):
        from worker.tasks import score_job_task

        score_job_task.apply(args=[job_id])

    check = factory()
    assert check.get(Job, job_id).status == JobStatus.SCORED
    assert check.query(Application).filter(Application.job_id == job_id).count() == 0
    check.close()
    queued_rescore.assert_called_once_with(job_id, settings)
    queued_generation.delay.assert_not_called()
    queued_generation.apply.assert_not_called()


@pytest.mark.parametrize(
    ("auto_apply", "canonical_ready"),
    [
        (False, True),
        (True, False),
    ],
)
def test_automatic_generation_rechecks_preparation_readiness(
    tmp_path,
    auto_apply,
    canonical_ready,
):
    factory = _factory(tmp_path)
    db = factory()
    job = Job(
        title="Revoked automatic preparation",
        company="Example",
        location="Israel",
        description="Do not generate after automatic preparation is disabled.",
        requirements="Python",
        source_url="https://example.test/revoked-auto-prepare",
        apply_url="https://example.test/revoked-auto-prepare",
        status=JobStatus.DRAFT,
    )
    db.add_all([job, UserProfileVersion(version=1, profile_yaml="{}")])
    db.commit()
    job_id = job.id
    db.close()

    settings = Settings(
        _env_file=None,
        auto_apply=auto_apply,
        draft_only=True,
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
        patch(
            "core.operations.readiness_report",
            return_value={"status": "ready", "checks": {}},
        ) as dependency_probe,
        patch(
            "core.automation_readiness.current_automation_readiness",
            return_value=automation,
        ) as automation_probe,
        patch("llm.generation.generate_full_application") as generated,
    ):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id, 1, True])

    check = factory()
    assert check.get(Job, job_id).status == JobStatus.SCORED
    assert check.query(Application).filter(Application.job_id == job_id).count() == 0
    check.close()
    if auto_apply:
        dependency_probe.assert_called_once()
        automation_probe.assert_called_once()
    else:
        dependency_probe.assert_not_called()
        automation_probe.assert_not_called()
    generated.assert_not_called()


def test_generation_handoff_requeues_job_when_profile_version_changed(tmp_path):
    factory = _factory(tmp_path)
    db = factory()
    job = Job(
        title="Stale generation handoff",
        company="Example",
        location="Israel",
        description="Reject a stale immutable profile binding.",
        requirements="Python",
        source_url="https://example.test/stale-generation-handoff",
        apply_url="https://example.test/stale-generation-handoff",
        status=JobStatus.DRAFT,
    )
    db.add_all(
        [
            job,
            UserProfileVersion(version=1, profile_yaml="{}"),
            UserProfileVersion(version=2, profile_yaml="{}"),
        ]
    )
    db.commit()
    job_id = job.id
    db.close()

    settings = Settings(
        _env_file=None,
        auto_apply=True,
        draft_only=True,
        tasks_always_eager=False,
    )
    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch(
            "worker.tasks._preparation_readiness_at_execution",
            return_value=(True, []),
        ),
        patch("worker.tasks.score_job_task") as queued_rescore,
        patch("llm.generation.generate_full_application") as generated,
    ):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id, 1, True])

    check = factory()
    assert check.get(Job, job_id).status == JobStatus.SCORED
    assert check.query(Application).filter(Application.job_id == job_id).count() == 0
    check.close()
    generated.assert_not_called()
    queued_rescore.delay.assert_called_once_with(job_id, True)
    queued_rescore.apply.assert_not_called()


def test_profile_change_during_generation_requeues_exact_job(tmp_path):
    factory = _factory(tmp_path)
    db = factory()
    job = Job(
        title="Profile changed during generation",
        company="Example",
        location="Israel",
        employment_type="",
        seniority="",
        description="Discard stale generated material.",
        requirements="Python",
        source_url="https://example.test/profile-change-during-generation",
        apply_url="https://example.test/profile-change-during-generation",
        status=JobStatus.DRAFT,
        score=95.0,
    )
    db.add_all([job, UserProfileVersion(version=1, profile_yaml="{}")])
    db.commit()
    job_id = job.id
    db.close()

    async def change_profile(*_args, **_kwargs):
        concurrent = factory()
        concurrent.add(UserProfileVersion(version=2, profile_yaml="{}"))
        concurrent.commit()
        concurrent.close()
        return GeneratedApplication(cover_letter="stale material")

    settings = Settings(
        _env_file=None,
        auto_apply=True,
        draft_only=True,
        tasks_always_eager=False,
        cv_routing_path="does-not-exist.yaml",
    )
    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch(
            "worker.tasks._preparation_readiness_at_execution",
            return_value=(True, []),
        ),
        patch("worker.tasks.score_job_task") as queued_rescore,
        patch(
            "llm.generation.generate_full_application",
            new=AsyncMock(side_effect=change_profile),
        ),
    ):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id, 1, True])

    check = factory()
    assert check.get(Job, job_id).status == JobStatus.SCORED
    assert check.query(Application).filter(Application.job_id == job_id).count() == 0
    check.close()
    queued_rescore.delay.assert_called_once_with(job_id, True)
    queued_rescore.apply.assert_not_called()
