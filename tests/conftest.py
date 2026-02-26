"""Pytest configuration and fixtures."""

import os
import sys

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _enable_test_auth_bypass():
    """Keep existing test behavior by enabling explicit insecure bypass in test runs."""
    from api.main import settings

    original_env = settings.app_env
    original_secret = settings.secret_key
    original_bypass = settings.allow_insecure_auth_bypass

    settings.app_env = "dev"
    settings.secret_key = "change-me"
    settings.allow_insecure_auth_bypass = True
    yield
    settings.app_env = original_env
    settings.secret_key = original_secret
    settings.allow_insecure_auth_bypass = original_bypass
