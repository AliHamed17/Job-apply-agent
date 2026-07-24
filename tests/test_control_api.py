from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)


def _auth():
    from core.config import get_settings
    return {"Authorization": f"Bearer {get_settings().secret_key}"}


def test_kill_and_status_roundtrip():
    assert client.post("/api/control/kill", headers=_auth()).status_code == 200
    assert client.get("/api/control/status", headers=_auth()).json()["killed"] is True
    assert client.post("/api/control/resume", headers=_auth()).status_code == 200
    assert client.get("/api/control/status", headers=_auth()).json()["killed"] is False
