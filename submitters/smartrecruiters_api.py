"""Disabled, OAuth-protected SmartRecruiters Application API capability."""

from __future__ import annotations

from dataclasses import dataclass

from core.submission_domain import ReasonCode

SMARTRECRUITERS_APPLICATION_SCOPE = "candidate_applications_manage"


class SmartRecruitersApiDisabledError(RuntimeError):
    """Bounded fail-closed signal for the separately authorized API mode."""

    def __init__(self) -> None:
        self.reason_code = ReasonCode.ADAPTER_NOT_QUALIFIED
        super().__init__(self.reason_code.value)


@dataclass(frozen=True, slots=True)
class SmartRecruitersApiCapability:
    transport: str = "protected_application_api"
    authentication_mode: str = "oauth"
    required_scope: str = SMARTRECRUITERS_APPLICATION_SCOPE
    enabled: bool = False
    qualified_posting_scope: tuple[str, ...] = ()

    def require_enabled(
        self,
        *,
        granted_scopes: tuple[str, ...],
        posting_uuid: str,
    ) -> None:
        if (
            not self.enabled
            or self.required_scope not in granted_scopes
            or posting_uuid not in self.qualified_posting_scope
        ):
            raise SmartRecruitersApiDisabledError


SMARTRECRUITERS_API_CAPABILITY = SmartRecruitersApiCapability()


async def submit_via_smartrecruiters_api(
    *_args: object,
    granted_scopes: tuple[str, ...] = (),
    posting_uuid: str = "",
    **_kwargs: object,
) -> None:
    """Never make an API request until a separately authorized release exists."""

    SMARTRECRUITERS_API_CAPABILITY.require_enabled(
        granted_scopes=granted_scopes,
        posting_uuid=posting_uuid,
    )
    raise SmartRecruitersApiDisabledError
