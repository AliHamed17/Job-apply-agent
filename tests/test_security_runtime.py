"""Tests for auth/runtime-related behavior in app middleware and settings."""

from fastapi.testclient import TestClient

from api.main import _rate_limit_store, app, settings
from core.config import Settings


def test_cors_origins_parsing():
    parsed = Settings(cors_allowed_origins="https://a.com, https://b.com ").cors_allowed_origin_list
    assert parsed == ["https://a.com", "https://b.com"]


def test_auth_bypass_allowed_only_with_explicit_flag():
    original_env = settings.app_env
    original_secret = settings.secret_key
    original_bypass = settings.allow_insecure_auth_bypass
    _rate_limit_store.clear()
    try:
        settings.app_env = "dev"
        settings.secret_key = "change-me"
        settings.allow_insecure_auth_bypass = False
        with TestClient(app) as client:
            resp = client.get("/api/jobs")
        assert resp.status_code == 401

        settings.allow_insecure_auth_bypass = True
        with TestClient(app) as client:
            resp = client.get("/api/jobs")
        assert resp.status_code == 200
    finally:
        settings.app_env = original_env
        settings.secret_key = original_secret
        settings.allow_insecure_auth_bypass = original_bypass


def test_auth_not_bypassed_in_prod_with_default_secret():
    original_env = settings.app_env
    original_secret = settings.secret_key
    _rate_limit_store.clear()
    try:
        settings.app_env = "dev"
        settings.secret_key = "change-me"
        with TestClient(app) as client:
            settings.app_env = "prod"
            settings.secret_key = "change-me"
            resp = client.get("/api/jobs")
        assert resp.status_code == 401
    finally:
        settings.app_env = original_env
        settings.secret_key = original_secret


def test_non_exact_docs_path_is_not_auth_exempt():
    original_env = settings.app_env
    original_secret = settings.secret_key
    _rate_limit_store.clear()
    try:
        settings.app_env = "prod"
        settings.secret_key = "real-secret"
        with TestClient(app) as client:
            resp = client.get("/docsx")
        assert resp.status_code == 401
    finally:
        settings.app_env = original_env
        settings.secret_key = original_secret
