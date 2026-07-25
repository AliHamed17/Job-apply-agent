"""Daily digest of applications + outbound activity."""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from celery import shared_task

logger = structlog.get_logger(__name__)


@dataclass
class DigestSummary:
    applied: int = 0
    needs_review: int = 0
    failed: int = 0
    outbound_sent: int = 0


def build_digest(db, day) -> DigestSummary:
    """Build the digest counting each category by *when it happened*, not
    by ``Job.created_at`` (a job created yesterday but submitted today
    must count as today's "applied", not get missed entirely).

    - applied: ``Submission`` rows that succeeded, dated by
      ``Submission.submitted_at`` (the actual submit event).
    - needs_review / failed: ``Application`` rows in that terminal
      status, dated by ``Application.updated_at`` (the row is updated
      exactly when ``worker.tasks.submit_application_task`` transitions
      it into that status — see the drainer-livelock fix).
    - outbound_sent: unchanged, already event-dated via
      ``OutboundContact.last_contacted_at``.
    """
    from sqlalchemy import func  # noqa: PLC0415

    from core.submission_truth import latest_employer_verified_query  # noqa: PLC0415
    from db.models import Application, JobStatus, OutboundContact, Submission  # noqa: PLC0415

    applied = (
        latest_employer_verified_query(db).filter(func.date(Submission.submitted_at) == day).count()
    )

    def _app_count(status):
        return (
            db.query(func.count(Application.id))
            .filter(
                Application.status == status,
                func.date(Application.updated_at) == day,
            )
            .scalar()
            or 0
        )

    outbound = (
        db.query(func.count(OutboundContact.id))
        .filter(func.date(OutboundContact.last_contacted_at) == day)
        .scalar()
        or 0
    )
    return DigestSummary(
        applied=applied,
        needs_review=_app_count(JobStatus.NEEDS_REVIEW),
        failed=_app_count(JobStatus.FAILED),
        outbound_sent=outbound,
    )


def format_digest(s: DigestSummary) -> str:
    return (
        f"📊 *Daily Job Agent Digest*\n"
        f"✅ Applied: {s.applied}\n"
        f"⚠️ Needs review: {s.needs_review}\n"
        f"❌ Failed: {s.failed}\n"
        f"📨 Outbound sent: {s.outbound_sent}"
    )


@shared_task(name="worker.digest.send_daily_digest_task")
def send_daily_digest_task() -> str:
    from datetime import datetime  # noqa: PLC0415

    from api.routes.webhook import _send_whatsapp_message  # noqa: PLC0415
    from core.config import get_settings  # noqa: PLC0415
    from core.utils import run_async  # noqa: PLC0415
    from db.session import get_session_factory  # noqa: PLC0415

    settings = get_settings()
    db = get_session_factory()()
    try:
        summary = build_digest(db, datetime.utcnow().date())
        text = format_digest(summary)
        if settings.allowed_sender_list:
            run_async(_send_whatsapp_message(settings.allowed_sender_list[0], text, settings))
        return text
    finally:
        db.close()
