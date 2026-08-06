"""Versioned ATS registry shared by detection and submission routing.

An adapter existing in the codebase is not evidence that it is safe for live
use. This immutable registry defines code identity and the committed fixture
baseline only. Runtime dry-run and live-canary scope comes from strict local
database authority and is injected into a fresh request-scoped registry. URL
detection and effective qualification still bind to this exact code identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from urllib.parse import urlparse

from submitters.ashby_identity import is_ashby_candidate_url
from submitters.greenhouse_identity import is_greenhouse_candidate_url
from submitters.smartrecruiters_identity import is_smartrecruiters_candidate_url

TWO_PHASE_EXECUTION_CONTRACT_VERSION = "two-phase-v2"


class QualificationTier(StrEnum):
    """Evidence level reached by one exact adapter version."""

    DISABLED = "disabled"
    DRY_RUN_ONLY = "dry_run_only"
    FIXTURE_QUALIFIED = "fixture_qualified"
    DRY_RUN_QUALIFIED = "dry_run_qualified"
    LIVE_CANARY_QUALIFIED = "live_canary_qualified"


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    """Immutable identity and qualification policy for an ATS adapter."""

    platform: str
    adapter_version: str
    selector_version: str
    transport: str
    authentication_mode: str
    supported_controls: tuple[str, ...]
    qualification: QualificationTier
    qualified_form_scope: tuple[str, ...]
    domains: tuple[str, ...]
    execution_contract_version: str | None = None
    allow_subdomains: bool = True

    @property
    def allows_live_submission(self) -> bool:
        """Only a completed live canary qualifies an external final action."""
        return self.qualification is QualificationTier.LIVE_CANARY_QUALIFIED

    @property
    def allows_final_execution(self) -> bool:
        """Require the two-phase contract and an exact qualified form scope.

        ``allows_live_submission`` remains the qualification-tier signal used
        by legacy compatibility paths.  This stronger property is the only
        gate the final-action executor registry may use.
        """
        return (
            self.allows_live_submission
            and self.execution_contract_version == TWO_PHASE_EXECUTION_CONTRACT_VERSION
            and bool(self.qualified_form_scope)
        )

    def qualifies_form_fingerprint(self, form_fingerprint: str) -> bool:
        """Return whether one exact observed form was live-canary qualified."""
        normalized = (form_fingerprint or "").strip()
        return bool(normalized) and normalized in self.qualified_form_scope


_COMMON_CONTROLS = ("text", "textarea", "select", "radio", "checkbox", "file")
_GREENHOUSE_CONTROLS = (
    "text",
    "textarea",
    "select",
    "multi_select",
    "radio",
    "checkbox",
    "date",
    "number",
    "email",
    "phone",
    "url",
    "file",
    "consent",
    "attestation",
)
_SMARTRECRUITERS_CONTROLS = _GREENHOUSE_CONTROLS

# PR1 deliberately qualifies no adapter for live use.  The first five planned
# ATS families remain available for safe inspection/dry-run work; legacy
# adapters are disabled until they receive their own fixture and canary program.
_ADAPTERS: tuple[AdapterDescriptor, ...] = (
    AdapterDescriptor(
        platform="workday",
        adapter_version="2.0.3",
        selector_version="workday-candidate-v2.4",
        transport="browser",
        authentication_mode="persistent_profile",
        supported_controls=_COMMON_CONTROLS,
        qualification=QualificationTier.FIXTURE_QUALIFIED,
        qualified_form_scope=(),
        domains=("myworkdayjobs.com", "myworkday.com", "workday.com"),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    ),
    AdapterDescriptor(
        platform="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-candidate-v9",
        transport="browser",
        authentication_mode="public_candidate_flow",
        supported_controls=_GREENHOUSE_CONTROLS,
        qualification=QualificationTier.FIXTURE_QUALIFIED,
        qualified_form_scope=(),
        domains=(
            "boards.greenhouse.io",
            "job-boards.greenhouse.io",
            "greenhouse-hosted.com",
        ),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    ),
    AdapterDescriptor(
        platform="lever",
        adapter_version="1.0.0",
        # v3 replaces v2, which never matched real markup (see
        # submitters/lever_v1.py). Deliberately NOT re-claiming
        # FIXTURE_QUALIFIED here: that tier means the committed fixture
        # baseline passes, and only one new fixture (built from a real,
        # completed submission) exists so far -- the other 24 v2 fixtures
        # still encode the old, wrong markup and have not been migrated.
        # DRY_RUN_ONLY is the honest claim: real evidence backs the code,
        # a comprehensive fixture baseline does not yet.
        selector_version="lever-candidate-v3",
        transport="browser",
        authentication_mode="public_candidate_flow",
        supported_controls=_COMMON_CONTROLS,
        qualification=QualificationTier.DRY_RUN_ONLY,
        qualified_form_scope=(),
        domains=("jobs.lever.co", "jobs.eu.lever.co"),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
        allow_subdomains=False,
    ),
    AdapterDescriptor(
        platform="ashby",
        adapter_version="1.0.0",
        selector_version="ashby-candidate-v1",
        transport="browser",
        authentication_mode="public_candidate_flow",
        supported_controls=(
            "text",
            "textarea",
            "select",
            "multi_select",
            "radio",
            "checkbox",
            "date",
            "number",
            "email",
            "phone",
            "url",
            "file",
            "consent",
            "attestation",
        ),
        qualification=QualificationTier.FIXTURE_QUALIFIED,
        qualified_form_scope=(),
        domains=("jobs.ashbyhq.com",),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    ),
    AdapterDescriptor(
        platform="workable",
        adapter_version="0.1.0",
        selector_version="legacy-v1",
        transport="legacy_hybrid",
        authentication_mode="public_candidate_flow",
        supported_controls=_COMMON_CONTROLS,
        qualification=QualificationTier.DISABLED,
        qualified_form_scope=(),
        domains=("apply.workable.com", "workable.com"),
    ),
    AdapterDescriptor(
        platform="smartrecruiters",
        adapter_version="1.0.0",
        selector_version="smartrecruiters-candidate-v1",
        transport="browser",
        authentication_mode="public_candidate_flow",
        supported_controls=_SMARTRECRUITERS_CONTROLS,
        qualification=QualificationTier.FIXTURE_QUALIFIED,
        qualified_form_scope=(),
        domains=("jobs.smartrecruiters.com",),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
        allow_subdomains=False,
    ),
    AdapterDescriptor(
        platform="jobvite",
        adapter_version="0.1.0",
        selector_version="legacy-v1",
        transport="legacy_hybrid",
        authentication_mode="public_candidate_flow",
        supported_controls=_COMMON_CONTROLS,
        qualification=QualificationTier.DISABLED,
        qualified_form_scope=(),
        domains=("jobs.jobvite.com", "jobvite.com"),
    ),
    AdapterDescriptor(
        platform="icims",
        adapter_version="0.1.0",
        selector_version="legacy-v1",
        transport="browser",
        authentication_mode="public_candidate_flow",
        supported_controls=_COMMON_CONTROLS,
        qualification=QualificationTier.DISABLED,
        qualified_form_scope=(),
        domains=("icims.com",),
    ),
    AdapterDescriptor(
        platform="comeet",
        adapter_version="0.1.0",
        selector_version="legacy-v1",
        transport="browser",
        authentication_mode="public_candidate_flow",
        supported_controls=_COMMON_CONTROLS,
        qualification=QualificationTier.DISABLED,
        qualified_form_scope=(),
        domains=("comeet.com", "comeet.co"),
    ),
    AdapterDescriptor(
        platform="linkedin",
        adapter_version="2.0.0",
        selector_version="linkedin-easy-apply-v1",
        transport="browser",
        authentication_mode="persistent_profile",
        supported_controls=_COMMON_CONTROLS,
        qualification=QualificationTier.DISABLED,
        qualified_form_scope=(),
        domains=("linkedin.com",),
    ),
    AdapterDescriptor(
        platform="indeed",
        adapter_version="0.1.0",
        selector_version="legacy-v1",
        transport="browser",
        authentication_mode="persistent_profile",
        supported_controls=_COMMON_CONTROLS,
        qualification=QualificationTier.DISABLED,
        qualified_form_scope=(),
        domains=("indeed.com",),
    ),
)

_ADAPTERS_BY_PLATFORM: Mapping[str, AdapterDescriptor] = MappingProxyType(
    {descriptor.platform: descriptor for descriptor in _ADAPTERS}
)


def _matches_domain(hostname: str, domain: str, *, allow_subdomains: bool) -> bool:
    return hostname == domain or (allow_subdomains and hostname.endswith(f".{domain}"))


def detect_platform(url: str) -> str:
    """Return a stable platform name without performing network requests."""
    candidate_url = (url or "").strip()
    if is_greenhouse_candidate_url(candidate_url):
        return "greenhouse"
    if is_ashby_candidate_url(candidate_url):
        return "ashby"
    if is_smartrecruiters_candidate_url(candidate_url):
        return "smartrecruiters"
    try:
        hostname = (urlparse(candidate_url).hostname or "").lower().rstrip(".")
    except ValueError:
        return "unknown"
    for descriptor in _ADAPTERS:
        if descriptor.platform in {"greenhouse", "ashby", "smartrecruiters"}:
            continue
        if any(
            _matches_domain(
                hostname,
                domain,
                allow_subdomains=descriptor.allow_subdomains,
            )
            for domain in descriptor.domains
        ):
            return descriptor.platform
    return "generic_portal" if hostname else "unknown"


def supported_platforms() -> list[str]:
    """Return platforms with a registered adapter descriptor."""
    return [descriptor.platform for descriptor in _ADAPTERS]


def registered_adapters() -> tuple[AdapterDescriptor, ...]:
    """Return the immutable adapter inventory in URL-detection order."""
    return _ADAPTERS


def adapter_for_platform(platform: str) -> AdapterDescriptor | None:
    """Resolve an exact platform identifier to its versioned descriptor."""
    return _ADAPTERS_BY_PLATFORM.get((platform or "").strip().lower())


def adapter_for_url(url: str) -> AdapterDescriptor | None:
    """Resolve a URL through the same detector used by ingestion and routing."""
    return adapter_for_platform(detect_platform(url))
