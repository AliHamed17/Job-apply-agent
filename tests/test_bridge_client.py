import pytest
from core.config import Settings
from worker.bridge_client import bridge_send


@pytest.mark.asyncio
async def test_bridge_send_posts(tmp_path):
    pdf = tmp_path / "cv.pdf"; pdf.write_bytes(b"%PDF x")
    sent = {}
    class _Resp:
        status_code = 200
    class _HTTP:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            sent["url"] = url; sent["to"] = json["to"]; sent["has_pdf"] = bool(json.get("pdf_base64"))
            return _Resp()
    s = Settings(_env_file=None, bridge_send_url="http://localhost:8100/send")
    ok = await bridge_send("971500000000", "Hi", str(pdf), s, http=_HTTP())
    assert ok is True
    assert sent["to"] == "971500000000"
    assert sent["has_pdf"] is True
