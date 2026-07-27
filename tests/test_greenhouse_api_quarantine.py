"""Greenhouse employer API mode remains separate, unconfigured, and inert."""

from pathlib import Path

import pytest

from core.submission_domain import ReasonCode
from submitters.greenhouse_api import (
    GreenhouseApiDisabledError,
    greenhouse_api_capability,
)


def test_greenhouse_api_capability_is_explicitly_disabled() -> None:
    assert greenhouse_api_capability.enabled is False
    assert greenhouse_api_capability.tenant_binding_required is True
    assert greenhouse_api_capability.authentication_mode == "employer_issued_basic_auth"
    assert greenhouse_api_capability.reason_code is ReasonCode.ADAPTER_NOT_QUALIFIED

    with pytest.raises(GreenhouseApiDisabledError) as exc_info:
        greenhouse_api_capability.require_enabled()

    assert exc_info.value.reason_code is ReasonCode.ADAPTER_NOT_QUALIFIED


def test_disabled_api_boundary_contains_no_transport_or_credential_surface() -> None:
    source = Path("submitters/greenhouse_api.py").read_text(encoding="utf-8")
    lowered = source.casefold()

    assert "import httpx" not in lowered
    assert "import requests" not in lowered
    assert "async_playwright" not in lowered
    assert "api_key:" not in lowered
    assert "password:" not in lowered
    assert "authorization:" not in lowered
    assert "https://" not in lowered
