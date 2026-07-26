"""Process-global two-phase adapter registration.

Importing this module never launches a browser. Workday v2 is currently
fixture-qualified only. It is registered for version inventory and explicit
offline qualification, while :meth:`SubmitterRegistry.get_inspector` returns
no ordinary employer inspector until a later scoped dry-run qualification.
Tests and explicit local qualification commands may inject their own registry
and session factory through :func:`register_workday_browser_v2`.
"""

from __future__ import annotations

from threading import Lock

from submitters.base import SubmitterRegistry, two_phase_registry
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
            _INITIALIZED = True
    return two_phase_registry
