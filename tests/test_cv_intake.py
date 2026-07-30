from __future__ import annotations

import asyncio
from profile.cv_intake import CVIngestError, _validate_pdf, bytes_to_temp, ingest_cv_from_temp
from profile.loader import load_profile_snapshot, set_profile
from profile.models import Preferences, UserProfile
from profile.writer import save_profile
from unittest.mock import AsyncMock, patch

import pytest
from starlette.responses import Response

from api.routes.profile import OnboardingProfileUpdate, update_onboarding_profile
from core.config import Settings


def test_bytes_to_temp_enforces_cap(tmp_path):
    with pytest.raises(CVIngestError, match="TOO_LARGE"):
        bytes_to_temp(b"x" * 11, tmp_path, 10)


def test_validate_pdf_accepts_current_pypdf(tmp_path):
    from pypdf import PdfWriter

    path = tmp_path / "valid.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as stream:
        writer.write(stream)

    _validate_pdf(path, max_bytes=10_000)


@pytest.mark.asyncio
async def test_ingest_rejects_magic_bytes_only(tmp_path):
    path = bytes_to_temp(b"not-a-pdf", tmp_path, 1024)
    settings = Settings(
        _env_file=None,
        user_profile_path=str(tmp_path / "profile.yaml"),
        application_data_dir=str(tmp_path),
    )
    with pytest.raises(CVIngestError, match="NOT_PDF"):
        await ingest_cv_from_temp(path, settings=settings, db=None, max_bytes=1024)
    assert not path.exists()


@pytest.mark.asyncio
async def test_profile_save_failure_restores_previous_resume(tmp_path):
    temp = bytes_to_temp(b"%PDF-fake", tmp_path, 1024)
    final = tmp_path / "resume.pdf"
    final.write_bytes(b"previous")
    yaml_path = tmp_path / "profile.yaml"
    yaml_path.write_bytes(b"previous-profile")
    settings = Settings(
        _env_file=None,
        user_profile_path=str(yaml_path),
        application_data_dir=str(tmp_path),
    )
    profile = UserProfile()
    with (
        patch("profile.cv_intake._validate_pdf"),
        patch(
            "profile.cv_intake.build_profile_from_pdf",
            new=AsyncMock(return_value=profile),
        ),
        patch("profile.cv_intake.load_profile_snapshot", return_value=UserProfile()),
        patch("profile.cv_intake.save_profile", side_effect=RuntimeError("disk failure")),
    ):
        with pytest.raises(RuntimeError, match="disk failure"):
            await ingest_cv_from_temp(temp, settings=settings, db=None, max_bytes=1024)
    assert final.read_bytes() == b"previous"
    assert yaml_path.read_bytes() == b"previous-profile"


