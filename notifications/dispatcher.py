"""Privacy-safe notification preparation for high-match alerts."""

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


def dispatch_high_match_alert(
    job_title: str,
    company: str,
    score: float,
    event_type: str = "application_prepared",
) -> AlertNotificationResult:
    """Prepare an alert without claiming that an external delivery occurred."""
    settings = get_settings()

    email = settings.notification_recipient_email
    phone = settings.notification_recipient_phone

    message = (
        f"🚀 [Job Apply Alert - {event_type.upper()}]\n"
        f"Job: {job_title} at {company}\n"
        f"Match Score: {score}/100"
    )

    logger.info(
        "alert_prepared",
        email_configured=bool(email),
        phone_configured=bool(phone),
        job=job_title,
        score=score,
    )

    return AlertNotificationResult(
        recipient_email=email,
        recipient_phone=phone,
        message=message,
        dispatched=False,
    )
