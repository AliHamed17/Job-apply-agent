import asyncio

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.greenhouse import GreenhouseSubmitter
from submitters.lever import LeverSubmitter


def _job(url: str) -> JobData:
    return JobData(title="Engineer", apply_url=url, source_url=url)


def _application() -> GeneratedApplication:
    return GeneratedApplication(cover_letter="CL", recruiter_message="RM", qa_answers={})


def test_lever_falls_back_to_browser_without_api_key(monkeypatch):
    submitter = LeverSubmitter(api_key="")
    called = {"ok": False}

    async def _fake_browser(*_args, **_kwargs):
        called["ok"] = True
        from submitters.base import SubmissionResult

        return SubmissionResult(success=True, platform="lever", status="draft_only")

    monkeypatch.setattr(submitter, "_submit_via_browser", _fake_browser)

    result = asyncio.run(submitter.submit(_job("https://jobs.lever.co/acme/123"), _application(), {}))
    assert called["ok"] is True
    assert result.success is True


def test_greenhouse_falls_back_to_browser_without_api_key(monkeypatch):
    submitter = GreenhouseSubmitter(api_key="")
    called = {"ok": False}

    async def _fake_browser(*_args, **_kwargs):
        called["ok"] = True
        from submitters.base import SubmissionResult

        return SubmissionResult(success=True, platform="greenhouse", status="draft_only")

    monkeypatch.setattr(submitter, "_submit_via_browser", _fake_browser)

    result = asyncio.run(submitter.submit(_job("https://boards.greenhouse.io/acme/jobs/123"), _application(), {}))
    assert called["ok"] is True
    assert result.success is True