@pytest.mark.asyncio
async def test_cv_ingest_preserves_operator_identity_and_evidence(tmp_path):
    temp = bytes_to_temp(b"%PDF-fake", tmp_path, 1024)
    yaml_path = tmp_path / "profile.yaml"
    yaml_path.write_text("existing: true", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        user_profile_path=str(yaml_path),
        application_data_dir=str(tmp_path),
    )
    existing = UserProfile.model_validate(
        {
            "personal": {
                "name": "Operator Name",
                "email": "operator@example.test",
                "phone": "+15550000000",
                "location": "Operator City",
            },
            "links": {"linkedin": "https://example.test/operator"},
            "preferences": {
                "remote_ok": False,
                "salary": {"min": 100, "max": 200, "currency": "USD"},
                "blacklist_companies": ["Blocked Co"],
            },
            "evidence": {
                "user_confirmed": {
                    "primary_language": "Python",
                    "work_authorization": "operator-only",
                },
                "cv_extracted_by_artifact": {"a" * 64: {"backend_framework": "Django"}},
            },
        }
    )
    rebuilt = UserProfile.model_validate(
        {
            "personal": {
                "name": "CV Name",
                "email": "cv@example.test",
                "phone": "+16660000000",
                "location": "CV City",
            },
            "links": {"linkedin": "https://example.test/cv"},
            "preferences": {
                "roles": ["Backend Engineer"],
                "keywords": ["FastAPI"],
            },
            "evidence": {
                "cv_extracted": {"backend_framework": "FastAPI"},
                "cv_extracted_by_artifact": {"b" * 64: {"backend_framework": "FastAPI"}},
            },
        }
    )
    captured: dict[str, UserProfile] = {}

    def capture_save(profile, _path, db=None):
        del db
        captured["profile"] = profile
        return 2

    with (
        patch("profile.cv_intake._validate_pdf"),
        patch(
            "profile.cv_intake.build_profile_from_pdf",
            new=AsyncMock(return_value=rebuilt),
        ),
        patch("profile.cv_intake.load_profile_snapshot", return_value=existing),
        patch("profile.cv_intake.save_profile", side_effect=capture_save),
    ):
        result = await ingest_cv_from_temp(
            temp,
            settings=settings,
            db=None,
            max_bytes=1024,
        )

    merged = captured["profile"]
    assert result["version"] == 2
    assert merged.personal == existing.personal
    assert merged.links == existing.links
    assert merged.evidence.user_confirmed == existing.evidence.user_confirmed
    assert merged.evidence.facts_for_cv("a" * 64) == {"backend_framework": "Django"}
    assert merged.evidence.facts_for_cv("b" * 64) == {"backend_framework": "FastAPI"}
    assert merged.evidence.cv_extracted == {"backend_framework": "FastAPI"}
    assert merged.preferences.roles == ["Backend Engineer"]
    assert merged.preferences.remote_ok is False
    assert merged.preferences.salary.min == 100
    assert merged.preferences.blacklist_companies == ["Blocked Co"]


@pytest.mark.asyncio
async def test_cv_ingest_merges_onboarding_saved_during_pdf_parse(tmp_path):
    yaml_path = tmp_path / "profile.yaml"
    settings = Settings(
        _env_file=None,
        user_profile_path=str(yaml_path),
        application_data_dir=str(tmp_path),
    )
    save_profile(
        UserProfile(
            preferences=Preferences(
                roles=["Initial Role"],
                locations=["Israel"],
            )
        ),
        yaml_path,
        db=None,
    )
    temp = bytes_to_temp(b"%PDF-fake", tmp_path, 1024)
    parse_started = asyncio.Event()
    finish_parse = asyncio.Event()
    rebuilt = UserProfile(
        preferences=Preferences(
            roles=["Machine Learning Engineer"],
            locations=["Worldwide Remote"],
        )
    )

    async def delayed_build(_path):
        parse_started.set()
        await finish_parse.wait()
        return rebuilt

    try:
        with (
            patch("profile.cv_intake._validate_pdf"),
            patch("profile.cv_intake.build_profile_from_pdf", side_effect=delayed_build),
        ):
            ingest_task = asyncio.create_task(
                ingest_cv_from_temp(
                    temp,
                    settings=settings,
                    db=None,
                    max_bytes=1024,
                )
            )
            await parse_started.wait()
            await update_onboarding_profile(
                OnboardingProfileUpdate(
                    legal_name="Confirmed Candidate",
                    primary_email="candidate@domain.test",
                    phone="+972 50 000 0000",
                    location="Israel",
                    search_locations=["Israel", "Worldwide Remote"],
                    work_authorization="Confirmed",
                    sponsorship="Confirmed",
                    nationality="Confirmed",
                ),
                Response(),
                db=None,
                settings=settings,
            )
            finish_parse.set()
            await ingest_task

        stored = load_profile_snapshot(yaml_path)
        assert stored.personal.name == "Confirmed Candidate"
        assert stored.personal.email == "candidate@domain.test"
        assert stored.evidence.user_confirmed["work_authorization"] == "Confirmed"
        assert stored.preferences.roles == ["Machine Learning Engineer"]
    finally:
        set_profile(UserProfile())
