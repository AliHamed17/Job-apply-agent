"""Tests for WhatsApp interaction metrics exposed by /metrics."""

from fastapi.testclient import TestClient

from api.main import app
from api.routes.webhook import reset_webhook_metrics
from worker.tasks import process_url_task


def _webhook_payload(message_id: str, sender: str, body: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": message_id,
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

    with TestClient(app) as client:
        resp = client.post(
            "/webhook/whatsapp",
            json=_webhook_payload("wamid.metrics.1", "15550001111", "Job https://example.com/job/1"),
        )
        assert resp.status_code == 200

        metrics_resp = client.get("/metrics")
        assert metrics_resp.status_code == 200
        metrics = metrics_resp.json()

    assert metrics["whatsapp_webhook_requests"] >= 1
    assert metrics["whatsapp_messages_received"] >= 1
    assert metrics["whatsapp_urls_extracted"] >= 1
    assert metrics["whatsapp_urls_enqueued"] >= 1


def test_detailed_whatsapp_metrics_include_top_domains(monkeypatch):
    reset_webhook_metrics()
    monkeypatch.setattr(process_url_task, "delay", lambda _job_id: None)

    with TestClient(app) as client:
        resp1 = client.post(
            "/webhook/whatsapp",
            json=_webhook_payload("wamid.metrics.2", "15550001111", "https://example.com/a"),
        )
        resp2 = client.post(
            "/webhook/whatsapp",
            json=_webhook_payload("wamid.metrics.3", "15550001111", "https://example.com/b https://foo.bar/x"),
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200

        detailed = client.get("/api/whatsapp/metrics")
        assert detailed.status_code == 200
        payload = detailed.json()

    assert "counters" in payload
    assert payload["counters"]["urls_enqueued"] >= 3
    domains = {item["domain"]: item["count"] for item in payload["top_url_domains"]}
    assert domains["example.com"] >= 2
    assert domains["foo.bar"] >= 1
