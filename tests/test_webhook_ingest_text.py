"""TestClient coverage for the outbound-routing wiring in api/routes/webhook.py.

Verifies that /api/ingest-text and the no-URL branch of the WhatsApp
receive_message webhook both actually reach worker.outbound.process_text_post
(via webhook._route_text_post), and that a URL-bearing message takes the
existing URL-extraction branch instead. No real bridge/email/LLM/network —
_route_text_post and process_url_task are patched, and the DB dependency is
overridden to an in-memory SQLite session.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api.main import app
from api.routes import webhook
from core.config import get_settings
from db.models import Base
from db.session import get_db

client = TestClient(app)


def _override_get_db():
    # StaticPool keeps one shared connection alive for the engine's lifetime —
    # FastAPI runs sync dependencies in a threadpool, so without it the
    # request's queries would land on a different :memory: connection (and
    # thus a different, table-less database) than the one create_all() used.
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        yield db
    finally:
        db.close()


def _auth():
    return {"Authorization": f"Bearer {get_settings().secret_key}"}


def test_ingest_text_invokes_outbound_routing():
    app.dependency_overrides[get_db] = _override_get_db
    calls = {}

    async def fake_route(text, db, settings, sender=None):
        calls["text"] = text
        calls["sender"] = sender
        return "sent_whatsapp"

    try:
        with patch.object(webhook, "_route_text_post", new=fake_route):
            resp = client.post(
                "/api/ingest-text",
                headers=_auth(),
                json={
                    "text": "Hiring RF Engineer, interested? DM me",
                    "sender": "+972500000123",
                },
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "result": "sent_whatsapp"}
        assert calls["text"] == "Hiring RF Engineer, interested? DM me"
        # The bridge-supplied sender must survive Pydantic and reach routing —
        # it's the only usable contact for "DM me" posts.
        assert calls["sender"] == "+972500000123"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_receive_message_routes_no_url_job_text_but_not_url_text():
    """A no-URL 'hiring' broadcast reaches _route_text_post; a URL-bearing
    text takes the existing extract-and-enqueue branch instead."""
    app.dependency_overrides[get_db] = _override_get_db

    def _payload(text, msg_id, sender="15550009999"):
        return {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": msg_id,
                                        "from": sender,
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }

    try:
        with (
            patch.object(
                webhook,
                "_route_text_post",
                new=AsyncMock(return_value="sent_whatsapp"),
            ) as route_mock,
            patch.object(
                webhook,
                "dispatch_url_processing",
                new=MagicMock(),
            ) as dispatch_mock,
        ):
            # URL-bearing text -> URL extraction branch, no outbound routing.
            resp1 = client.post(
                "/webhook/whatsapp",
                json=_payload("Check this job https://example.com/jobs/123", "wamid.url1"),
            )
            assert resp1.status_code == 200
            assert route_mock.await_count == 0
            assert dispatch_mock.call_count == 1

            # No-URL "hiring" text -> outbound routing branch.
            resp2 = client.post(
                "/webhook/whatsapp",
                json=_payload("Hiring RF Engineer, WhatsApp +971500000000", "wamid.job1"),
            )
            assert resp2.status_code == 200
            assert route_mock.await_count == 1
            assert route_mock.await_args.args[0] == "Hiring RF Engineer, WhatsApp +971500000000"
    finally:
        app.dependency_overrides.pop(get_db, None)
