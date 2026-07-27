"""Base submitter interface and registry for job board integrations."""

from __future__ import annotations

import inspect
import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hmac import compare_digest
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import structlog

from core.submission_domain import (
    CommitOutcome,
    FinalSubmitPermit,
    FormPlanV1,
    PreflightOutcome,
    PreparedFinalActionV1,
)
from ingestion.url_utils import normalize_url, url_hash
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    AdapterDescriptor,
    QualificationTier,
    adapter_for_platform,
    adapter_for_url,
)

logger = structlog.get_logger(__name__)


@dataclass
class SubmissionResult:
    """Result of a submission attempt."""

    success: bool
    platform: str
    status: str  # submitted|draft_only|failed|unknown|captcha_blocked
    confirmation_id: str | None = None
    confirmation_url: str | None = None
    error: str | None = None
    reason_code: str | None = None
    diagnostic_details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, repr=False)
class AdapterPreflightContext:
    """Private, ephemeral local inputs for reconstructing browser preflight.

    This object is passed directly from the private worker to an adapter. It is
    never a domain record, API payload, event, diagnostic, or persistence
    model. Its representation is deliberately redacted because it contains a
    normalized employer URL and an absolute local CV path.
    """

    normalized_job_url: str
    selected_cv_id: str
    selected_cv_hash: str
    resume_path: str

    def __post_init__(self) -> None:
        if self.normalized_job_url != normalize_url(self.normalized_job_url):
            raise ValueError("preflight job URL must be normalized")
        if not self.selected_cv_id.strip():
            raise ValueError("preflight selected CV ID is required")
        if re.fullmatch(r"[0-9a-f]{64}", self.selected_cv_hash) is None:
            raise ValueError("preflight selected CV hash must be SHA-256")
        resume = Path(self.resume_path)
        if not resume.is_absolute():
            raise ValueError("preflight CV path must be absolute")

    def __repr__(self) -> str:
        return "AdapterPreflightContext(<private>)"


class BaseSubmitter(ABC):
    """Legacy one-step submitter interface.

    Implementations of this interface are retained only for disabled
    compatibility and dry-run fixture work.  They are never registered in the
    two-phase final-action executor registry.
    """

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
            "captcha",
            "recaptcha",
            "hcaptcha",
            "challenge",
            "verify you are human",
            "i'm not a robot",
        ]
        content_lower = content.lower()
        return any(ind in content_lower for ind in indicators)


@runtime_checkable
class TwoPhaseSubmitter(Protocol):
    """Version-pinned adapter with inspect, reversible preflight, and commit.

    Inspection may observe and plan a form. Preflight may navigate, fill,
    validate, and verify the attachment but cannot perform the final action.
    Commit receives only the resulting opaque action handle plus a one-use
    permit, so no navigation/refill work can occur past the ambiguity boundary.
    """

    descriptor: AdapterDescriptor

    def can_inspect(self, job: JobData) -> bool:
        """Return whether this adapter can safely inspect the candidate form."""
        ...

    async def inspect(
        self,
        *,
        application_id: int,
        application_revision: int,
        job: JobData,
        application: GeneratedApplication,
        user_profile: Mapping[str, Any],
        resume_path: str | None,
        selected_cv_id: str | None = None,
        answer_policy: Any | None = None,
    ) -> FormPlanV1:
        """Observe using an optional request-scoped policy before an attempt exists."""
        ...

    async def preflight(
        self,
        *,
        plan: FormPlanV1,
        permit: FinalSubmitPermit,
        context: AdapterPreflightContext | None = None,
    ) -> PreflightOutcome:
        """Prepare the exact action or return a definitive pre-commit outcome."""
        ...

    async def commit(
        self,
        *,
        action: PreparedFinalActionV1,
        permit: FinalSubmitPermit,
    ) -> CommitOutcome:
        """Perform only the one prepared click/POST and return a typed outcome."""
        ...


