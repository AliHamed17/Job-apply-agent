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
        with (
            patch(
                "api.routes.profile.enqueue_pending_job_rescore",
                return_value=3,
            ) as rescore,
            patch(
                "api.routes.profile.auto_prepare_scored_jobs_if_ready",
                return_value=2,
            ) as requeue,
        ):
            response = client.put(
                "/api/profile/onboarding",
                headers=_auth(),
                json={
                    "legal_name": "Confirmed Candidate",
                    "primary_email": "candidate@domain.test",
                    "phone": "+972 50 000 0000",
                    "location": "Israel",
                    "search_locations": ["Tel Aviv, Israel", "Worldwide Remote"],
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
        assert body["rescored"] == 0
        assert body["rescore_queued"] == 3
        assert body["auto_prepared"] == 2
        rescore.assert_called_once()
        assert rescore.call_args.kwargs["expected_profile_version"] == body["profile_version"]
        requeue.assert_called_once()
        assert body["readiness"]["discovery"]["ready"] is True
        assert body["readiness"]["preparation"]["ready"] is True
        assert body["readiness"]["submission"]["ready"] is True

        stored = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        assert stored["personal"]["name"] == "Confirmed Candidate"
        assert stored["preferences"]["locations"] == [
            "Tel Aviv, Israel",
            "Worldwide Remote",
        ]
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


@pytest.mark.parametrize(
    "email",
    [
        "candidate@example..com",
        ".candidate@domain.test",
        "candidate@-domain.test",
        "candidate domain.test",
    ],
)
def test_local_onboarding_rejects_malformed_email(email):
    response = client.put(
        "/api/profile/onboarding",
        headers=_auth(),
        json={
            "legal_name": "Confirmed Candidate",
            "primary_email": email,
            "phone": "+972 50 000 0000",
            "location": "Israel",
            "search_locations": ["Israel"],
            "work_authorization": "Confirmed",
            "sponsorship": "Confirmed",
            "nationality": "Confirmed",
        },
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "phone",
    ["-------", "()()()()", "+() - ()", "+10000000000"],
)
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


# ── Answer bank: jurisdiction-scoped legal facts + recurring facts ──────────


def _onboarding_base() -> dict:
    return {
        "legal_name": "Confirmed Candidate",
        "primary_email": "candidate@domain.test",
        "phone": "+972 50 000 0000",
        "location": "Israel",
        "search_locations": ["Tel Aviv, Israel"],
        "work_authorization": "Confirmed by operator",
        "sponsorship": "Confirmed by operator",
        "citizenship": "",
        "nationality": "Confirmed by operator",
    }


def _seeded_settings(tmp_path):
    from core.config import Settings

    profile_path = tmp_path / "user_profile.yaml"
    save_profile(
        UserProfile(
            personal=Personal(name="Jane Doe", email="jane.doe@example.com"),
            preferences=Preferences(roles=["Software Engineer"], locations=["Israel"]),
        ),
        profile_path,
        db=None,
    )
    return profile_path, Settings(_env_file=None, user_profile_path=str(profile_path))


def _put_onboarding(payload):
    with (
        patch("api.routes.profile.enqueue_pending_job_rescore", return_value=0),
        patch("api.routes.profile.auto_prepare_scored_jobs_if_ready", return_value=0),
    ):
        return client.put("/api/profile/onboarding", headers=_auth(), json=payload)


def test_onboarding_stores_jurisdiction_scoped_legal_facts(tmp_path):
    """An Israeli authorisation fact must never be able to answer a US question.

    The flat work_authorization key is the jurisdiction-*unspecified* answer, so
    a resolver matching a label that names a country needs the suffixed key. A
    jurisdiction with no confirmed fact must be absent, not stored as "", so it
    abstains rather than resolving to an empty answer.
    """
    from core.config import get_settings

    profile_path, settings = _seeded_settings(tmp_path)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        payload = _onboarding_base() | {
            "work_authorization_il": "Israeli citizen, no sponsorship required",
            "sponsorship_il": "No",
        }
        assert _put_onboarding(payload).status_code == 200

        stored = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        confirmed = stored["evidence"]["user_confirmed"]
        assert confirmed["work_authorization_il"] == "Israeli citizen, no sponsorship required"
        assert confirmed["visa_sponsorship_il"] == "No"
        # Never confirmed for the US, so the key must not exist at all.
        assert "work_authorization_us" not in confirmed
        assert "visa_sponsorship_us" not in confirmed
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_onboarding_derives_both_years_experience_shapes(tmp_path):
    """Free-text fields want '2 years'; NUMBER controls want '2'."""
    from core.config import get_settings

    profile_path, settings = _seeded_settings(tmp_path)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        assert _put_onboarding(_onboarding_base() | {"years_experience": "2"}).status_code == 200
        confirmed = yaml.safe_load(profile_path.read_text(encoding="utf-8"))["evidence"][
            "user_confirmed"
        ]
        assert confirmed["years_experience"] == "2 years"
        assert confirmed["years_experience_number"] == "2"
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_onboarding_stores_recurring_facts_and_removes_blanks(tmp_path):
    from core.config import get_settings

    profile_path, settings = _seeded_settings(tmp_path)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        filled = _onboarding_base() | {
            "notice_period": "30 days",
            "salary_expectation": "35000",
            "salary_currency": "ILS",
            "relocation": "No",
            "work_mode": "Hybrid",
            "highest_degree": "M.Sc. Information Systems",
            "demographic_disclosure": "decline",
        }
        assert _put_onboarding(filled).status_code == 200
        confirmed = yaml.safe_load(profile_path.read_text(encoding="utf-8"))["evidence"][
            "user_confirmed"
        ]
        assert confirmed["notice_period"] == "30 days"
        assert confirmed["salary_currency"] == "ILS"
        assert confirmed["demographic_disclosure"] == "decline"
        assert "how_did_you_hear" not in confirmed

        # Re-submitting with a blank must remove the fact, not store "".
        assert _put_onboarding(filled | {"notice_period": ""}).status_code == 200
        confirmed = yaml.safe_load(profile_path.read_text(encoding="utf-8"))["evidence"][
            "user_confirmed"
        ]
        assert "notice_period" not in confirmed
    finally:
        app.dependency_overrides.pop(get_settings, None)


def test_onboarding_rejects_unknown_field():
    resp = _put_onboarding(_onboarding_base() | {"not_a_real_field": "x"})
    assert resp.status_code == 422


def test_onboarding_rejects_invalid_demographic_disclosure():
    resp = _put_onboarding(_onboarding_base() | {"demographic_disclosure": "male"})
    assert resp.status_code == 422


def test_onboarding_rejects_non_numeric_years_experience():
    resp = _put_onboarding(_onboarding_base() | {"years_experience": "about two"})
    assert resp.status_code == 422
