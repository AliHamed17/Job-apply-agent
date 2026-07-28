from __future__ import annotations

import inspect

import pytest

from core.submission_domain import AttemptOutcome, ReasonCode
from submitters import ashby as legacy_ashby
from submitters import ashby_api
from submitters.ashby_api import (
    ASHBY_OFFICIAL_APPLICATION_ENDPOINT,
    ASHBY_REQUIRED_PERMISSION,
    AshbyApiAuthorization,
    AshbyAuthorizedApiTransport,
    authorized_ashby_api_transport,
)


def test_undocumented_public_transport_and_success_shim_are_absent() -> None:
    sources = (
        inspect.getsource(legacy_ashby),
        inspect.getsource(ashby_api),
    )
    lowered = "\n".join(sources).casefold()

    assert "_api_base" not in lowered
    assert "client.post(" not in lowered
    assert "status_code in (200, 201)" not in lowered
    assert "asyncclient" not in lowered
    assert "httpx" not in lowered
    assert "_submit_via_browser" not in lowered
    assert ASHBY_OFFICIAL_APPLICATION_ENDPOINT.endswith("/applicationForm.submit")
    assert ASHBY_REQUIRED_PERMISSION == "candidatesWrite"


def test_authorization_is_redacted_and_cannot_enable_transport() -> None:
    authorization = AshbyApiAuthorization(
        api_key="fixture-secret",
        candidates_write_confirmed=True,
    )

    assert authorization.configured is True
    assert "fixture-secret" not in repr(authorization)
    assert authorized_ashby_api_transport(authorization) is None
    assert AshbyAuthorizedApiTransport.enabled is False
    assert AshbyAuthorizedApiTransport.qualification == "disabled"


@pytest.mark.asyncio
async def test_api_placeholder_fails_before_commit() -> None:
    outcome = await AshbyAuthorizedApiTransport().submit()

    assert outcome.kind is AttemptOutcome.FAILED_BEFORE_COMMIT
    assert outcome.reason_code is ReasonCode.ADAPTER_NOT_QUALIFIED
