from __future__ import annotations

import re
from pathlib import Path

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "JobAgent.Runtime.psm1"
CLI = ROOT / "scripts" / "job_agent.ps1"
INSTALL = ROOT / "scripts" / "install_control_plane_runner.ps1"
UNINSTALL = ROOT / "scripts" / "uninstall_control_plane_runner.ps1"
STATUS = ROOT / "scripts" / "control_plane_runner_status.ps1"
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
BOOTSTRAP_DOC = ROOT / "docs" / "control-plane-bootstrap.md"


def test_windows_runtime_requires_supported_pwsh_explicitly() -> None:
    for path in (MODULE, CLI, INSTALL, UNINSTALL, STATUS):
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        assert first_line == "#Requires -Version 7.2"

    bootstrap = BOOTSTRAP_DOC.read_text(encoding="utf-8")
    assert "PowerShell 7.2 or newer" in bootstrap
    assert "pwsh -NoProfile -File .\\scripts\\job_agent.ps1 bootstrap" in bootstrap
    for command in ("start", "status", "open", "stop"):
        assert f"pwsh -NoProfile -File .\\scripts\\job_agent.ps1 {command}" in bootstrap


def test_windows_runtime_entrypoints_and_fail_closed_contract_are_present() -> None:
    module = MODULE.read_text(encoding="utf-8")
    cli = CLI.read_text(encoding="utf-8")

    assert "bootstrap', 'start', 'status', 'open', 'stop" in cli
    assert "SupportsShouldProcess = $true" in cli
    assert "JobAgent.Runtime.psm1" in cli
    assert "%LOCALAPPDATA%" not in module
    assert "Join-Path $localRoot 'JobApplyAgent'" in module
    for setting in (
        "DRY_RUN = 'true'",
        "DRAFT_ONLY = 'true'",
        "AUTO_APPLY = 'false'",
        "PORTAL_FINAL_SUBMIT_ENABLED = 'false'",
        "LIVE_AUTOMATION_ACKNOWLEDGED = 'false'",
    ):
        assert setting in module
    assert "http://127.0.0.1:" in module
    assert "DASHBOARD_URL_NOT_EXACT_LOOPBACK" in module
    assert "JOB_AGENT_COMMAND_ALREADY_RUNNING" in module
    assert "REPOSITORY_NOT_CLEAN" in module
    assert "RELEASE_NOT_MAIN_DERIVED" in module
    assert "RUNTIME_RELEASE_STALE" in module
    assert "UpgradeRelease" in cli
    assert "EnterpriseCaCertificatePath" in cli
    assert "EnterpriseCaSha256" in cli
    assert "ENTERPRISE_CA_PATH_AND_SHA256_REQUIRED" in cli
    assert "Install-JobAgentEnterpriseBuildCa" in module
    assert "JOB_AGENT_ENTERPRISE_CA_FILE" in module
    assert "Update-JobAgentRuntimeRelease" in module
    assert "[System.IO.File]::Move($temporary, $runtimePath, $true)" in module
    assert "-RequireClean" in module
    assert "-RequireMain" in module
    assert "Assert-JobAgentExternalLayout" in module
    assert "EXTERNAL_ROOT_REPARSE_POINT" in module


def test_browser_open_requires_stable_release_and_occurs_once() -> None:
    module = MODULE.read_text(encoding="utf-8")

    for field in (
        "'build_sha'",
        "'ui_asset_digest'",
        "'source_digest'",
        "'protocol_version'",
        "'boot_id'",
        "'started_at'",
    ):
        assert field in module
    assert "/health/live" in module
    assert "/health/ready" in module
    assert "/api/runtime/capabilities" in module
    assert "worker.compatible" in module
    browser_function = module.split("function Open-JobAgentDashboard", maxsplit=1)[1].split(
        "function Get-JobAgentIdentitySelection",
        maxsplit=1,
    )[0]
    assert browser_function.count("& $BrowserLauncher $DashboardUrl") == 1
    assert browser_function.index("Wait-JobAgentStableRuntime") < browser_function.index(
        "& $BrowserLauncher $DashboardUrl"
    )


