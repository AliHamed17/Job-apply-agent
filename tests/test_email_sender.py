import pytest
from core.config import Settings
from submitters.email_sender import send_cv_email


@pytest.mark.asyncio
async def test_builds_and_sends(tmp_path):
    pdf = tmp_path / "cv.pdf"; pdf.write_bytes(b"%PDF-1.4 x")
    captured = {}
    async def fake_sender(message, host, port, username, password, start_tls):
        captured["to"] = message["To"]; captured["host"] = host
        captured["has_attachment"] = message.is_multipart()
    s = Settings(_env_file=None, smtp_host="smtp.test", smtp_user="u", smtp_password="p",
                 smtp_from_addr="me@test.com")
    ok = await send_cv_email("hr@x.com", "Application: RF Engineer", "Hello",
                             str(pdf), s, sender=fake_sender)
    assert ok is True
    assert captured["to"] == "hr@x.com"
    assert captured["has_attachment"] is True


@pytest.mark.asyncio
async def test_noop_without_smtp_host(tmp_path):
    s = Settings(_env_file=None, smtp_host="")
    ok = await send_cv_email("hr@x.com", "s", "b", None, s, sender=None)
    assert ok is False
