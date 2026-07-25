"""Israeli board wrappers, submitter routing, and WhatsApp link ingest.

Rewritten from tests that asserted fabricated behavior. The parsers used to
invent a job when a selector missed ("Software Engineer" at "Drushim
Employer"), and DrushimSubmitter used to return status="submitted" with a
made-up confirmation id without opening a browser. Both are now real, so the
tests assert honesty: unparseable input yields None, and the submitter never
claims a submission it cannot evidence.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from discovery.israel_boards import parse_drushim_job, parse_jobs_il_job
from jobs.models import JobData
from submitters.israel_boards import DrushimSubmitter


@pytest.fixture
def client(auth_headers):
    return TestClient(app, headers=auth_headers)


# ── wrappers delegate to the real parser ──────────────────────────────


def test_drushim_parser():
    html = (
        "<html><body><h1>AI Engineer</h1>"
        '<div class="company">Drushim HighTech</div>'
        '<div class="location">Tel Aviv</div>'
        '<div class="description">Python PyTorch RAG</div>'
        "</body></html>"
    )
    job = parse_drushim_job(html, "https://www.drushim.co.il/job/12345")
    assert job is not None
    assert job.title == "AI Engineer"
    assert job.company == "Drushim HighTech"
    assert "drushim.co.il" in job.apply_url


def test_jobs_il_parser():
    html = (
        "<html><body><h1>DevOps Lead</h1>"
        '<div class="company">JobIL Cloud</div>'
        '<div class="city">Haifa</div>'
        '<div class="job-desc">Docker Kubernetes</div>'
        "</body></html>"
    )
    job = parse_jobs_il_job(html, "https://www.jobs.co.il/job/67890")
    assert job is not None
    assert job.title == "DevOps Lead"
    assert job.company == "JobIL Cloud"
    assert "jobs.co.il" in job.apply_url


@pytest.mark.parametrize("parser", [parse_drushim_job, parse_jobs_il_job])
@pytest.mark.parametrize("html", ["", "<html><body><p>nothing here</p></body></html>"])
def test_parsers_return_none_rather_than_inventing_a_job(parser, html):
    """The old versions returned a JobData titled "Software Engineer" at
    "Drushim Employer" for input like this, which would then be scored and
    applied to."""
    assert parser(html, "https://www.drushim.co.il/job/1") is None


def test_description_and_requirements_are_not_the_same_blob():
    """They used to be assigned identical text, which skews CV routing —
    route_cv matches skills against the requirements field."""
    html = (
        "<html><body><h1>מהנדס תוכנה</h1>"
        "<h3>תיאור התפקיד</h3><p>פיתוח מערכות בפייתון.</p>"
        "<h3>דרישות התפקיד</h3><p>ניסיון ב-Kubernetes.</p>"
        "</body></html>"
    )
    job = parse_drushim_job(html, "https://www.drushim.co.il/job/2")
    assert job is not None
    assert job.description != job.requirements
    assert "Kubernetes" in job.requirements


# ── submitter ─────────────────────────────────────────────────────────


def test_drushim_submitter_claims_israeli_boards():
    sub = DrushimSubmitter()
    for url in (
        "https://www.drushim.co.il/job/100",
        "https://www.alljobs.co.il/Search/UploadSingle.aspx?JobID=1",
        "https://www.jobmaster.co.il/jobs/5",
    ):
        assert sub.can_submit(JobData(title="AI Lead", apply_url=url)) is True


def test_drushim_submitter_declines_other_boards():
    sub = DrushimSubmitter()
    job = JobData(title="AI Lead", apply_url="https://boards.greenhouse.io/acme/jobs/1")
    assert sub.can_submit(job) is False


def test_drushim_submitter_declines_when_no_url():
    """can_submit used to read job.platform, which JobData does not define —
    it would have raised AttributeError the moment it was wired in."""
    assert DrushimSubmitter().can_submit(JobData(title="AI Lead")) is False


@pytest.mark.asyncio
async def test_drushim_submitter_never_fakes_a_submission(monkeypatch):
    """With Playwright unavailable it must report draft_only, not success."""
    import builtins

    real_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("playwright not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)

    from llm.generation import GeneratedApplication

    result = await DrushimSubmitter().submit(
        JobData(title="AI Lead", apply_url="https://www.drushim.co.il/job/1"),
        GeneratedApplication(cover_letter="x", recruiter_message="y", qa_answers={}),
        {},
    )
    assert result.status == "draft_only"
    assert result.confirmation_id is None, "must not invent a confirmation id"


# ── WhatsApp link ingest ──────────────────────────────────────────────


def test_whatsapp_link_ingest_endpoint(client, auth_headers):
    resp = client.post(
        "/api/webhook/whatsapp-link",
        json={
            "sender_phone": "+972-53-339-2826",
            "message_text": "Hey Ali check this job https://www.drushim.co.il/job/99999",
            "auto_apply_immediately": True,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["job_id"] > 0
    assert "processed" in data["message"].lower()
