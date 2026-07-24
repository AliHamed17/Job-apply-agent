from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.config import Settings
from core.operations import rate_limit_allowed, readiness_report


def test_production_configuration_rejects_placeholders() -> None:
    settings = Settings(
        app_env="production",
        secret_key="change-me",
        whatsapp_app_secret="",
        cors_origins="*",
        dry_run=False,
    )
    with pytest.raises(ValueError, match="Unsafe production configuration"):
        settings.validate_runtime()


def test_production_configuration_accepts_safe_dry_run(tmp_path: Path) -> None:
    settings = Settings(
        app_env="production",
        secret_key="s" * 32,
        whatsapp_app_secret="verified-secret",
        cors_origins="https://jobs.example.test",
        draft_only=True,
        auto_apply=False,
        dry_run=True,
        application_data_dir=str(tmp_path),
    )
    settings.validate_runtime()


def test_live_production_requires_explicit_acknowledgement() -> None:
    settings = Settings(
        app_env="production",
        secret_key="s" * 32,
        whatsapp_app_secret="verified-secret",
        cors_origins="https://jobs.example.test",
        draft_only=False,
        dry_run=False,
    )
    with pytest.raises(ValueError, match="LIVE_AUTOMATION_ACKNOWLEDGED"):
        settings.validate_runtime()


def test_redis_rate_limit_is_atomic() -> None:
    pipeline = MagicMock()
    pipeline.__enter__.return_value = pipeline
    pipeline.execute.side_effect = [(1, True), (2, True)]
    client = MagicMock()
    client.pipeline.return_value = pipeline
    with patch("core.operations.redis_client", return_value=client):
        assert rate_limit_allowed("example", 1)
        assert not rate_limit_allowed("example", 1)


def test_readiness_degrades_for_missing_dependency(tmp_path: Path) -> None:
    settings = Settings(application_data_dir=str(tmp_path))
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.scalar.return_value = "004_submission_attempts"
    engine = MagicMock()
    engine.connect.return_value = connection
    client = MagicMock()
    client.ping.return_value = True
    client.get.return_value = None
    with (
        patch("core.operations.get_engine", return_value=engine),
        patch("core.operations.redis_client", return_value=client),
        patch(
            "alembic.script.ScriptDirectory.get_current_head",
            return_value="004_submission_attempts",
        ),
    ):
        report = readiness_report(settings)
    assert report["status"] == "degraded"
    assert report["checks"]["database"]["ok"]
    assert not report["checks"]["worker"]["ok"]


def test_metrics_are_prometheus_and_contain_no_personal_data(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    from api.main import app

    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "job_agent_http_requests_total" in response.text
    forbidden = [
        "person@example.com",
        "+972501234567",
        "linkedin.com/jobs/view/",
        "https://",
    ]
    assert not any(value in response.text for value in forbidden)
    assert not re.search(r'route="[^"]*\\d{4,}', response.text)
