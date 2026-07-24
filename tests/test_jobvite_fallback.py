"""Regression: the Jobvite form submitter used to reference an undefined
`resume_path` in _submit_form's browser-fallback paths (CAPTCHA / non-2xx /
exception), so any fallback raised NameError instead of attempting the
browser submit. This pins that _submit_form threads resume_path through to
_submit_via_browser without crashing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import SubmissionResult
from submitters.jobvite import JobviteSubmitter


@pytest.mark.asyncio
async def test_form_submit_falls_back_to_browser_with_resume_path_on_error():
    sub = JobviteSubmitter()
    job = JobData(
        title="Engineer", company="X",
        apply_url="https://jobs.jobvite.com/acme/job/oABC123",
        source_url="https://jobs.jobvite.com/acme/job/oABC123",
    )
    app = GeneratedApplication(cover_letter="hi", recruiter_message="hi", qa_answers={})
    sentinel = SubmissionResult(success=True, platform="jobvite", status="draft_only")

    # Force the try-block to raise so the except -> browser-fallback path runs
    # (this is exactly where the undefined `resume_path` used to blow up).
    with patch("submitters.jobvite.httpx.AsyncClient", side_effect=RuntimeError("boom")), \
         patch.object(sub, "_submit_via_browser", new=AsyncMock(return_value=sentinel)) as browser:
        result = await sub.submit(
            job, app, {"personal": {"name": "Ali Hamed"}}, resume_path="/data/resume.pdf"
        )

    assert result is sentinel
    assert browser.await_count == 1
    # resume_path must be forwarded (positional 4th arg) — the whole point of the fix.
    assert browser.await_args.args[3] == "/data/resume.pdf"
