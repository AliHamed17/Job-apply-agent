"""Disabled Lever integration-API capability.

Lever's authenticated integration transport is intentionally separate from
the public candidate-browser adapter. No credential, browser failure, or
unsupported form may silently select this transport.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.submission_domain import ReasonCode


class LeverApiDisabledError(RuntimeError):
    """Bounded fail-closed signal for the quarantined API transport."""

    def __init__(self) -> None:
        self.reason_code = ReasonCode.ADAPTER_NOT_QUALIFIED
        super().__init__(self.reason_code.value)


@dataclass(frozen=True, slots=True)
class LeverApiCapability:
    transport: str = "authorized_integration_api"
    authentication_mode: str = "employer_issued_credentials"
    enabled: bool = False
    qualified_scope: tuple[str, ...] = ()

    def require_enabled(self) -> None:
        if not self.enabled or not self.qualified_scope:
            raise LeverApiDisabledError


LEVER_API_CAPABILITY = LeverApiCapability()


async def submit_via_lever_api(*_args: object, **_kwargs: object) -> None:
    """Never perform an API request until a separate authorized release exists."""

    LEVER_API_CAPABILITY.require_enabled()
    raise LeverApiDisabledError
