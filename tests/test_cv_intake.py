from __future__ import annotations

from profile.cv_intake import CVIngestError, bytes_to_temp, ingest_cv_from_temp
from profile.models import UserProfile
from unittest.mock import AsyncMock, patch

import pytest

from core.config import Settings


def test_bytes_to_temp_enforces_cap(tmp_path):
    with pytest.raises(CVIngestError, match="TOO_LARGE"):
        bytes_to_temp(b"x" * 11, tmp_path, 10)


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
        patch("profile.cv_intake.save_profile", side_effect=RuntimeError("disk failure")),
    ):
        with pytest.raises(RuntimeError, match="disk failure"):
            await ingest_cv_from_temp(
                temp, settings=settings, db=None, max_bytes=1024
            )
    assert final.read_bytes() == b"previous"
    assert yaml_path.read_bytes() == b"previous-profile"
