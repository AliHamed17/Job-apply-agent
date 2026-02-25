"""Base submitter interface and registry for job board integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import structlog

from jobs.models import JobData
from llm.generation import GeneratedApplication

logger = structlog.get_logger(__name__)


@dataclass
class SubmissionResult:
    """Result of a submission attempt."""

    success: bool
    platform: str
    status: str  # "submitted", "draft_only", "failed", "captcha_blocked"
    confirmation_id: str | None = None
    confirmation_url: str | None = None
    error: str | None = None


class BaseSubmitter(ABC):
    """Abstract interface for job board submitters."""

    platform_name: str = "base"

    @abstractmethod
    def can_submit(self, job: JobData) -> bool:
        """Check if this submitter can handle the job's apply URL."""
        ...

    @abstractmethod
    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        """Submit an application. Returns SubmissionResult."""
        ...

    def detect_captcha(self, content: str) -> bool:
        """Check for CAPTCHA indicators — never bypass, switch to draft-only."""
        indicators = [
            "captcha", "recaptcha", "hcaptcha", "challenge",
            "verify you are human", "i'm not a robot",
        ]
        content_lower = content.lower()
        return any(ind in content_lower for ind in indicators)


class DraftOnlySubmitter(BaseSubmitter):
    """No-op submitter that records a draft — used as default/fallback."""

    platform_name = "draft_only"

    def can_submit(self, job: JobData) -> bool:
        return True

    async def submit(
        self,
        job: JobData,
        application: GeneratedApplication,
        user_profile: dict,
        resume_path: str | None = None,
    ) -> SubmissionResult:
        logger.info("draft_recorded", job=job.title, company=job.company)
        return SubmissionResult(
            success=True,
            platform="draft_only",
            status="draft_only",
        )


class SubmitterRegistry:
    """Registry of available submitters with fallback to draft-only."""

    def __init__(self):
        self._submitters: list[BaseSubmitter] = []
        self._draft_fallback = DraftOnlySubmitter()

    def register(self, submitter: BaseSubmitter) -> None:
        self._submitters.append(submitter)

    def get_submitter(self, job: JobData, draft_only: bool = True) -> BaseSubmitter:
        """Find the appropriate submitter for a job."""
        if draft_only:
            return self._draft_fallback

        for sub in self._submitters:
            if sub.can_submit(job):
                return sub

        logger.info("no_submitter_found", url=job.apply_url)
        return self._draft_fallback
