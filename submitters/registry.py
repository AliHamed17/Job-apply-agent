"""Process-global two-phase adapter registration.

Importing this module never launches a browser. Implemented candidate adapters
are registered for version inventory and explicit offline qualification, while
:meth:`SubmitterRegistry.get_inspector` returns no ordinary employer inspector
until an exact adapter and form scope reach dry-run qualification. Tests and
explicit local qualification commands may inject isolated registries and
session factories.
"""

from __future__ import annotations

from threading import Lock

from submitters.ashby_playwright import playwright_ashby_browser_factory
from submitters.ashby_v1 import register_ashby_browser_v1
from submitters.base import SubmitterRegistry, two_phase_registry
from submitters.greenhouse_playwright import playwright_greenhouse_browser_factory
from submitters.greenhouse_v1 import register_greenhouse_browser_v1
from submitters.lever_playwright import playwright_lever_browser_factory
from submitters.lever_v1 import register_lever_browser_v1
from submitters.platforms import AdapterDescriptor, adapter_for_platform, adapter_for_url
from submitters.smartrecruiters_playwright import (
    playwright_smartrecruiters_browser_factory,
)
from submitters.smartrecruiters_v1 import register_smartrecruiters_browser_v1
from submitters.workday_playwright import playwright_workday_browser_factory
from submitters.workday_v2 import register_workday_browser_v2

_REGISTRY_LOCK = Lock()
_INITIALIZED = False


def get_two_phase_registry() -> SubmitterRegistry:
    """Return one lazily initialized process-global adapter registry."""

    global _INITIALIZED
    if _INITIALIZED:
        return two_phase_registry
    with _REGISTRY_LOCK:
        if not _INITIALIZED:
            register_workday_browser_v2(
                two_phase_registry,
                browser_factory=playwright_workday_browser_factory,
            )
            register_greenhouse_browser_v1(
                two_phase_registry,
                browser_factory=playwright_greenhouse_browser_factory,
            )
            register_lever_browser_v1(
                two_phase_registry,
                browser_factory=playwright_lever_browser_factory,
            )
            register_ashby_browser_v1(
                two_phase_registry,
                browser_factory=playwright_ashby_browser_factory,
            )
            register_smartrecruiters_browser_v1(
                two_phase_registry,
                browser_factory=playwright_smartrecruiters_browser_factory,
            )
            _INITIALIZED = True
    return two_phase_registry


def _same_adapter_code(left: AdapterDescriptor, right: AdapterDescriptor) -> bool:
    """Compare immutable code identity while allowing scoped evidence to differ."""

    return (
        left.platform == right.platform
        and left.adapter_version == right.adapter_version
        and left.selector_version == right.selector_version
        and left.execution_contract_version == right.execution_contract_version
        and left.transport == right.transport
        and left.authentication_mode == right.authentication_mode
        and left.supported_controls == right.supported_controls
        and left.domains == right.domains
        and left.allow_subdomains == right.allow_subdomains
    )


def build_scoped_two_phase_registry(descriptor: AdapterDescriptor) -> SubmitterRegistry:
    """Build a fresh registry for one already-validated effective descriptor.

    The descriptor must match committed adapter code exactly. Qualification and
    form scope may differ only because the caller derived them from strict local
    database authority. A fresh instance prevents prepared browser state from
    leaking across independently qualified scopes.
    """

    committed = adapter_for_platform(descriptor.platform)
    if committed is None or not _same_adapter_code(committed, descriptor):
        raise ValueError("adapter descriptor does not match committed code")

    def by_platform(platform: str) -> AdapterDescriptor | None:
        return descriptor if (platform or "").strip().lower() == descriptor.platform else None

    def by_url(url: str) -> AdapterDescriptor | None:
        detected = adapter_for_url(url)
        if detected is None or not _same_adapter_code(detected, descriptor):
            return None
        return descriptor

    registry = SubmitterRegistry(
        platform_descriptor_resolver=by_platform,
        url_descriptor_resolver=by_url,
    )
    if descriptor.platform == "workday":
        register_workday_browser_v2(
            registry,
            browser_factory=playwright_workday_browser_factory,
            descriptor=descriptor,
        )
    elif descriptor.platform == "greenhouse":
        register_greenhouse_browser_v1(
            registry,
            browser_factory=playwright_greenhouse_browser_factory,
            descriptor=descriptor,
        )
    elif descriptor.platform == "lever":
        register_lever_browser_v1(
            registry,
            browser_factory=playwright_lever_browser_factory,
            descriptor=descriptor,
        )
    elif descriptor.platform == "ashby":
        register_ashby_browser_v1(
            registry,
            browser_factory=playwright_ashby_browser_factory,
            descriptor=descriptor,
        )
    elif descriptor.platform == "smartrecruiters":
        register_smartrecruiters_browser_v1(
            registry,
            browser_factory=playwright_smartrecruiters_browser_factory,
            descriptor=descriptor,
        )
    else:
        raise ValueError("adapter has no scoped two-phase implementation")
    return registry


__all__ = ["build_scoped_two_phase_registry", "get_two_phase_registry"]
