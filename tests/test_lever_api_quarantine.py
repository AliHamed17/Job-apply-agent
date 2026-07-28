"""Lever API and legacy transport quarantine tests."""

from __future__ import annotations

import inspect

import pytest

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.lever import LeverSubmitter
from submitters.lever_api import (
    LEVER_API_CAPABILITY,
    LeverApiDisabledError,
    submit_via_lever_api,
)

JOB_URL = "https://jobs.lever.co/sample-company/11111111-2222-4333-8444-555555555555/apply"


@pytest.mark.asyncio
async def test_authorized_api_capability_is_separate_empty_and_disabled() -> None:
    assert LEVER_API_CAPABILITY.transport == "authorized_integration_api"
    assert LEVER_API_CAPABILITY.authentication_mode == "employer_issued_credentials"
    assert LEVER_API_CAPABILITY.enabled is False
    assert LEVER_API_CAPABILITY.qualified_scope == ()

    with pytest.raises(LeverApiDisabledError) as exc_info:
        await submit_via_lever_api(object())

    assert exc_info.value.reason_code.value == "ADAPTER_NOT_QUALIFIED"


@pytest.mark.asyncio
async def test_legacy_shim_returns_bounded_failure_without_network_or_fallback() -> None:
    source = inspect.getsource(LeverSubmitter)
    assert "httpx" not in source
    assert "playwright" not in source
    assert "fallback" not in source.casefold()

    result = await LeverSubmitter(api_key="ignored").submit(
        JobData(title="Fixture", apply_url=JOB_URL),
        GeneratedApplication(),
        {},
        resume_path="C:/private/cv.pdf",
    )

    assert result.success is False
    assert result.status == "failed"
    assert result.reason_code == "ADAPTER_NOT_QUALIFIED"
    assert result.confirmation_id is None
