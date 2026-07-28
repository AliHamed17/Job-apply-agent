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