def test_task_scripts_require_exact_marker_ownership_and_no_unrestricted_replace() -> None:
    install = INSTALL.read_text(encoding="utf-8")
    uninstall = UNINSTALL.read_text(encoding="utf-8")
    status = STATUS.read_text(encoding="utf-8")

    assert "RunnerTaskOwnershipMarker" in install
    assert "LegacyAdoptable" in install
    assert "AdoptExisting" in install
    assert "OwnedDrifted" in install
    assert "RepairOwned" in install
    assert re.search(r"\bReplace\b", install) is None
    assert "FOREIGN_TASK_CONFLICT" in install
    assert "RUNNER_TASK_ACTIVE_DRIFT_REPAIR_REFUSED" in install
    assert install.index("Stop-ScheduledTask") < install.index("Register-ScheduledTask")
    assert "OwnedExact" in uninstall
    assert "TASK_REMOVAL_REFUSED_" in uninstall
    assert "Get-JobAgentTaskOwnership" in status
    assert "Unregister-ScheduledTask" not in MODULE.read_text(encoding="utf-8")
    module = MODULE.read_text(encoding="utf-8")
    for task_field in (
        "LogonType",
        "Triggers",
        "Enabled",
        "MultipleInstances",
        "RestartCount",
        "RestartInterval",
        "ExecutionTimeLimit",
        "StartWhenAvailable",
    ):
        assert task_field in module


def test_compose_parent_environment_is_sanitized_and_restored() -> None:
    module = MODULE.read_text(encoding="utf-8")
    compose_function = module.split("function Invoke-JobAgentCompose", maxsplit=1)[1].split(
        "function Get-JobAgentComposeContainers",
        maxsplit=1,
    )[0]

    assert "Read-JobAgentRuntimeEnvironment" in compose_function
    assert "Assert-JobAgentSafeRuntimeEnvironment" in compose_function
    assert "SetEnvironmentVariable" in compose_function
    assert "COMPOSE_FILE" in compose_function
    for transport_name in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
    ):
        assert transport_name in compose_function
    assert "finally" in compose_function
    compose_arguments = module.split(
        "function Get-JobAgentComposeArguments",
        maxsplit=1,
    )[1].split("function Invoke-JobAgentCompose", maxsplit=1)[0]
    assert "'--context'" in compose_arguments
    assert "'default'" in compose_arguments


def test_endpoint_ownership_requires_running_web_container_and_live_process() -> None:
    module = MODULE.read_text(encoding="utf-8")
    endpoint_function = module.split(
        "function Get-JobAgentEndpointOwnership",
        maxsplit=1,
    )[1].split("function Get-JobAgentComposeArguments", maxsplit=1)[0]

    assert "OwningProcess" in endpoint_function
    assert "'State'" in endpoint_function
    assert "'running'" in endpoint_function
    assert "runningWeb.Count -ne 1" in endpoint_function
    assert "AuthenticatedRuntimeVerified" in endpoint_function
    assert "MetadataMatched" in endpoint_function
    assert "AuthenticatedRuntimeIdentity" in endpoint_function
    assert "'Unverifiable'" in endpoint_function


def test_open_reuses_release_process_and_endpoint_ownership_preflight() -> None:
    module = MODULE.read_text(encoding="utf-8")
    open_function = module.split("function Invoke-JobAgentOpen", maxsplit=1)[1].split(
        "function Invoke-JobAgentStop",
        maxsplit=1,
    )[0]

    for contract in (
        "-RequireClean",
        "-RequireMain",
        "Assert-JobAgentRuntimeRelease",
        "Get-JobAgentTaskState",
        "Get-JobAgentComposeOwnership",
        "Get-JobAgentEndpointOwnership",
        "OwnedExact",
        "Running",
        "Test-JobAgentStableRuntime",
        "AuthenticatedRuntimeVerified",
    ):
        assert contract in open_function
    first_ownership = open_function.index("Get-JobAgentEndpointOwnership")
    authenticated_probe = open_function.index("Test-JobAgentStableRuntime")
    proof_binding = open_function.index("-AuthenticatedRuntimeVerified")
    verified_ownership = open_function.rfind(
        "Get-JobAgentEndpointOwnership",
        0,
        proof_binding,
    )
    browser_open = open_function.rindex("Open-JobAgentDashboard")
    assert first_ownership < authenticated_probe < verified_ownership < proof_binding < browser_open


