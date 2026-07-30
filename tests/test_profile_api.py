import io
from profile.loader import set_profile
from profile.models import Personal, Preferences, UserProfile
from profile.writer import save_profile
from unittest.mock import patch

import pytest
import yaml
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
        return {
            "version": 3,
            "name": built.personal.name,
            "roles": built.preferences.roles,
            "keywords_count": 0,
            "rescored": 0,
        }

    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, user_profile_path=str(tmp_path / "user_profile.yaml")
    )
    try:
        with (
            patch("api.routes.profile.stream_to_temp", return_value=tmp_path / "x.pdf"),
            patch("api.routes.profile.ingest_cv_from_temp", side_effect=fake_ingest) as ingest_mock,
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


def test_local_onboarding_persists_confirmed_facts_as_new_profile_version(tmp_path):
    from core.config import Settings, get_settings

    profile_path = tmp_path / "user_profile.yaml"
    current = UserProfile(
        personal=Personal(name="Jane Doe", email="jane.doe@example.com"),
        preferences=Preferences(
            roles=["Machine Learning Engineer"],
            locations=["Israel", "Worldwide Remote"],
        ),
    )
    save_profile(current, profile_path, db=None)
    settings = Settings(
        _env_file=None,
        user_profile_path=str(profile_path),
    )
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with patch(
            "api.routes.profile.auto_prepare_scored_jobs_if_ready",
            return_value=2,
        ) as requeue:
            response = client.put(
                "/api/profile/onboarding",
                headers=_auth(),
                json={
                    "legal_name": "Confirmed Candidate",
                    "primary_email": "candidate@domain.test",
                    "phone": "+972 50 000 0000",
                    "location": "Israel",
                    "search_locations": ["Israel", "Worldwide Remote"],
                    "work_authorization": "Confirmed by operator",
                    "sponsorship": "Confirmed by operator",
                    "citizenship": "",
                    "nationality": "Confirmed by operator",
                    "gender": "Prefer not to say",
                },
            )

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["profile_version"] >= 1
        assert body["auto_prepared"] == 2
        requeue.assert_called_once()
        assert body["readiness"]["discovery"]["ready"] is True
        assert body["readiness"]["preparation"]["ready"] is True
        assert body["readiness"]["submission"]["ready"] is True

        stored = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        assert stored["personal"]["name"] == "Confirmed Candidate"
        assert stored["preferences"]["locations"] == ["Israel", "Worldwide Remote"]
        assert "work_authorization" not in stored["personal"]
        assert stored["evidence"]["user_confirmed"] == {
            "gender": "Prefer not to say",
            "nationality": "Confirmed by operator",
            "visa_sponsorship": "Confirmed by operator",
            "work_authorization": "Confirmed by operator",
        }
    finally:
        app.dependency_overrides.pop(get_settings, None)
        set_profile(UserProfile())


def test_local_onboarding_rejects_placeholder_identity_without_writing(tmp_path):
    from core.config import Settings, get_settings

    profile_path = tmp_path / "user_profile.yaml"
    save_profile(
        UserProfile(preferences=Preferences(roles=["Software Engineer"], locations=["Israel"])),
        profile_path,
        db=None,
    )
    original_profile = profile_path.read_text(encoding="utf-8")
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        user_profile_path=str(profile_path),
    )
    try:
        response = client.put(
            "/api/profile/onboarding",
            headers=_auth(),
            json={
                "legal_name": "Jane Doe",
                "primary_email": "jane@example.com",
                "phone": "+972 50 000 0000",
                "location": "Israel",
                "search_locations": ["Israel"],
                "work_authorization": "Confirmed",
                "sponsorship": "Confirmed",
                "nationality": "Confirmed",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "PROFILE_NAME_PLACEHOLDER"
        assert profile_path.read_text(encoding="utf-8") == original_profile
    finally:
        app.dependency_overrides.pop(get_settings, None)
        set_profile(UserProfile())


@pytest.mark.parametrize("phone", ["-------", "()()()()", "+() - ()"])
def test_local_onboarding_rejects_phone_without_enough_digits(
    tmp_path,
    phone,
):
    from core.config import Settings, get_settings

    profile_path = tmp_path / "user_profile.yaml"
    save_profile(
        UserProfile(
            preferences=Preferences(
                roles=["Software Engineer"],
                locations=["Israel"],
            )
        ),
        profile_path,
        db=None,
    )
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        user_profile_path=str(profile_path),
    )
    try:
        response = client.put(
            "/api/profile/onboarding",
            headers=_auth(),
            json={
                "legal_name": "Confirmed Candidate",
                "primary_email": "candidate@domain.test",
                "phone": phone,
                "location": "Israel",
                "search_locations": ["Israel"],
                "work_authorization": "Confirmed",
                "sponsorship": "Confirmed",
                "nationality": "Confirmed",
            },
        )

        assert response.status_code == 422
        assert yaml.safe_load(profile_path.read_text(encoding="utf-8"))["personal"]["name"] == ""
    finally:
        app.dependency_overrides.pop(get_settings, None)
        set_profile(UserProfile())


def test_local_onboarding_rejects_placeholder_search_locations(tmp_path):
    from core.config import Settings, get_settings

    profile_path = tmp_path / "user_profile.yaml"
    save_profile(
        UserProfile(
            preferences=Preferences(
                roles=["Software Engineer"],
                locations=["Your preferred location"],
            )
        ),
        profile_path,
        db=None,
    )
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        user_profile_path=str(profile_path),
    )
    try:
        response = client.put(
            "/api/profile/onboarding",
            headers=_auth(),
            json={
                "legal_name": "Confirmed Candidate",
                "primary_email": "candidate@domain.test",
                "phone": "+972 50 000 0000",
                "location": "Israel",
                "search_locations": ["Your preferred location"],
                "work_authorization": "Confirmed",
                "sponsorship": "Confirmed",
                "nationality": "Confirmed",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "PROFILE_SEARCH_LOCATIONS_PLACEHOLDER"
    finally:
        app.dependency_overrides.pop(get_settings, None)
        set_profile(UserProfile())


def test_local_onboarding_rejects_placeholder_current_location(tmp_path):
    from core.config import Settings, get_settings

    profile_path = tmp_path / "user_profile.yaml"
    save_profile(
        UserProfile(
            preferences=Preferences(
                roles=["Software Engineer"],
                locations=["Israel"],
            )
        ),
        profile_path,
        db=None,
    )
    original_profile = profile_path.read_text(encoding="utf-8")
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        user_profile_path=str(profile_path),
    )
    try:
        response = client.put(
            "/api/profile/onboarding",
            headers=_auth(),
            json={
                "legal_name": "Confirmed Candidate",
                "primary_email": "candidate@domain.test",
                "phone": "+972 50 000 0000",
                "location": "City, Country",
                "search_locations": ["Israel"],
                "work_authorization": "Confirmed",
                "sponsorship": "Confirmed",
                "nationality": "Confirmed",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == ("PROFILE_CURRENT_LOCATION_PLACEHOLDER")
        assert profile_path.read_text(encoding="utf-8") == original_profile
    finally:
        app.dependency_overrides.pop(get_settings, None)
        set_profile(UserProfile())
