"""Truth-derived presentation fields for job and application records."""

from __future__ import annotations

from dataclasses import dataclass

from core.submission_truth import is_employer_verified
from db.models import Job, JobStatus


@dataclass(frozen=True)
class SubmissionDisplay:
    """A presentation status that cannot turn green from a legacy DB status."""

    source_status: str
    display_status: str
    employer_verified: bool


def job_submission_display(job: Job) -> SubmissionDisplay:
    """Derive the display state from the latest attempt's exact employer evidence."""
    source_status = job.status.value if job.status else ""
    application = job.application
    attempt = application.submission if application is not None else None
    verified = is_employer_verified(attempt)

    if verified:
        display_status = JobStatus.SUBMITTED.value
    elif source_status == JobStatus.SUBMITTED.value:
        display_status = "unverified"
    else:
        display_status = source_status

    return SubmissionDisplay(
        source_status=source_status,
        display_status=display_status,
        employer_verified=verified,
    )
