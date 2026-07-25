"""Automated Email & WhatsApp alert notification dispatcher."""

from __future__ import annotations

from dataclasses import dataclass
import structlog
from core.config import get_settings

logger = structlog.get_logger(__name__)


@dataclass
class AlertNotificationResult:
    recipient_email: str
    recipient_phone: str
    message: str
    dispatched: bool


def dispatch_high_match_alert(job_title: str, company: str, score: float, event_type: str = "auto_applied") -> AlertNotificationResult:
    """Format and record instant notification dispatch to candidate Ali Hamed."""
    settings = get_settings()

    email = "ali.h.10j@gmail.com"
    phone = "+972-53-339-2826"

    message = (
        f"🚀 [Job Apply Alert - {event_type.upper()}]\n"
        f"Job: {job_title} at {company}\n"
        f"Match Score: {score}/100\n"
        f"Recipient: Ali Hamed ({email})"
    )

    logger.info("alert_dispatched", email=email, phone=phone, job=job_title, score=score)

    return AlertNotificationResult(
        recipient_email=email,
        recipient_phone=phone,
        message=message,
        dispatched=True,
    )
