import io
from profile.models import UserProfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def _auth():
    from core.config import get_settings
    return {"Authorization": f"Bearer {get_settings().secret_key}"}


def test_upload_resume_builds_profile(tmp_path):
    from core.config import Settings, get_settings

    built = UserProfile()
    built.personal.name = "Ali Hamed"
    built.preferences.roles = ["RF Engineer"]

    async def fake_ingest(tmp, *, settings, db, max_bytes):
        return {"version": 3, "name": built.personal.name, "roles": built.preferences.roles,
                "keywords_count": 0, "rescored": 0}

    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, user_profile_path=str(tmp_path / "user_profile.yaml")
    )
    try:
        with (
            patch("api.routes.profile.stream_to_temp", return_value=tmp_path / "x.pdf"),
            patch(
                "api.routes.profile.ingest_cv_from_temp", side_effect=fake_ingest
            ) as ingest_mock,
        ):
            resp = client.post(
                "/api/profile/resume",
                headers=_auth(),
                files={"file": ("cv.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 3
        assert body["name"] == "Ali Hamed"
        assert ingest_mock.called
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_upload_resume_rejects_wrong_content_type():
    resp = client.post(
        "/api/profile/resume",
        headers=_auth(),
        files={"file": ("cv.txt", io.BytesIO(b"not a pdf"), "text/plain")},
    )
    assert resp.status_code == 422
