from unittest.mock import AsyncMock, patch

import pytest

from core.config import Settings


@pytest.mark.asyncio
async def test_handle_document_rebuilds_profile(tmp_path):
    from api.routes import webhook
    settings = Settings(_env_file=None, user_profile_path=str(tmp_path / "user_profile.yaml"))
    msg = {"type": "document",
           "document": {"id": "MEDIA123", "mime_type": "application/pdf",
                        "filename": "cv.pdf"},
           "from": "15550001111"}

    async def fake_ingest(tmp, *, settings, db, max_bytes):
        return {"version": 2, "roles": [], "rescored": 0}

    with (
        patch.object(webhook, "_download_media", new=AsyncMock(return_value=b"%PDF fake")),
        patch("profile.cv_intake.bytes_to_temp", return_value=tmp_path / "x.pdf"),
        patch(
            "profile.cv_intake.ingest_cv_from_temp", side_effect=fake_ingest
        ) as ingest_mock,
        patch.object(webhook, "_send_whatsapp_message", new=AsyncMock()) as send_mock,
    ):
        handled = await webhook._handle_document(msg, db=None, settings=settings)

    assert handled is True
    assert ingest_mock.called
    assert send_mock.called
