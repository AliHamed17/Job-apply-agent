import pytest
from unittest.mock import AsyncMock, patch
from core.config import Settings


@pytest.mark.asyncio
async def test_handle_document_rebuilds_profile(tmp_path):
    from api.routes import webhook
    settings = Settings(_env_file=None, user_profile_path=str(tmp_path / "user_profile.yaml"))
    msg = {"type": "document",
           "document": {"id": "MEDIA123", "mime_type": "application/pdf",
                        "filename": "cv.pdf"},
           "from": "15550001111"}

    with patch.object(webhook, "_download_media", new=AsyncMock(return_value=b"%PDF fake")), \
         patch.object(webhook, "build_profile_from_pdf", new=AsyncMock()) as build_mock, \
         patch.object(webhook, "save_profile", return_value=2), \
         patch.object(webhook, "rescore_pending_jobs", return_value=0), \
         patch.object(webhook, "_send_whatsapp_message", new=AsyncMock()) as send_mock:
        handled = await webhook._handle_document(msg, db=None, settings=settings)

    assert handled is True
    assert build_mock.called
    assert send_mock.called
