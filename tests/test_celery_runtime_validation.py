"""Celery worker and Beat must share the API's production safety gate."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from core.config import Settings
from worker import celery_app as celery_module


def _safe_production_settings(**updates: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "production",
        "secret_key": "s" * 40,
        "whatsapp_app_secret": "configured-signature-secret-" + "w" * 32,
        "llm_provider": "ollama",
        "llm_model": "qwen2.5:7b",
        "ollama_base_url": "http://127.0.0.1:11434",
        "ollama_no_cloud": True,
        "cors_origins": "https://control.example.test",
        "draft_only": True,
        "auto_apply": False,
        "dry_run": True,
        "portal_final_submit_enabled": False,
        "live_automation_acknowledged": False,
        "tasks_always_eager": False,
    }
    values.update(updates)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"secret_key": "change-me"}, "SECRET_KEY"),
        (
            {
                "whatsapp_app_secret": "your-app-secret-for-signature-verification",
            },
            "WHATSAPP_APP_SECRET",
        ),
        ({"tasks_always_eager": True}, "TASKS_ALWAYS_EAGER"),
        ({"llm_provider": "openai"}, "LLM_PROVIDER"),
        (
            {
                "dry_run": False,
                "live_automation_acknowledged": False,
            },
            "LIVE_AUTOMATION_ACKNOWLEDGED",
        ),
    ],
)
def test_celery_app_rejects_unsafe_production_before_construction(
    monkeypatch,
    updates: dict[str, Any],
    message: str,
) -> None:
    constructed = False

    def forbidden_constructor(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        nonlocal constructed
        constructed = True
        raise AssertionError("Celery must not be constructed")

    monkeypatch.setattr(celery_module, "Celery", forbidden_constructor)

    with pytest.raises(ValueError, match=message):
        celery_module.create_celery_app(_safe_production_settings(**updates))

    assert not constructed


@pytest.mark.parametrize(
    "startup_hook",
    [
        celery_module.validate_worker_startup,
        celery_module.validate_beat_startup,
    ],
    ids=["worker", "beat"],
)
def test_worker_and_beat_startup_revalidate_production(
    monkeypatch,
    startup_hook: Callable[..., None],
) -> None:
    unsafe = _safe_production_settings(llm_provider="anthropic")
    monkeypatch.setattr(celery_module, "get_settings", lambda: unsafe)

    with pytest.raises(ValueError, match="LLM_PROVIDER"):
        startup_hook()


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_nonproduction_celery_creation_remains_import_safe(app_env: str) -> None:
    app = celery_module.create_celery_app(
        Settings(
            _env_file=None,
            app_env=app_env,
            secret_key="change-me",
            llm_provider="mock",
            dry_run=False,
        )
    )

    assert app.main == "job_apply_agent"
