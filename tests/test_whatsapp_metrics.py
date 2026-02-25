"""Tests for WhatsApp interaction metrics exposed by /metrics."""

from uuid import uuid4

from fastapi.testclient import TestClient

from api.main import app
from api.routes.webhook import reset_webhook_metrics
from worker.tasks import process_url_task


def _webhook_payload(message_id: str, sender: str, body: str) -> dict:
    unique_message_id = f"{message_id}.{uuid4().hex[:8]}"
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": unique_message_id,
                                    "from": sender,
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def test_metrics_include_whatsapp_counters(monkeypatch):
    reset_webhook_metrics()
    monkeypatch.setattr(process_url_task, "delay", lambda _job_id: None)

    uid = uuid4().hex[:8]

    with TestClient(app) as client:
        resp = client.post(
            "/webhook/whatsapp",
            json=_webhook_payload(
                "wamid.metrics.1",
                "15550001111",
                f"Job https://example.com/job/{uid}?ref_id={uid}",
            ),
        )
        assert resp.status_code == 200

        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == 200
        metrics = metrics_resp.json()

    assert metrics["whatsapp_webhook_requests"] >= 1
    assert metrics["whatsapp_messages_received"] >= 1
    assert metrics["whatsapp_urls_extracted"] >= 1
    assert metrics.get("whatsapp_urls_enqueued", 0) >= 1
    assert metrics["whatsapp_likely_job_urls"] >= 1


def test_detailed_whatsapp_metrics_include_top_domains(monkeypatch):
    reset_webhook_metrics()
    monkeypatch.setattr(process_url_task, "delay", lambda _job_id: None)

    uid1 = uuid4().hex[:6]
    uid2 = uuid4().hex[:6]

    with TestClient(app) as client:
        resp1 = client.post(
            "/webhook/whatsapp",
            json=_webhook_payload(
                "wamid.metrics.2",
                "15550001111",
                f"https://example.com/a?job={uid1}",
            ),
        )
        resp2 = client.post(
            "/webhook/whatsapp",
            json=_webhook_payload(
                "wamid.metrics.3",
                "15550001111",
                f"https://example.com/b?job={uid2} https://foo.bar/x?job={uid2}",
            ),
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200

        detailed = client.get("/api/whatsapp/metrics")
        assert detailed.status_code == 200
        payload = detailed.json()

    assert "counters" in payload
    assert payload["counters"].get("urls_enqueued", 0) >= 3
    domains = {item["domain"]: item["count"] for item in payload["top_url_domains"]}
    assert domains["example.com"] >= 2
    assert domains["foo.bar"] >= 1


def test_metrics_track_platform_and_non_job_urls(monkeypatch):
    reset_webhook_metrics()
    monkeypatch.setattr(process_url_task, "delay", lambda _job_id: None)

    uid = uuid4().hex[:6]

    with TestClient(app) as client:
        resp = client.post(
            "/webhook/whatsapp",
            json=_webhook_payload(
                "wamid.metrics.4",
                "15550001111",
                "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/US/123?x="
                f"{uid} https://www.apple.com/iphone/?x={uid}",
            ),
        )
        assert resp.status_code == 200

        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == 200
        metrics = metrics_resp.json()

    assert metrics["whatsapp_platform_workday_urls"] >= 1
    assert metrics["whatsapp_non_job_urls"] >= 1
