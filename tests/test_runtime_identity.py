from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from core.config import Settings
from core.operations import _heartbeat_status, record_heartbeat
from core.runtime_identity import (
    PROTOCOL_VERSION,
    RuntimeIdentity,
    build_runtime_capabilities,
    build_runtime_identity,
    compute_source_digest,
    compute_ui_asset_digest,
    resolve_build_sha,
)
from core.single_instance import (
    AlreadyRunningError,
    SingleInstanceLock,
    acquire_dashboard_instance_lock,
    resolve_dashboard_lock_config,
)


def _identity(
    build_sha: str = "a" * 40,
    *,
    source_digest: str = "sha256:" + "c" * 64,
    protocol_version: str = PROTOCOL_VERSION,
) -> RuntimeIdentity:
    return RuntimeIdentity(
        build_sha=build_sha,
        build_source="test",
        ui_asset_digest="sha256:" + "b" * 64,
        source_digest=source_digest,
        protocol_version=protocol_version,
        boot_id="00000000-0000-4000-8000-000000000001",
        started_at=datetime(2026, 7, 26, tzinfo=UTC),
    )


def _ready_report(
    *,
    worker_build: str = "a" * 40,
    worker_protocol: str = PROTOCOL_VERSION,
) -> dict[str, object]:
    checks: dict[str, dict[str, object]] = {
        component: {"ok": True}
        for component in (
            "database",
            "migration",
            "redis",
            "worker",
            "beat",
            "shared_storage",
            "browser",
            "llm",
        )
    }
    checks["llm"].update(
        provider="mock",
        model="deterministic-test",
        local=True,
        digest=None,
    )
    worker_identity = _identity(worker_build, protocol_version=worker_protocol)
    checks["worker"].update(
        build_sha=worker_build,
        source_digest=worker_identity.source_digest,
        release_id=worker_identity.release_id,
        protocol_version=worker_protocol,
    )
    return {"status": "ready", "checks": checks}


def _live_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        dry_run=False,
        draft_only=False,
        auto_apply=False,
        portal_final_submit_enabled=True,
        live_automation_acknowledged=True,
        database_url="postgresql://jobagent:test@localhost/jobagent",
        secret_key="operator-auth-test-secret-" + "x" * 32,
    )


def test_build_sha_prefers_bounded_deployment_environment() -> None:
    build_sha, source = resolve_build_sha(
        environ={
            "APP_BUILD_SHA": "release-2026.07.26",
            "VERCEL_GIT_COMMIT_SHA": "f" * 40,
        }
    )
    assert build_sha == "release-2026.07.26"
    assert source == "app_build_sha"


