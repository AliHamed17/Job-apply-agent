"""Fail-closed qualification coverage for versioned ATS adapters."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.ats import list_ats_adapters
from db.models import Application, Base, Job, JobStatus, Submission
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


def test_detection_and_qualification_share_one_adapter_inventory():
    descriptors = registered_adapters()

    assert tuple(descriptor.platform for descriptor in descriptors) == tuple(supported_platforms())
    assert len({descriptor.platform for descriptor in descriptors}) == len(descriptors)

    for descriptor in descriptors:
        url = (
            "https://boards.greenhouse.io/qualification/jobs/1"
            if descriptor.platform == "greenhouse"
            else f"https://{descriptor.domains[0]}/jobs/qualification-test"
        )
        assert detect_platform(url) == descriptor.platform
        assert adapter_for_url(url) is descriptor
        assert adapter_for_platform(descriptor.platform) is descriptor
        assert _SEMVER.fullmatch(descriptor.adapter_version)
        assert descriptor.qualification in {
            QualificationTier.DISABLED,
            QualificationTier.DRY_RUN_ONLY,
            QualificationTier.FIXTURE_QUALIFIED,
        }
        assert descriptor.allows_live_submission is False

    assert {
        descriptor.platform
        for descriptor in descriptors
        if descriptor.qualification is QualificationTier.DRY_RUN_ONLY
    } == _PLANNED_FIRST_FIVE - {"workday", "greenhouse", "lever"}
    assert {
        descriptor.platform
        for descriptor in descriptors
        if descriptor.qualification is QualificationTier.FIXTURE_QUALIFIED
    } == {"workday", "greenhouse", "lever"}


@pytest.mark.asyncio
async def test_ats_inventory_exposes_fixture_only_browser_adapters_as_send_disabled():
    inventory = await list_ats_adapters()
    workday = next(adapter for adapter in inventory if adapter.ats == "workday")
    greenhouse = next(adapter for adapter in inventory if adapter.ats == "greenhouse")

    assert workday.qualification_tier == "fixture_qualified"
    assert workday.final_execution_enabled is False
    assert workday.qualified_form_scope == []
    assert workday.adapter_version == "2.0.3"
    assert workday.selector_version == "workday-candidate-v2.4"
    assert greenhouse.qualification_tier == "fixture_qualified"
    assert greenhouse.final_execution_enabled is False
    assert greenhouse.qualified_form_scope == []
    assert greenhouse.adapter_version == "1.0.0"
    assert greenhouse.selector_version == "greenhouse-candidate-v9"
    assert greenhouse.execution_contract_version == "two-phase-v2"
    descriptor = adapter_for_platform("greenhouse")
    assert descriptor is not None
    assert descriptor.domains == (
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "greenhouse-hosted.com",
    )


@pytest.mark.asyncio
async def test_ats_inventory_exposes_fixture_only_lever_as_send_disabled():
    inventory = await list_ats_adapters()
    lever = next(adapter for adapter in inventory if adapter.ats == "lever")

    assert lever.qualification_tier == "fixture_qualified"
    assert lever.final_execution_enabled is False
    assert lever.qualified_form_scope == []
    assert lever.adapter_version == "1.0.0"
    assert lever.selector_version == "lever-candidate-v2"
    assert lever.transport == "browser"
    assert lever.authentication_mode == "public_candidate_flow"


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
def test_legacy_application_task_never_invokes_external_submit(
    tmp_path,
    apply_url,
    submitter_path,
):
    factory = _factory(tmp_path)
    application_id = _approved_application(factory, apply_url)
    submit = AsyncMock()

    with patch(submitter_path, new=submit):
        from worker.tasks import submit_application_task

        result = submit_application_task.apply(args=[application_id]).get()

    assert result == {
        "state": "blocked",
        "reason_code": "DATABASE_COMMAND_REQUIRED",
    }
    submit.assert_not_awaited()

    db = factory()
    application = db.get(Application, application_id)
    assert application.status == JobStatus.APPROVED
    assert application.job.status == JobStatus.APPROVED
    assert application.needs_review_reason is None
    assert db.query(Submission).filter(Submission.application_id == application_id).count() == 0
    db.close()
