"""Send the CV by email via SMTP."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


async def _default_sender(message, host, port, username, password, start_tls):
    import aiosmtplib  # noqa: PLC0415
    await aiosmtplib.send(message, hostname=host, port=port, username=username or None,
                          password=password or None, start_tls=start_tls)


async def send_cv_email(to_addr, subject, body, pdf_path, settings, sender=None) -> bool:
    if not settings.smtp_host:
        logger.info("smtp_not_configured")
        return False
    msg = EmailMessage()
    msg["From"] = settings.smtp_from_addr or settings.smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    if pdf_path and Path(pdf_path).exists():
        msg.add_attachment(Path(pdf_path).read_bytes(), maintype="application",
                           subtype="pdf", filename="CV.pdf")
    send = sender or _default_sender
    try:
        await send(msg, settings.smtp_host, settings.smtp_port,
                   settings.smtp_user, settings.smtp_password, True)
        logger.info("cv_email_sent", to=to_addr)
        return True
    except Exception as exc:
        logger.error("cv_email_failed", to=to_addr, error=str(exc))
        return False
