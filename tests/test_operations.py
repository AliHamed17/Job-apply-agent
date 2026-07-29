from __future__ import annotations

import asyncio
import re
import time
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
        llm_provider="ollama",
        tasks_always_eager=False,
    )
    with pytest.raises(ValueError, match="Unsafe production configuration"):
        settings.validate_runtime()


def test_production_configuration_accepts_safe_dry_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda **_kwargs: True,
    )
    settings = Settings(
        app_env="production",
        secret_key="s" * 32,
        whatsapp_app_secret="verified-signature-secret-" + "w" * 32,
        cors_origins="https://jobs.example.test",
        draft_only=True,
        auto_apply=False,
        dry_run=True,
        application_data_dir=str(tmp_path),
        llm_provider="ollama",
        tasks_always_eager=False,
    )
    settings.validate_runtime()


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "*",
        "https://*.example.test",
        "ftp://control.example.test",
        "http://control.example.test",
        "https://user:password@control.example.test",
        "https://control.example.test/path",
        "https://control.example.test?mode=live",
        "https://control.example.test#fragment",
        "https://control.example.test:99999",
    ],
)
def test_production_rejects_unsafe_cors_origins(tmp_path: Path, origin: str) -> None:
    settings = Settings(
        app_env="production",
        secret_key="s" * 32,
        whatsapp_app_secret="verified-signature-secret-" + "w" * 32,
        cors_origins=origin,
        draft_only=True,
        auto_apply=False,
        dry_run=True,
        application_data_dir=str(tmp_path),
        llm_provider="ollama",
        tasks_always_eager=False,
    )

    with pytest.raises(ValueError, match="exact HTTPS or loopback HTTP origins"):
        settings.validate_runtime()


