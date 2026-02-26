"""Pytest configuration and fixtures."""

import os
import sys

import pytest
from starlette.testclient import TestClient

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _auth_on_by_default(monkeypatch):
    """Default test mode: auth enabled, clients send Bearer token automatically."""
    from api.main import settings

    original_env = settings.app_env
    original_secret = settings.secret_key
    original_bypass = settings.allow_insecure_auth_bypass

    settings.app_env = "dev"
    settings.secret_key = "test-secret"
    settings.allow_insecure_auth_bypass = False

    original_request = TestClient.request

    def _request_with_auth(self, method, url, *args, **kwargs):
        headers = kwargs.get("headers")
        if headers is None:
            headers = {}
        if "Authorization" not in headers:
            headers = {**headers, "Authorization": f"Bearer {settings.secret_key}"}
        kwargs["headers"] = headers
        return original_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "request", _request_with_auth)
    yield

    settings.app_env = original_env
    settings.secret_key = original_secret
    settings.allow_insecure_auth_bypass = original_bypass


@pytest.fixture
def insecure_auth_bypass():
    """Opt-in legacy mode for tests that intentionally validate bypass behavior."""
    from api.main import settings

    original = settings.allow_insecure_auth_bypass
    settings.allow_insecure_auth_bypass = True
    yield
    settings.allow_insecure_auth_bypass = original