def test_build_sha_ignores_unsafe_environment_and_reads_git_ref(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    ref = git_dir / "refs" / "heads" / "main"
    ref.parent.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    ref.write_text("0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")

    build_sha, source = resolve_build_sha(
        environ={"APP_BUILD_SHA": "../../not-a-release"},
        repo_root=tmp_path,
    )

    assert build_sha == "0123456789abcdef0123456789abcdef01234567"
    assert source == "git_ref"


def test_ui_asset_digest_is_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    static_dir = tmp_path / "api" / "static"
    templates_dir = tmp_path / "api" / "templates"
    static_dir.mkdir(parents=True)
    templates_dir.mkdir(parents=True)
    script = static_dir / "app.js"
    script.write_text("const version = 1;", encoding="utf-8")
    (templates_dir / "index.html").write_text("<main>Dashboard</main>", encoding="utf-8")

    first = compute_ui_asset_digest(tmp_path)
    assert first == compute_ui_asset_digest(tmp_path)

    script.write_text("const version = 2;", encoding="utf-8")
    assert compute_ui_asset_digest(tmp_path) != first


def test_source_digest_is_allowlisted_deterministic_and_content_sensitive(
    tmp_path: Path,
) -> None:
    core_dir = tmp_path / "core"
    migrations_dir = tmp_path / "migrations" / "versions"
    private_dir = tmp_path / "cvs"
    core_dir.mkdir()
    migrations_dir.mkdir(parents=True)
    private_dir.mkdir()
    runtime_source = core_dir / "service.py"
    migration = migrations_dir / "001_initial.py"
    runtime_source.write_text("VERSION = 1\n", encoding="utf-8")
    migration.write_text('revision = "001"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    private_cv = private_dir / "personal.pdf"
    private_cv.write_bytes(b"private-cv-v1")

    first = compute_source_digest(tmp_path)
    assert first == compute_source_digest(tmp_path)
    assert first.startswith("sha256:")

    private_cv.write_bytes(b"private-cv-v2")
    assert compute_source_digest(tmp_path) == first

    runtime_source.write_text("VERSION = 2\n", encoding="utf-8")
    assert compute_source_digest(tmp_path) != first


def test_capabilities_allow_ready_command_protocol_runtime() -> None:
    result = build_runtime_capabilities(
        _live_settings(),
        _ready_report(),
        _identity(),
    )

    assert result["mode"] == {
        "name": "explicit_live",
        "dry_run": False,
        "draft_only": False,
        "live_submit_enabled": True,
    }
    assert result["submission"] == {"allowed": True, "reasons": []}
    assert result["worker"]["compatible"] is True
    assert set(result["readiness"]["checks"]) == {
        "database",
        "migration",
        "redis",
        "worker",
        "beat",
        "shared_storage",
        "browser",
        "llm",
    }


def test_local_model_outage_denies_submission() -> None:
    readiness = _ready_report()
    readiness["checks"]["llm"].update(
        ok=False,
        reason_code="LLM_PROVIDER_UNAVAILABLE",
    )

    result = build_runtime_capabilities(
        _live_settings(),
        readiness,
        _identity(),
    )

    assert result["submission"]["allowed"] is False
    assert result["submission"]["reasons"] == ["RUNTIME_NOT_READY"]
    assert result["readiness"]["checks"]["llm"] is False


def test_capabilities_fail_closed_with_bounded_reasons() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        dry_run=True,
        draft_only=True,
        auto_apply=True,
        portal_final_submit_enabled=False,
        live_automation_acknowledged=False,
        secret_key="change-me",
        database_url="sqlite:///./test-runtime-identity.db",
    )
    readiness = _ready_report(worker_build="c" * 40, worker_protocol="old")
    readiness["checks"]["browser"]["ok"] = False
    readiness["checks"]["secret-dependency"] = {
        "ok": False,
        "detail": "person@example.com",
    }

    result = build_runtime_capabilities(settings, readiness, _identity())

    assert result["submission"]["allowed"] is False
    assert result["submission"]["reasons"] == [
        "DRY_RUN_ENABLED",
        "DRAFT_ONLY_ENABLED",
        "FINAL_SUBMIT_DISABLED",
        "LIVE_AUTOMATION_NOT_ACKNOWLEDGED",
        "UNATTENDED_AUTOMATION_ENABLED",
        "DATABASE_SERIALIZATION_REQUIRED",
        "OPERATOR_AUTH_REQUIRED",
        "RUNTIME_NOT_READY",
        "BUILD_MISMATCH",
        "PROTOCOL_MISMATCH",
    ]
    serialized = json.dumps(result)
    assert "person@example.com" not in serialized
    assert "secret-dependency" not in serialized


def test_development_default_secret_never_allows_live_submission() -> None:
    settings = _live_settings().model_copy(
        update={
            "app_env": "development",
            "secret_key": "change-me",
        }
    )

    result = build_runtime_capabilities(settings, _ready_report(), _identity())

    assert result["mode"]["name"] == "blocked_auth"
    assert result["mode"]["live_submit_enabled"] is False
    assert result["submission"]["allowed"] is False
    assert result["submission"]["reasons"] == ["OPERATOR_AUTH_REQUIRED"]


def test_same_git_build_with_different_backend_source_is_incompatible() -> None:
    api_identity = _identity()
    worker_identity = _identity(source_digest="sha256:" + "d" * 64)
    readiness = _ready_report()
    readiness["checks"]["worker"].update(
        source_digest=worker_identity.source_digest,
        release_id=worker_identity.release_id,
    )

    result = build_runtime_capabilities(_live_settings(), readiness, api_identity)

    assert result["submission"]["allowed"] is False
    assert result["submission"]["reasons"] == ["BUILD_MISMATCH"]
    assert result["worker"]["compatible"] is False


def test_source_mutation_after_identity_creation_denies_submission(tmp_path: Path) -> None:
    core_dir = tmp_path / "core"
    core_dir.mkdir()
    runtime_source = core_dir / "service.py"
    runtime_source.write_text("VERSION = 1\n", encoding="utf-8")
    identity = build_runtime_identity(
        environ={"APP_BUILD_SHA": "same-git-head"},
        project_root=tmp_path,
    )
    readiness = _ready_report(worker_build=identity.build_sha)
    readiness["checks"]["worker"].update(
        source_digest=identity.source_digest,
        release_id=identity.release_id,
    )

    runtime_source.write_text("VERSION = 2\n", encoding="utf-8")
    result = build_runtime_capabilities(
        _live_settings(),
        readiness,
        identity,
        current_source_digest=compute_source_digest(tmp_path),
    )

    assert result["submission"]["allowed"] is False
    assert result["submission"]["reasons"] == ["BUILD_MISMATCH"]


def test_unknown_build_identity_never_enables_submission() -> None:
    unknown_identity = _identity("unknown")
    result = build_runtime_capabilities(
        _live_settings(),
        _ready_report(worker_build="unknown"),
        unknown_identity,
    )

    assert result["submission"] == {
        "allowed": False,
        "reasons": [
            "BUILD_IDENTITY_UNAVAILABLE",
            "WORKER_IDENTITY_UNAVAILABLE",
        ],
    }
    assert result["worker"]["compatible"] is False


def test_runtime_capabilities_preserve_only_bounded_ollama_server_version() -> None:
    report = _ready_report()
    report["checks"]["llm"]["ollama_server_version"] = "0.31.1"
    result = build_runtime_capabilities(
        _live_settings(),
        report,
        _identity(),
    )
    assert result["llm"]["ollama_server_version"] == "0.31.1"

    report["checks"]["llm"]["ollama_server_version"] = "https://private.invalid"
    result = build_runtime_capabilities(
        _live_settings(),
        report,
        _identity(),
    )
    assert result["llm"]["ollama_server_version"] is None


def test_legacy_worker_heartbeat_remains_readable() -> None:
    settings = Settings(_env_file=None, dependency_heartbeat_ttl_seconds=120)
    client = MagicMock()
    client.get.return_value = "1000"
    with (
        patch("core.operations.redis_client", return_value=client),
        patch("core.operations.time.time", return_value=1010),
    ):
        status = _heartbeat_status("worker", settings)

    assert status == {"ok": True, "age_seconds": 10.0}


def test_invalid_worker_heartbeat_degrades_without_raising() -> None:
    settings = Settings(_env_file=None)
    client = MagicMock()
    client.get.return_value = "not-json-or-a-timestamp"
    with patch("core.operations.redis_client", return_value=client):
        assert _heartbeat_status("worker", settings) == {
            "ok": False,
            "detail": "invalid",
        }


def test_heartbeat_publishes_release_and_protocol_metadata() -> None:
    client = MagicMock()
    identity = _identity()
    with (
        patch("core.operations.redis_client", return_value=client),
        patch("core.operations.get_runtime_identity", return_value=identity),
        patch("core.operations.time.time", return_value=1234.5),
    ):
        record_heartbeat("worker", Settings(_env_file=None))

    args, kwargs = client.set.call_args
    payload = json.loads(args[1])
    assert args[0] == "job-agent:heartbeat:worker"
    assert payload == {
        "seen_at": 1234.5,
        "build_sha": identity.build_sha,
        "source_digest": identity.source_digest,
        "release_id": identity.release_id,
        "protocol_version": PROTOCOL_VERSION,
    }
    assert kwargs == {"ex": 3600}


def test_runtime_capabilities_endpoint_has_bounded_shape(monkeypatch, auth_headers) -> None:
    from api.main import app
    from api.routes import runtime as runtime_route

    expected = build_runtime_capabilities(_live_settings(), _ready_report(), _identity())
    monkeypatch.setattr(runtime_route, "get_settings", _live_settings)
    monkeypatch.setattr(runtime_route, "readiness_report", lambda _settings: _ready_report())
    monkeypatch.setattr(
        runtime_route,
        "build_runtime_capabilities",
        lambda _settings, _report: expected,
    )

    response = TestClient(app).get(
        "/api/runtime/capabilities",
        headers=auth_headers,
    )

    assert response.status_code == 200
    expected_response = {
        **expected,
        "llm": {key: value for key, value in expected["llm"].items() if value is not None},
    }
    assert response.json() == expected_response
    assert set(response.json()) == {
        "release",
        "mode",
        "readiness",
        "submission",
        "worker",
        "llm",
    }
    assert response.json()["llm"] == {
        "provider": "mock",
        "model": "deterministic-test",
        "local": True,
        "ready": True,
    }


def test_dashboard_embeds_exact_api_release_identity(monkeypatch) -> None:
    import api.main as api_main

    identity = _identity()
    monkeypatch.setattr(api_main, "get_runtime_identity", lambda: identity)
    monkeypatch.setattr(
        api_main,
        "compute_ui_asset_digest",
        lambda: identity.ui_asset_digest,
    )
    monkeypatch.setattr(
        api_main,
        "compute_source_digest",
        lambda: identity.source_digest,
    )

    response = TestClient(api_main.app).get("/")

    assert response.status_code == 200
    assert f'name="job-agent-build-sha" content="{identity.build_sha}"' in response.text
    assert f'name="job-agent-ui-digest" content="{identity.ui_asset_digest}"' in response.text
    assert f'name="job-agent-source-digest" content="{identity.source_digest}"' in response.text
    assert f'name="job-agent-protocol" content="{identity.protocol_version}"' in response.text
    assert f'name="job-agent-boot-id" content="{identity.boot_id}"' in response.text


def test_single_instance_lock_blocks_same_endpoint_and_releases(tmp_path: Path) -> None:
    first = SingleInstanceLock("127.0.0.1", 8000, boot_id="first", lock_dir=tmp_path)
    second = SingleInstanceLock("127.0.0.1", 8000, boot_id="second", lock_dir=tmp_path)

    with first:
        with pytest.raises(AlreadyRunningError) as exc_info:
            second.acquire()
        assert exc_info.value.owner["pid"] is not None
        assert exc_info.value.owner["boot_id"] == "first"

    with second:
        assert second.acquired
    assert not second.acquired


def test_single_instance_lock_is_scoped_by_port(tmp_path: Path) -> None:
    with (
        SingleInstanceLock("localhost", 8000, lock_dir=tmp_path),
        SingleInstanceLock("localhost", 8001, lock_dir=tmp_path),
    ):
        assert len(list(tmp_path.glob("*.lock"))) == 2


def test_local_wildcard_and_loopback_share_one_lock_scope(tmp_path: Path) -> None:
    first = SingleInstanceLock("127.0.0.1", 8000, lock_dir=tmp_path)
    overlapping = SingleInstanceLock("0.0.0.0", 8000, lock_dir=tmp_path)

    with first:
        with pytest.raises(AlreadyRunningError):
            overlapping.acquire()


def test_dashboard_lock_config_prefers_explicit_environment() -> None:
    config = resolve_dashboard_lock_config(
        app_env="development",
        environ={
            "JOB_AGENT_API_HOST": "0.0.0.0",
            "JOB_AGENT_API_PORT": "8123",
            "PORT": "9999",
        },
        argv=["--host", "localhost", "--port", "7000"],
    )

    assert config.enabled is True
    assert config.host == "0.0.0.0"
    assert config.port == 8123


def test_dashboard_lock_config_reads_direct_uvicorn_arguments() -> None:
    config = resolve_dashboard_lock_config(
        app_env="development",
        environ={},
        argv=["api.main:app", "--host=localhost", "--port", "8124"],
    )

    assert config.enabled is True
    assert config.host == "localhost"
    assert config.port == 8124


@pytest.mark.parametrize(
    ("app_env", "environ"),
    [
        ("test", {}),
        ("development", {"PYTEST_CURRENT_TEST": "active"}),
        ("production", {"VERCEL": "1"}),
        ("production", {"AWS_LAMBDA_FUNCTION_NAME": "dashboard"}),
        ("development", {"JOB_AGENT_INSTANCE_LOCK": "false"}),
    ],
)
def test_dashboard_lock_skips_test_serverless_and_explicit_disable(
    app_env: str,
    environ: dict[str, str],
) -> None:
    config = resolve_dashboard_lock_config(
        app_env=app_env,
        environ=environ,
        argv=[],
    )

    assert config.enabled is False


def test_dashboard_lock_integration_acquires_configured_endpoint(tmp_path: Path) -> None:
    environment = {
        "JOB_AGENT_API_HOST": "127.0.0.1",
        "JOB_AGENT_API_PORT": "8125",
        "JOB_AGENT_INSTANCE_LOCK_DIR": str(tmp_path),
    }
    first = acquire_dashboard_instance_lock(
        app_env="development",
        boot_id="first",
        environ=environment,
        argv=[],
    )
    assert first is not None
    try:
        with pytest.raises(AlreadyRunningError):
            acquire_dashboard_instance_lock(
                app_env="development",
                boot_id="second",
                environ=environment,
                argv=[],
            )
    finally:
        first.release()


async def test_api_lifespan_holds_and_releases_instance_lock(monkeypatch) -> None:
    import api.main as api_main

    instance_lock = MagicMock()
    acquire = MagicMock(return_value=instance_lock)
    monkeypatch.setattr(api_main, "acquire_dashboard_instance_lock", acquire)
    monkeypatch.setattr(api_main, "init_db", MagicMock())

    async with api_main.lifespan(api_main.app):
        instance_lock.release.assert_not_called()

    acquire.assert_called_once()
    instance_lock.release.assert_called_once_with()


async def test_api_lifespan_releases_lock_when_initialization_fails(monkeypatch) -> None:
    import api.main as api_main

    instance_lock = MagicMock()
    monkeypatch.setattr(
        api_main,
        "acquire_dashboard_instance_lock",
        MagicMock(return_value=instance_lock),
    )
    monkeypatch.setattr(
        api_main,
        "init_db",
        MagicMock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        async with api_main.lifespan(api_main.app):
            pytest.fail("lifespan must not yield after failed initialization")

    instance_lock.release.assert_called_once_with()


def test_canonical_dashboard_launcher_exports_endpoint_to_lifespan(monkeypatch) -> None:
    from scripts import start_dashboard

    run = MagicMock()
    monkeypatch.setattr(start_dashboard.uvicorn, "run", run)

    start_dashboard.main(["--host", "0.0.0.0", "--port", "8126", "--reload"])

    assert os.environ["JOB_AGENT_API_HOST"] == "0.0.0.0"
    assert os.environ["JOB_AGENT_API_PORT"] == "8126"
    assert os.environ["JOB_AGENT_INSTANCE_LOCK"] == "true"
    run.assert_called_once_with(
        "api.main:app",
        host="0.0.0.0",
        port=8126,
        reload=True,
    )
