"""Fail-closed qualification coverage for versioned ATS adapters."""

from __future__ import annotations

import json
import re
from profile.models import UserProfile
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import (
    Application,
    Base,
    Job,
    JobStatus,
    Submission,
    SubmissionStatus,
)
from submitters.platforms import (
    QualificationTier,
    adapter_for_platform,
    adapter_for_url,
    detect_platform,
    registered_adapters,
    supported_platforms,
)

_PLANNED_FIRST_FIVE = {
    "workday",
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
}
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'qualification.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _approved_application(factory, apply_url: str) -> int:
    db = factory()
    job = Job(
        title="Safety Test",
        company="Acme",
        location="",
        employment_type="",
        seniority="",
        description="",
        requirements="",
        apply_url=apply_url,
        source_url=apply_url,
        status=JobStatus.APPROVED,
        score=90.0,
    )
    db.add(job)
    db.flush()
    application = Application(
        job_id=job.id,
        cover_letter="",
        recruiter_message="",
        qa_answers="{}",
        status=JobStatus.APPROVED,
    )
    db.add(application)
    db.commit()
    application_id = application.id
    db.close()
    return application_id


def _live_settings() -> Settings:
    return Settings(
        _env_file=None,
        draft_only=False,
        dry_run=False,
        auto_apply=True,
        cv_routing_path="does-not-exist.yaml",
    )


def test_detection_and_qualification_share_one_adapter_inventory():
    descriptors = registered_adapters()

    assert tuple(descriptor.platform for descriptor in descriptors) == tuple(supported_platforms())
    assert len({descriptor.platform for descriptor in descriptors}) == len(descriptors)

    for descriptor in descriptors:
        url = f"https://{descriptor.domains[0]}/jobs/qualification-test"
        assert detect_platform(url) == descriptor.platform
        assert adapter_for_url(url) is descriptor
        assert adapter_for_platform(descriptor.platform) is descriptor
        assert _SEMVER.fullmatch(descriptor.adapter_version)
        assert descriptor.qualification in {
            QualificationTier.DISABLED,
            QualificationTier.DRY_RUN_ONLY,
        }
        assert descriptor.allows_live_submission is False

    assert {
        descriptor.platform
        for descriptor in descriptors
        if descriptor.qualification is QualificationTier.DRY_RUN_ONLY
    } == _PLANNED_FIRST_FIVE


@pytest.mark.parametrize(
    ("apply_url", "submitter_path"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/1",
            "submitters.greenhouse.GreenhouseSubmitter.submit",
        ),
        (
            "https://nvidia.wd5.myworkdayjobs.com/job/1",
            "submitters.workday.WorkdaySubmitter.submit",
        ),
        (
            "https://www.linkedin.com/jobs/view/1",
            "submitters.linkedin_v2.LinkedInV2Submitter.submit",
        ),
    ],
)
def test_unqualified_live_adapter_never_invokes_external_submit(
    tmp_path,
    apply_url,
    submitter_path,
):
    factory = _factory(tmp_path)
    application_id = _approved_application(factory, apply_url)
    submit = AsyncMock()

    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=_live_settings()),
        patch("worker.tasks._validated_submit_command_available", return_value=True),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch("core.governor.get_governor", return_value=object()),
        patch(submitter_path, new=submit),
    ):
        from worker.tasks import submit_application_task

        submit_application_task.apply(args=[application_id])

    submit.assert_not_awaited()

    db = factory()
    application = db.get(Application, application_id)
    attempt = db.query(Submission).filter(Submission.application_id == application_id).one()
    assert application.status == JobStatus.NEEDS_REVIEW
    assert application.job.status == JobStatus.NEEDS_REVIEW
    assert application.needs_review_reason == "ADAPTER_NOT_QUALIFIED"
    assert attempt.status == SubmissionStatus.FAILED
    assert attempt.reason_code == "ADAPTER_NOT_QUALIFIED"
    assert attempt.submitted_at is None
    assert attempt.submitter_name == detect_platform(apply_url)
    assert json.loads(attempt.diagnostic_details)["terminal_reason"] == ("ADAPTER_NOT_QUALIFIED")
    db.close()
