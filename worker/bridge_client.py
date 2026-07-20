"""HTTP client to the WhatsApp bridge send endpoint."""

from __future__ import annotations

import base64
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


async def bridge_send(to: str, text: str, pdf_path: str | None, settings, http=None) -> bool:
    payload = {"to": to, "text": text}
    if pdf_path and Path(pdf_path).exists():
        payload["pdf_base64"] = base64.b64encode(Path(pdf_path).read_bytes()).decode()
    headers = {"Authorization": f"Bearer {settings.secret_key}"}
    try:
        if http is None:
            import httpx  # noqa: PLC0415
            http = httpx.AsyncClient(timeout=30.0)
        async with http as client:
            resp = await client.post(settings.bridge_send_url, json=payload, headers=headers)
            ok = 200 <= resp.status_code < 300
            logger.info("bridge_send", to=to, ok=ok)
            return ok
    except Exception as exc:
        logger.error("bridge_send_failed", to=to, error=str(exc))
        return False