@pytest.mark.parametrize(
    "origin",
    [
        "https://control.example.test",
        "http://localhost:3000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ],
)
def test_production_accepts_exact_safe_cors_origins(
    tmp_path: Path,
    origin: str,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda **_kwargs: True,
    )
    settings = Settings(
        app_env="production",
        secret_key="s" * 32,
        whatsapp_app_secret="verified-signature-secret-" + "w" * 32,
        cors_origins=origin,
        draft_only=True,
        auto_apply=False,
        dry_run=True,
        application_data_dir=str(tmp_path),
        llm_provider="ollama",
        tasks_always_eager=False,
    )

    settings.validate_runtime()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"llm_provider": "mock"}, "LLM_PROVIDER must be ollama"),
        ({"llm_model": "qwen2.5:14b"}, "qualified qwen2.5:7b"),
        (
            {"ollama_base_url": "https://remote.example.test"},
            "local inference endpoint",
        ),
        ({"ollama_no_cloud": False}, "OLLAMA_NO_CLOUD"),
    ],
)
def test_production_rejects_unqualified_or_nonlocal_llm(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    runtime_options = {"llm_provider": "ollama", **overrides}
    settings = Settings(
        app_env="production",
        secret_key="s" * 32,
        whatsapp_app_secret="verified-signature-secret-" + "w" * 32,
        cors_origins="https://jobs.example.test",
        draft_only=True,
        auto_apply=False,
        dry_run=True,
        application_data_dir=str(tmp_path),
        tasks_always_eager=False,
        **runtime_options,
    )

    with pytest.raises(ValueError, match=message):
        settings.validate_runtime()


def test_live_production_requires_explicit_acknowledgement() -> None:
    settings = Settings(
        app_env="production",
        secret_key="s" * 32,
        whatsapp_app_secret="verified-signature-secret-" + "w" * 32,
        cors_origins="https://jobs.example.test",
        draft_only=False,
        dry_run=False,
        live_automation_acknowledged=False,
        llm_provider="ollama",
        tasks_always_eager=False,
    )
    with pytest.raises(ValueError, match="LIVE_AUTOMATION_ACKNOWLEDGED"):
        settings.validate_runtime()


def test_live_production_requires_postgresql() -> None:
    settings = Settings(
        app_env="production",
        secret_key="s" * 32,
        whatsapp_app_secret="verified-signature-secret-" + "w" * 32,
        cors_origins="https://jobs.example.test",
        database_url="sqlite:///unsafe-live.db",
        draft_only=False,
        dry_run=False,
        portal_final_submit_enabled=True,
        live_automation_acknowledged=True,
        llm_provider="ollama",
        tasks_always_eager=False,
    )
    with pytest.raises(ValueError, match="requires PostgreSQL"):
        settings.validate_runtime()


def test_weak_development_auth_cannot_enter_live_api_mode(monkeypatch) -> None:
    import api.main as api_main

    monkeypatch.setattr(api_main.settings, "app_env", "development")
    monkeypatch.setattr(api_main.settings, "secret_key", "change-me")
    monkeypatch.setattr(api_main.settings, "dry_run", False)
    monkeypatch.setattr(api_main.settings, "draft_only", False)
    monkeypatch.setattr(api_main.settings, "portal_final_submit_enabled", True)
    response = TestClient(api_main.app).get(
        "/api/applications",
        headers={"Authorization": "Bearer change-me"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OPERATOR_AUTH_REQUIRED"


def test_short_custom_development_secret_still_protects_prepare_only_api(monkeypatch) -> None:
    import api.main as api_main

    monkeypatch.setattr(api_main.settings, "app_env", "development")
    monkeypatch.setattr(api_main.settings, "secret_key", "short-custom-token")
    monkeypatch.setattr(api_main.settings, "dry_run", True)
    monkeypatch.setattr(api_main.settings, "draft_only", True)
    monkeypatch.setattr(api_main.settings, "portal_final_submit_enabled", False)
    client = TestClient(api_main.app)

    assert client.get("/api/runtime/capabilities").status_code == 401
    assert (
        client.get(
            "/api/runtime/capabilities",
            headers={"Authorization": "Bearer short-custom-token"},
        ).status_code
        == 200
    )


def test_redis_rate_limit_is_atomic() -> None:
    pipeline = MagicMock()
    pipeline.__enter__.return_value = pipeline
    pipeline.execute.side_effect = [(1, True), (2, True)]
    client = MagicMock()
    client.pipeline.return_value = pipeline
    with patch("core.operations.redis_client", return_value=client):
        assert rate_limit_allowed("example", 1)
        assert not rate_limit_allowed("example", 1)


def test_public_control_plane_survives_redis_outage(monkeypatch) -> None:
    import api.main as api_main

    def redis_unavailable(*_args, **_kwargs):
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr(api_main.settings, "app_env", "production")
    monkeypatch.setattr(api_main, "rate_limit_allowed", redis_unavailable)
    monkeypatch.setattr(
        api_main,
        "readiness_report",
        lambda *_args: {"status": "degraded", "checks": {"redis": {"ok": False}}},
    )
    client = TestClient(api_main.app)

    assert client.get("/").status_code == 200
    assert client.get("/health/live").status_code == 200
    readiness = client.get("/health/ready")
    assert readiness.status_code == 503
    assert readiness.json()["status"] == "degraded"
    assert client.get("/static/js/app.js").status_code == 200

    protected = client.get(
        "/api/applications",
        headers={"Authorization": f"Bearer {api_main.settings.secret_key}"},
    )
    assert protected.status_code == 503
    assert protected.json() == {"detail": "Service unavailable"}


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["health", "runtime"])
async def test_readiness_http_probe_does_not_block_event_loop(
    monkeypatch,
    endpoint: str,
) -> None:
    import api.main as api_main
    from api.routes import runtime as runtime_route

    report = {
        "status": "degraded",
        "checks": {
            "llm": {
                "ok": False,
                "provider": "ollama",
                "model": "qwen2.5:7b",
                "local": True,
                "digest": None,
                "reason_code": "LLM_PROVIDER_UNAVAILABLE",
            }
        },
    }

    def slow_readiness(*_args):
        time.sleep(0.15)
        return report

    if endpoint == "health":
        monkeypatch.setattr(api_main, "readiness_report", slow_readiness)
        probe = api_main.health_ready
    else:
        monkeypatch.setattr(runtime_route, "readiness_report", slow_readiness)
        probe = runtime_route.get_runtime_capabilities

    task = asyncio.create_task(probe())
    started = time.perf_counter()
    await asyncio.sleep(0.02)
    assert time.perf_counter() - started < 0.08
    await task


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


def test_http_metric_method_uses_fixed_vocabulary(monkeypatch) -> None:
    import api.main as api_main
    from core.metrics import normalize_http_method

    hostile_method = "CANDIDATEEMAILPRIVATE"
    assert normalize_http_method("get") == "GET"
    assert normalize_http_method(hostile_method) == "OTHER"

    monkeypatch.setattr(api_main.settings, "app_env", "development")
    client = TestClient(api_main.app)
    client.request(
        hostile_method,
        "/health/live",
        headers={"Authorization": f"Bearer {api_main.settings.secret_key}"},
    )
    exposition = client.get("/metrics").text

    assert 'method="OTHER"' in exposition
    assert hostile_method not in exposition
