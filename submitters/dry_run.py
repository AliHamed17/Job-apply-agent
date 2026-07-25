"""AI Form Pre-Flight Simulator & Dry-Run Inspector."""

from __future__ import annotations

from dataclasses import dataclass, field
import structlog

from jobs.models import JobData
from profile.models import UserProfile
from submitters.base import SubmitterRegistry

logger = structlog.get_logger(__name__)

_registry = SubmitterRegistry()


@dataclass
class DryRunReport:
    application_id: int
    platform_detected: str
    is_ready_for_submission: bool
    mapped_fields_count: int
    unmapped_questions: list[str] = field(default_factory=list)
    qa_generated_answers: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def run_form_preflight(
    app_id: int,
    job: JobData,
    profile: UserProfile,
    qa_answers: dict[str, str] | None = None,
) -> DryRunReport:
    """Perform pre-flight dry-run inspection on application form filling readiness."""
    submitter = _registry.get_submitter(job)
    platform_name = submitter.platform_name


    qa_dict = qa_answers or {}
    warnings: list[str] = []

    mapped_count = 0
    if profile.personal.name:
        mapped_count += 1
    if profile.personal.email:
        mapped_count += 1
    if profile.personal.phone:
        mapped_count += 1
    if profile.resume and profile.resume.text:
        mapped_count += 1

    if not profile.personal.phone:
        warnings.append("Phone number missing from candidate profile")
    if not profile.personal.email:
        warnings.append("Email address missing from candidate profile")

    is_ready = len(warnings) == 0

    return DryRunReport(
        application_id=app_id,
        platform_detected=platform_name,
        is_ready_for_submission=is_ready,
        mapped_fields_count=mapped_count,
        unmapped_questions=[],
        qa_generated_answers=qa_dict,
        warnings=warnings,
    )