def supports_preflight_context(submitter: object) -> bool:
    """Whether one adapter explicitly accepts the private worker context."""

    preflight = getattr(submitter, "preflight", None)
    if not callable(preflight):
        return False
    try:
        parameters = inspect.signature(preflight).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(parameter.name == "context" for parameter in parameters)


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

    def __init__(self) -> None:
        self._submitters: list[BaseSubmitter] = []
        self._draft_fallback = DraftOnlySubmitter()
        self._two_phase: dict[str, TwoPhaseSubmitter] = {}

    def register(self, submitter: BaseSubmitter) -> None:
        """Register a compatibility-only one-step adapter."""
        self._submitters.append(submitter)

    def register_two_phase(self, submitter: TwoPhaseSubmitter) -> None:
        """Register an inspector without implicitly authorizing final action.

        Registration is pinned to the central descriptor inventory.  A class
        merely implementing ``commit`` cannot self-declare versions or scope.
        """
        if not isinstance(submitter, TwoPhaseSubmitter):
            raise TypeError("adapter does not implement the two-phase submitter protocol")

        descriptor = submitter.descriptor
        registered = adapter_for_platform(descriptor.platform)
        if registered is None or not _same_execution_identity(descriptor, registered):
            raise ValueError("adapter descriptor does not match the central registry")
        if descriptor.platform in self._two_phase:
            raise ValueError(f"two-phase adapter already registered for {descriptor.platform}")
        self._two_phase[descriptor.platform] = submitter

    def get_inspector(self, job: JobData) -> TwoPhaseSubmitter | None:
        """Return an ordinary employer inspector only after scoped dry-run qualification.

        Fixture qualification belongs to an explicit offline harness and never
        authorizes the dashboard/API to open an arbitrary employer URL.
        """
        descriptor = adapter_for_url(job.apply_url or job.source_url)
        if (
            descriptor is None
            or descriptor.qualification
            not in {
                QualificationTier.DRY_RUN_QUALIFIED,
                QualificationTier.LIVE_CANARY_QUALIFIED,
            }
            or not descriptor.qualified_form_scope
        ):
            return None
        submitter = self._two_phase.get(descriptor.platform)
        if submitter is None or not _same_execution_identity(submitter.descriptor, descriptor):
            return None
        return submitter if submitter.can_inspect(job) else None

    def get_final_executor(
        self,
        job: JobData,
        *,
        adapter_version: str,
        selector_version: str,
        execution_contract_version: str,
        form_fingerprint: str,
    ) -> TwoPhaseSubmitter | None:
        """Resolve a final executor only for an exact live-qualified identity."""
        descriptor = adapter_for_url(job.apply_url or job.source_url)
        if descriptor is None:
            return None
        if (
            descriptor.adapter_version != adapter_version
            or descriptor.selector_version != selector_version
            or descriptor.execution_contract_version != execution_contract_version
            or execution_contract_version != TWO_PHASE_EXECUTION_CONTRACT_VERSION
            or not descriptor.allows_final_execution
            or not descriptor.qualifies_form_fingerprint(form_fingerprint)
        ):
            return None

        submitter = self._two_phase.get(descriptor.platform)
        if submitter is None or not _same_execution_identity(submitter.descriptor, descriptor):
            return None
        return submitter if submitter.can_inspect(job) else None

    def resolve_final_executor(
        self,
        job: JobData,
        plan: FormPlanV1,
        permit: FinalSubmitPermit,
        execution_contract_version: str,
        at: datetime,
    ) -> TwoPhaseSubmitter | None:
        """Resolve the exact executor for one reviewed, unexpired capability.

        Permit replay is handled transactionally by the command service.  This
        boundary independently rechecks all immutable values visible to the
        adapter and fails closed on malformed time or URL input.
        """
        if at.tzinfo is None or at.utcoffset() is None:
            return None
        try:
            if plan.is_expired(at) or permit.is_expired(at) or not permit.binds(plan):
                return None
            normalized_url = normalize_url(job.apply_url or job.source_url)
        except (TypeError, ValueError):
            return None
        if not compare_digest(permit.job_url_hash, url_hash(normalized_url)):
            return None

        descriptor = adapter_for_url(normalized_url)
        if descriptor is None or descriptor.platform != plan.adapter_name:
            return None
        return self.get_final_executor(
            job,
            adapter_version=plan.adapter_version,
            selector_version=plan.selector_version,
            execution_contract_version=execution_contract_version,
            form_fingerprint=plan.form_fingerprint,
        )

    def get_submitter(self, job: JobData, draft_only: bool = True) -> BaseSubmitter:
        """Find a compatibility submitter; never returns a final executor."""
        if draft_only:
            return self._draft_fallback

        for sub in self._submitters:
            if sub.can_submit(job):
                return sub

        logger.info("no_submitter_found", url=job.apply_url)
        return self._draft_fallback


def _same_execution_identity(
    left: AdapterDescriptor,
    right: AdapterDescriptor,
) -> bool:
    """Compare every descriptor field that can change final-action behavior."""
    return (
        left.platform == right.platform
        and left.adapter_version == right.adapter_version
        and left.selector_version == right.selector_version
        and left.execution_contract_version == right.execution_contract_version
        and left.transport == right.transport
        and left.authentication_mode == right.authentication_mode
        and left.supported_controls == right.supported_controls
        and left.qualification == right.qualification
        and left.qualified_form_scope == right.qualified_form_scope
        and left.domains == right.domains
        and left.allow_subdomains == right.allow_subdomains
    )


# Production and worker code resolve through one authoritative registry.  It is
# intentionally empty: PR2 ships no live two-phase adapter.
two_phase_registry = SubmitterRegistry()