def test_start_and_status_require_authenticated_endpoint_proof() -> None:
    module = MODULE.read_text(encoding="utf-8")
    ranges = (
        module.split("function Get-JobAgentLocalStatus", maxsplit=1)[1].split(
            "function Invoke-JobAgentStart",
            maxsplit=1,
        )[0],
        module.split("function Invoke-JobAgentStart", maxsplit=1)[1].split(
            "function Invoke-JobAgentOpen",
            maxsplit=1,
        )[0],
    )

    for function_text in ranges:
        first_ownership = function_text.index("Get-JobAgentEndpointOwnership")
        authenticated_probe = function_text.index("Test-JobAgentStableRuntime")
        proof_binding = function_text.index("-AuthenticatedRuntimeVerified")
        verified_ownership = function_text.rfind(
            "Get-JobAgentEndpointOwnership",
            0,
            proof_binding,
        )
        assert first_ownership < authenticated_probe < verified_ownership < proof_binding


def test_status_preserves_unbound_foreign_and_unverifiable_classifications() -> None:
    module = MODULE.read_text(encoding="utf-8")
    status_function = module.split("function Get-JobAgentLocalStatus", maxsplit=1)[1].split(
        "function Invoke-JobAgentStart",
        maxsplit=1,
    )[0]

    assert "OwnedUnverifiable" in status_function
    assert "'Foreign'" in status_function
    assert "IdentityState" in status_function
    assert "EndpointOwnership" in status_function


def test_compose_uses_only_loopback_ports_and_external_private_mounts() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    env_text = ENV_EXAMPLE.read_text(encoding="utf-8")
    host_environment = dotenv_values(ENV_EXAMPLE)

    assert compose["name"] == "job-apply-agent"
    assert "LINKEDIN_BROWSER_PROFILE_DIR=${JOB_AGENT_BROWSER_STATE_DIR}/linkedin" in env_text
    assert "PORTAL_BROWSER_PROFILE_ROOT=${JOB_AGENT_BROWSER_STATE_DIR}/portals" in env_text
    browser_state = host_environment["JOB_AGENT_BROWSER_STATE_DIR"]
    assert host_environment["LINKEDIN_BROWSER_PROFILE_DIR"] == f"{browser_state}/linkedin"
    assert host_environment["PORTAL_BROWSER_PROFILE_ROOT"] == f"{browser_state}/portals"
    for service in compose["services"].values():
        for port in service.get("ports", []):
            assert str(port).startswith("127.0.0.1:")
    for service_name in ("web-api", "celery-worker", "celery-beat"):
        service = compose["services"][service_name]
        assert service["env_file"] == "${JOB_AGENT_ENV_FILE:-.env}"
        assert service["environment"]["JOB_AGENT_ENV_FILE"] == ""
        assert (
            service["environment"]["EMPLOYER_CATALOG_PATH"]
            == "/app/profile-data/employer_catalog.yaml"
        )
        assert (
            service["environment"]["GMAIL_OAUTH_TOKEN_PATH"]
            == "/app/profile-data/.gmail_oauth.json"
        )
        mounts = "\n".join(service.get("volumes", []))
        assert "${JOB_AGENT_PROFILE_DATA_DIR:-./profile-data}" in mounts
        assert "${JOB_AGENT_BROWSER_STATE_DIR:-./.browser-state}" in mounts
    for service_name in ("celery-worker", "celery-beat"):
        profile_mount = next(
            mount
            for mount in compose["services"][service_name]["volumes"]
            if "${JOB_AGENT_PROFILE_DATA_DIR:-./profile-data}" in mount
        )
        assert profile_mount.endswith(":ro")
    web_environment = compose["services"]["web-api"]["environment"]
    worker_environment = compose["services"]["celery-worker"]["environment"]
    for environment in (web_environment, worker_environment):
        assert environment["USER_PROFILE_PATH"] == "/app/profile-data/user_profile.yaml"
        assert environment["CV_DIRECTORY"] == "/app/profile-data/cvs"
        assert environment["LINKEDIN_BROWSER_PROFILE_DIR"] == "/app/browser-state/linkedin"
        assert environment["PORTAL_BROWSER_PROFILE_ROOT"] == "/app/browser-state/portals"


