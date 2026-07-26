import pytest
from fastapi.testclient import TestClient

from api.main import app
from discovery.israel_boards import parse_drushim_job, parse_jobs_il_job
from jobs.models import JobData
from submitters.israel_boards import DrushimSubmitter


@pytest.fixture
def client(auth_headers):
    return TestClient(app, headers=auth_headers)


def test_drushim_parser():
    html = """
    <html><body><h1>AI Engineer</h1>
    <div class="company">Drushim HighTech</div>
    <div class="location">Tel Aviv</div>
    <div class="description">Python PyTorch RAG</div>
    </body></html>
    """
    job = parse_drushim_job(html, "https://www.drushim.co.il/job/12345")
    assert job is not None
    assert job.title == "AI Engineer"
    assert job.company == "Drushim HighTech"
    assert "drushim.co.il" in job.apply_url


def test_jobs_il_parser():
    html = """
    <html><body><h1>DevOps Lead</h1>
    <div class="company">JobIL Cloud</div>
    <div class="city">Haifa</div>
    <div class="job-desc">Docker Kubernetes</div>
    </body></html>
    """
    job = parse_jobs_il_job(html, "https://www.jobs.co.il/job/67890")
    assert job is not None
    assert job.title == "DevOps Lead"
    assert job.company == "JobIL Cloud"
    assert "jobs.co.il" in job.apply_url


@pytest.mark.parametrize("parser", [parse_drushim_job, parse_jobs_il_job])
@pytest.mark.parametrize("html", ["", "<html><body><p>nothing here</p></body></html>"])
def test_parsers_return_none_rather_than_inventing_a_job(parser, html):
    """The previous versions returned a JobData titled "Software Engineer" at
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


def test_drushim_submitter():
    sub = DrushimSubmitter()
    job = JobData(
        title="AI Lead", company="TestComp", apply_url="https://www.drushim.co.il/job/100"
    )
    assert sub.can_submit(job) is True


def test_drushim_submitter_does_not_crash_on_unrelated_urls():
    """can_submit used to read job.platform, which JobData does not define —
    it raised AttributeError for any job whose URL didn't already match
    drushim/jobs.co.il, crashing the whole submitter cascade for that job."""
    sub = DrushimSubmitter()
    job = JobData(title="AI Lead", apply_url="https://boards.greenhouse.io/acme/jobs/1")
    assert sub.can_submit(job) is False


def test_whatsapp_link_ingest_endpoint(client, monkeypatch):
    from worker.tasks import process_url_task

    queued: list[int] = []
    monkeypatch.setattr(process_url_task, "apply", lambda args: queued.append(args[0]))
    resp = client.post(
        "/api/webhook/whatsapp-link",
        json={
            "sender_phone": "+972-50-000-0000",
            "message_text": "Check this job https://www.drushim.co.il/job/99999",
            "auto_apply_immediately": True,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["url_id"] > 0
    assert data["job_id"] is None
    assert data["status"] == "pending"
    assert queued == [data["url_id"]]
    assert "queued" in data["message"].lower()


def test_whatsapp_link_ingest_rejects_missing_hostname(client):
    response = client.post(
        "/api/webhook/whatsapp-link",
        json={"message_text": "Broken job URL http:///job/123"},
    )
    assert response.status_code == 400
