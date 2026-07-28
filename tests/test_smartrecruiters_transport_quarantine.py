"""The retired shim and protected API can never perform an external action."""

from __future__ import annotations

import inspect

import pytest

from core.submission_domain import ReasonCode
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.smartrecruiters import SmartRecruitersSubmitter
from submitters.smartrecruiters_api import (
    SMARTRECRUITERS_API_CAPABILITY,
    SMARTRECRUITERS_APPLICATION_SCOPE,
    SmartRecruitersApiCapability,
    SmartRecruitersApiDisabledError,
    submit_via_smartrecruiters_api,
)

JOB_URL = "https://jobs.smartrecruiters.com/FixtureCo/123456789-sanitized-role"


@pytest.mark.asyncio
async def test_legacy_shim_is_always_bounded_failure() -> None:
    submitter = SmartRecruitersSubmitter(api_key="must-not-enable-anything")
    job = JobData(title="Fixture role", apply_url=JOB_URL)

    result = await submitter.submit(
        job,
        GeneratedApplication(),
        {},
        resume_path=None,
    )

    assert submitter.can_submit(job)
    assert result.success is False
    assert result.status == "failed"
    assert result.reason_code == ReasonCode.ADAPTER_NOT_QUALIFIED.value
    assert result.error == "SMARTRECRUITERS_LEGACY_TRANSPORT_DISABLED"
    assert "httpx" not in inspect.getsource(type(submitter).submit)
    assert "playwright" not in inspect.getsource(type(submitter).submit)


def test_legacy_parse_helper_returns_public_numeric_id_not_uuid_or_slug() -> None:
    assert SmartRecruitersSubmitter._parse_url(JOB_URL) == (
        "FixtureCo",
        "123456789",
    )
    assert SmartRecruitersSubmitter._parse_url(
        "https://api.smartrecruiters.com/jobs/123456789"
    ) == ("", "")


def test_protected_api_capability_is_separate_disabled_and_scope_bounded() -> None:
    assert SMARTRECRUITERS_API_CAPABILITY == SmartRecruitersApiCapability()
    assert SMARTRECRUITERS_API_CAPABILITY.transport == "protected_application_api"
    assert SMARTRECRUITERS_API_CAPABILITY.authentication_mode == "oauth"
    assert SMARTRECRUITERS_API_CAPABILITY.enabled is False
    assert SMARTRECRUITERS_API_CAPABILITY.qualified_posting_scope == ()
    assert (
        SMARTRECRUITERS_API_CAPABILITY.required_scope
        == SMARTRECRUITERS_APPLICATION_SCOPE
        == "candidate_applications_manage"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes",
    [(), ("jobs_read",), (SMARTRECRUITERS_APPLICATION_SCOPE,)],
)
async def test_api_function_never_sends_even_when_caller_mentions_scope(scopes) -> None:
    with pytest.raises(SmartRecruitersApiDisabledError) as exc_info:
        await submit_via_smartrecruiters_api(
            granted_scopes=scopes,
            posting_uuid="11111111-2222-4333-8444-555555555555",
        )

    assert exc_info.value.reason_code is ReasonCode.ADAPTER_NOT_QUALIFIED


def test_capability_requires_enabled_scope_and_exact_qualified_posting() -> None:
    capability = SmartRecruitersApiCapability(
        enabled=True,
        qualified_posting_scope=("11111111-2222-4333-8444-555555555555",),
    )
    with pytest.raises(SmartRecruitersApiDisabledError):
        capability.require_enabled(
            granted_scopes=(),
            posting_uuid="11111111-2222-4333-8444-555555555555",
        )
    with pytest.raises(SmartRecruitersApiDisabledError):
        capability.require_enabled(
            granted_scopes=(SMARTRECRUITERS_APPLICATION_SCOPE,),
            posting_uuid="99999999-2222-4333-8444-555555555555",
        )
    capability.require_enabled(
        granted_scopes=(SMARTRECRUITERS_APPLICATION_SCOPE,),
        posting_uuid="11111111-2222-4333-8444-555555555555",
    )
