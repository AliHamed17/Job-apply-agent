"""Tests for auth/runtime-related behavior in app middleware and settings."""

from fastapi.testclient import TestClient

from api.main import _rate_limit_store, app, settings
from core.config import Settings


def test_cors_origins_parsing():
    parsed = Settings(cors_allowed_origins="https://a.com, https://b.com ").cors_allowed_origin_list
    assert parsed == ["https://a.com", "https://b.com"]


def test_auth_bypass_allowed_only_with_explicit_flag(insecure_auth_bypass):
    original_env = settings.app_env
    original_secret = settings.secret_key
    _rate_limit_store.clear()
    try:
        settings.app_env = "dev"
        settings.secret_key = "change-me"
        with TestClient(app) as client:
            resp = client.get("/api/jobs", headers={"Authorization": ""})
        assert resp.status_code == 200
    finally:
        settings.app_env = original_env
        settings.secret_key = original_secret


def test_prod_default_secret_fails_runtime_validation():
    conf = Settings(app_env="prod", secret_key="change-me")
    errors = conf.validate_runtime_config()
    assert any("SECRET_KEY" in e for e in errors)


def test_non_exact_docs_path_is_not_auth_exempt():
    original_env = settings.app_env
    original_secret = settings.secret_key
    _rate_limit_store.clear()
    try:
        settings.app_env = "prod"
        settings.secret_key = "real-secret"
        with TestClient(app) as client:
            resp = client.get("/docsx", headers={"Authorization": ""})
        assert resp.status_code == 401
    finally:
        settings.app_env = original_env
        settings.secret_key = original_secret


def test_security_headers_present_on_health():
    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"
    assert "default-src 'self'" in resp.headers.get("content-security-policy", "")


def test_untrusted_host_is_blocked():
    with TestClient(app) as client:
        resp = client.get("/health", headers={"host": "evil.example.com"})

    assert resp.status_code == 400


def test_prod_wildcard_trusted_host_fails_runtime_validation():
    conf = Settings(app_env="prod", secret_key="abc", trusted_hosts="*")
    errors = conf.validate_runtime_config()
    assert any("TRUSTED_HOSTS" in e for e in errors)
