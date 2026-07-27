"""Disabled boundary for a future authorized Greenhouse API transport.

Candidate-browser automation and employer-issued API authorization are
different capabilities.  This module intentionally contains no HTTP client,
credential fields, or fallback into the browser adapter.  A future API release
must bind one employer board, legitimate scope, receipt schema, and
qualification record before replacing this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.submission_domain import ReasonCode


class GreenhouseApiDisabledError(RuntimeError):
    """The separately authorized employer API capability is unavailable."""

    reason_code = ReasonCode.ADAPTER_NOT_QUALIFIED


@dataclass(frozen=True, slots=True)
class GreenhouseApiCapability:
    """Reader-facing metadata for the deliberately disabled API mode."""

    transport: str = "employer_authorized_api"
    authentication_mode: str = "employer_issued_basic_auth"
    tenant_binding_required: bool = True
    enabled: bool = False

    def require_enabled(self) -> None:
        """Fail before accepting credentials, private content, or network work."""

        raise GreenhouseApiDisabledError(self.reason_code)

    @property
    def reason_code(self) -> ReasonCode:
        return ReasonCode.ADAPTER_NOT_QUALIFIED


greenhouse_api_capability = GreenhouseApiCapability()
