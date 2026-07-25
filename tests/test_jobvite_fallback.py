"""A transport error after Jobvite POST must not trigger a duplicate browser POST."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.jobvite import JobviteSubmitter


@pytest.mark.asyncio
async def test_form_submit_transport_error_is_unknown_without_browser_retry():
    sub = JobviteSubmitter()
    job = JobData(
        title="Engineer",
        company="X",
        apply_url="https://jobs.jobvite.com/acme/job/oABC123",
        source_url="https://jobs.jobvite.com/acme/job/oABC123",
    )
    app = GeneratedApplication(cover_letter="hi", recruiter_message="hi", qa_answers={})
    # The request may have reached Jobvite before the client observed the error.
    # Repeating it in a browser could therefore create a duplicate application.
    with (
        patch("submitters.jobvite.httpx.AsyncClient", side_effect=RuntimeError("boom")),
        patch.object(sub, "_submit_via_browser", new=AsyncMock()) as browser,
    ):
        result = await sub.submit(
            job, app, {"personal": {"name": "Ali Hamed"}}, resume_path="/data/resume.pdf"
        )

    assert result.status == "unknown"
    assert result.reason_code == "SUBMIT_UNCONFIRMED"
    browser.assert_not_awaited()
