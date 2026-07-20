from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)


def _auth():
    from core.config import get_settings
    return {"Authorization": f"Bearer {get_settings().secret_key}"}


def test_overview_shape():
    r = client.get("/api/control/overview", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert "governor" in body and "counts" in body and "needs_review" in body
    assert "remaining" in body["governor"]
