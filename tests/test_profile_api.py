import io
from fastapi.testclient import TestClient
from unittest.mock import patch
from api.main import app
from profile.models import UserProfile

client = TestClient(app)


def _auth():
    from core.config import get_settings
    return {"Authorization": f"Bearer {get_settings().secret_key}"}


def test_upload_resume_builds_profile(tmp_path):
    from core.config import get_settings, Settings

    built = UserProfile(); built.personal.name = "Ali Hamed"
    built.preferences.roles = ["RF Engineer"]

    async def fake_build(path, client=None):
        return built

    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, user_profile_path=str(tmp_path / "user_profile.yaml")
    )
    try:
        with patch("api.routes.profile.build_profile_from_pdf", side_effect=fake_build), \
             patch("api.routes.profile.save_profile", return_value=3) as save_mock, \
             patch("api.routes.profile.rescore_pending_jobs", return_value=0):
            resp = client.post(
                "/api/profile/resume",
                headers=_auth(),
                files={"file": ("cv.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == 3
        assert body["name"] == "Ali Hamed"
        assert save_mock.called
    finally:
        app.dependency_overrides.pop(get_settings, None)
