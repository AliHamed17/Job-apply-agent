"""Quarantined Ashby employer API transport.

Ashby's documented ``applicationForm.submit`` API uses HTTP Basic
authentication and requires a key with Candidates write permission. This
module intentionally contains no HTTP client and cannot submit. A separately
authorized and qualified release may implement that transport later.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.submission_domain import FailedBeforeCommitOutcome, ReasonCode

ASHBY_OFFICIAL_APPLICATION_ENDPOINT = "https://api.ashbyhq.com/applicationForm.submit"
ASHBY_REQUIRED_PERMISSION = "candidatesWrite"


@dataclass(frozen=True, slots=True, repr=False)
class AshbyApiAuthorization:
    """Explicit operator attestation for a legitimate employer-issued key."""

    api_key: str
    candidates_write_confirmed: bool

    def __repr__(self) -> str:
        return "AshbyApiAuthorization(<redacted>)"

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip()) and self.candidates_write_confirmed is True


class AshbyAuthorizedApiTransport:
    """Disabled placeholder that cannot be mistaken for browser fallback."""

    enabled = False
    qualification = "disabled"

    async def submit(self, *_args, **_kwargs) -> FailedBeforeCommitOutcome:
        return FailedBeforeCommitOutcome(reason_code=ReasonCode.ADAPTER_NOT_QUALIFIED)


def authorized_ashby_api_transport(
    authorization: AshbyApiAuthorization | None,
) -> AshbyAuthorizedApiTransport | None:
    """Never expose an API executor before a separate qualification release."""

    del authorization
    return None