def test_stop_preserves_data_and_requires_exact_compose_ownership() -> None:
    module = MODULE.read_text(encoding="utf-8")
    stop_function = module.split("function Invoke-JobAgentStop", maxsplit=1)[1].split(
        "Export-ModuleMember",
        maxsplit=1,
    )[0]

    assert "COMPOSE_PROJECT_NOT_OWNED_EXACT" in stop_function
    assert "RUNNER_TASK_NOT_OWNED_EXACT" in stop_function
    assert "@('down', '--remove-orphans')" in stop_function
    assert "--volumes" not in stop_function
    assert "DataVolumesPreserved = $true" in stop_function
    assert "Get-JobAgentIdentitySelection" not in stop_function
    assert "RunnerConfigPath" not in stop_function
    assert "Get-JobAgentTaskState" not in stop_function
    assert "Get-JobAgentEmergencyTaskState" in stop_function
    assert "Stop-JobAgentEmergencyRunnerTask" in stop_function
    assert "Assert-JobAgentEmergencyTaskTarget" in stop_function
    assert "RUNNER_TASK_TARGET_NOT_CANONICAL" in module
    assert "'MarkerOwned'" in stop_function
    assert stop_function.count("Get-JobAgentEmergencyTaskState") >= 3
    assert stop_function.count("Get-JobAgentComposeContainers") >= 3
    assert "RUNNER_TASK_STOP_UNCONFIRMED" in stop_function
    assert "$finalTaskState -notin @('Ready', 'Disabled')" in stop_function
    assert "COMPOSE_PROJECT_STOP_UNCONFIRMED" in stop_function
    assert "$finalCompose.Classification -ne 'Absent'" in stop_function


def test_emergency_stop_uses_only_task_marker_and_compose_labels() -> None:
    module = MODULE.read_text(encoding="utf-8")
    task_ownership = module.split(
        "function Get-JobAgentEmergencyTaskOwnership",
        maxsplit=1,
    )[1].split("function ConvertFrom-JobAgentComposeLabels", maxsplit=1)[0]
    task_stop = module.split(
        "function Stop-JobAgentEmergencyRunnerTask",
        maxsplit=1,
    )[1].split("function Get-JobAgentLocalStatus", maxsplit=1)[0]
    task_state = module.split(
        "function Get-JobAgentEmergencyTaskState",
        maxsplit=1,
    )[1].split("function Stop-JobAgentEmergencyRunnerTask", maxsplit=1)[0]

    assert "RunnerTaskOwnershipMarker" in task_ownership
    assert "'MarkerOwned'" in task_ownership
    assert "'Foreign'" in task_ownership
    assert "Get-JobAgentIdentitySelection" not in task_ownership
    assert "RunnerConfigPath" not in task_ownership
    assert "Stop-ScheduledTask" in task_stop
    assert "-InputObject $state.Task -ErrorAction Stop" in task_stop
    assert "Get-JobAgentEmergencyTaskState" in task_stop
    assert "Assert-JobAgentEmergencyTaskTarget" in task_stop
    assert "Get-JobAgentIdentitySelection" not in task_stop
    assert "Get-ScheduledTask -ErrorAction Stop" in task_state
    assert "ErrorAction SilentlyContinue" not in task_state
    assert "RunnerTaskName" in task_state
    assert "RunnerTaskPath" in task_state
    assert "RUNNER_TASK_QUERY_FAILED" in task_state
    assert "RUNNER_TASK_PATH_NOT_CANONICAL" in task_state
