import asyncio

from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.ashby import AshbySubmitter
from submitters.jobvite import JobviteSubmitter
from submitters.smartrecruiters import SmartRecruitersSubmitter
from submitters.workable import WorkableSubmitter


def _application() -> GeneratedApplication:
    return GeneratedApplication(cover_letter="CL", recruiter_message="RM", qa_answers={})


def test_ashby_falls_back_to_browser_without_api_key(monkeypatch):
    submitter = AshbySubmitter(api_key="")
    called = {"ok": False}

    async def _fake_browser(*_args, **_kwargs):
        called["ok"] = True
        from submitters.base import SubmissionResult

        return SubmissionResult(success=False, platform="ashby", status="requires_human_confirmation")

    monkeypatch.setattr("submitters.ashby.run_browser_form_fill", _fake_browser)
    job = JobData(title="Eng", apply_url="https://jobs.ashbyhq.com/acme/123", source_url="https://jobs.ashbyhq.com/acme/123")
    result = asyncio.run(submitter.submit(job, _application(), {}))
    assert called["ok"] is True
    assert result.status == "requires_human_confirmation"


def test_workable_falls_back_to_browser_on_401(monkeypatch):
    submitter = WorkableSubmitter(api_key="k")

    class _Resp:
        status_code = 401
        text = "Unauthorized"

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return _Resp()

    monkeypatch.setattr("submitters.workable.httpx.AsyncClient", lambda **_kwargs: _Client())

    called = {"ok": False}

    async def _fake_browser(*_args, **_kwargs):
        called["ok"] = True
        from submitters.base import SubmissionResult

        return SubmissionResult(success=False, platform="workable", status="requires_human_confirmation")

    monkeypatch.setattr("submitters.workable.run_browser_form_fill", _fake_browser)

    job = JobData(title="Eng", apply_url="https://company.workable.com/j/1", source_url="https://company.workable.com/j/1")
    result = asyncio.run(submitter.submit(job, _application(), {}))
    assert called["ok"] is True
    assert result.status == "requires_human_confirmation"


def test_smartrecruiters_falls_back_to_browser_on_401(monkeypatch):
    submitter = SmartRecruitersSubmitter(api_key="k")

    class _Resp:
        status_code = 401
        text = "Unauthorized"

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return _Resp()

    monkeypatch.setattr("submitters.smartrecruiters.httpx.AsyncClient", lambda **_kwargs: _Client())

    called = {"ok": False}

    async def _fake_browser(*_args, **_kwargs):
        called["ok"] = True
        from submitters.base import SubmissionResult

        return SubmissionResult(success=False, platform="smartrecruiters", status="requires_human_confirmation")

    monkeypatch.setattr("submitters.smartrecruiters.run_browser_form_fill", _fake_browser)

    job = JobData(title="Eng", apply_url="https://jobs.smartrecruiters.com/acme/1", source_url="https://jobs.smartrecruiters.com/acme/1")
    result = asyncio.run(submitter.submit(job, _application(), {}))
    assert called["ok"] is True
    assert result.status == "requires_human_confirmation"


def test_jobvite_falls_back_to_browser_on_captcha(monkeypatch):
    submitter = JobviteSubmitter(api_key="k")

    class _Resp:
        status_code = 403
        text = "captcha challenge"

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return _Resp()

    monkeypatch.setattr("submitters.jobvite.httpx.AsyncClient", lambda **_kwargs: _Client())

    called = {"ok": False}

    async def _fake_browser(*_args, **_kwargs):
        called["ok"] = True
        from submitters.base import SubmissionResult

        return SubmissionResult(success=False, platform="jobvite", status="requires_human_confirmation")

    monkeypatch.setattr("submitters.jobvite.run_browser_form_fill", _fake_browser)

    job = JobData(title="Eng", apply_url="https://jobs.jobvite.com/acme/job/1", source_url="https://jobs.jobvite.com/acme/job/1")
    result = asyncio.run(submitter.submit(job, _application(), {}))
    assert called["ok"] is True
    assert result.status == "requires_human_confirmation"
