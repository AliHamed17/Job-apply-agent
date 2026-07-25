"""Executive daily/weekly application status tracker and digest generator."""

from __future__ import annotations

from dataclasses import dataclass
import structlog
from db.models import Application, Job, JobStatus

logger = structlog.get_logger(__name__)


@dataclass
class ExecutiveDigest:
    total_jobs_scanned: int
    total_applications: int
    auto_applied_count: int
    needs_review_count: int
    submitted_count: int
    interview_invited_count: int
    conversion_rate_pct: float
    summary_text: str


def generate_executive_digest(db) -> ExecutiveDigest:
    """Generate executive summary stats for notifications and digests."""
    total_jobs = db.query(Job).count()
    total_apps = db.query(Application).count()
    auto_applied = db.query(Application).filter(Application.status == JobStatus.APPROVED).count()
    needs_review = db.query(Application).filter(Application.status == JobStatus.NEEDS_REVIEW).count()
    submitted = db.query(Application).filter(Application.status == JobStatus.SUBMITTED).count()
    interviews = db.query(Application).filter(Application.outcome == "interview_invited").count()

    conv_rate = round((interviews / total_apps * 100), 1) if total_apps > 0 else 0.0

    summary_text = (
        f"Executive Job Apply Digest:\n"
        f"• Total Jobs Scanned: {total_jobs}\n"
        f"• Applications Generated: {total_apps}\n"
        f"• Auto-Submitted: {submitted}\n"
        f"• Needs Review: {needs_review}\n"
        f"• Interview Invitations: {interviews} ({conv_rate}% conversion rate)"
    )

    return ExecutiveDigest(
        total_jobs_scanned=total_jobs,
        total_applications=total_apps,
        auto_applied_count=auto_applied,
        needs_review_count=needs_review,
        submitted_count=submitted,
        interview_invited_count=interviews,
        conversion_rate_pct=conv_rate,
        summary_text=summary_text,
    )
