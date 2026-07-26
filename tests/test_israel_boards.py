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
    assert job.title == "DevOps Lead"
    assert job.company == "JobIL Cloud"
    assert "jobs.co.il" in job.apply_url


def test_drushim_submitter():
    sub = DrushimSubmitter()
    job = JobData(
        title="AI Lead", company="TestComp", apply_url="https://www.drushim.co.il/job/100"
    )
    assert sub.can_submit(job) is True


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
