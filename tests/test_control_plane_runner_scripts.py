"""Behavioral checks for the private-runner Windows task scripts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_control_plane_runner.ps1"
RUNTIME_MODULE = ROOT / "scripts" / "JobAgent.Runtime.psm1"


def _powershell() -> str:
    for executable in ("pwsh", "powershell"):
        try:
            probe = subprocess.run(
                [executable, "-NoProfile", "-NonInteractive", "-Command", "exit 0"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        if probe.returncode == 0:
            return executable
    pytest.skip("PowerShell is unavailable")


def _ps_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def test_installer_whatif_without_existing_task_returns_simulated_state(tmp_path):
    repository = tmp_path / "repository"
    worker = repository / "worker"
    worker.mkdir(parents=True)
    (worker / "control_plane_runner.py").write_text("# test fixture\n", encoding="utf-8")

    private_key = tmp_path / "runner.key"
    public_key = tmp_path / "control.pub"
    private_key.write_text("private-key-fixture", encoding="ascii")
    public_key.write_text("public-key-fixture", encoding="ascii")
    local_app_data = tmp_path / "local-app-data"
    runtime_env = local_app_data / "JobApplyAgent" / "runtime" / "runtime.env"
    runtime_env.parent.mkdir(parents=True)
    generated = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                f"Import-Module {_ps_literal(RUNTIME_MODULE)} -Force; "
                f"$layout = Get-JobAgentLayout -LocalAppDataRoot "
                f"{_ps_literal(local_app_data.resolve())}; "
                "New-JobAgentRuntimeEnvironmentText "
                "-Layout $layout -BuildSha ('a' * 40)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    runtime_env.write_text(generated.stdout, encoding="utf-8")
    config = tmp_path / "runner.json"
    config.write_text(
        json.dumps(
            {
                "private_key_path": str(private_key.resolve()),
                "control_plane_public_key_path": str(public_key.resolve()),
                "runtime_env_path": str(runtime_env.resolve()),
            }
        ),
        encoding="utf-8",
    )

    command = f"""
        function Get-ScheduledTask {{
            [CmdletBinding()]
            param([string]$TaskName)
            return $null
        }}
        function New-ScheduledTaskAction {{
            param($Execute, $Argument, $WorkingDirectory)
            return [pscustomobject]@{{ Kind = 'Action' }}
        }}
        function New-ScheduledTaskTrigger {{
            param([switch]$AtLogOn, $User)
            return [pscustomobject]@{{ Kind = 'Trigger' }}
        }}
        function New-ScheduledTaskPrincipal {{
            param($UserId, $LogonType, $RunLevel)
            return [pscustomobject]@{{ Kind = 'Principal' }}
        }}
        function New-ScheduledTaskSettingsSet {{
            param(
                $MultipleInstances,
                $RestartCount,
                $RestartInterval,
                $ExecutionTimeLimit,
                [switch]$StartWhenAvailable
            )
            return [pscustomobject]@{{ Kind = 'Settings' }}
        }}
        function Register-ScheduledTask {{ throw 'Register must not run under WhatIf' }}
        function Start-ScheduledTask {{ throw 'Start must not run under WhatIf' }}
        $result = & {_ps_literal(INSTALLER)} `
            -RepositoryPath {_ps_literal(repository.resolve())} `
            -PythonExecutable {_ps_literal(Path(sys.executable).resolve())} `
            -ConfigPath {_ps_literal(config.resolve())} `
            -TaskName 'JobApplyAgent-WhatIf-Test' `
            -WhatIf
        '__RESULT__' + ($result | ConvertTo-Json -Compress)
    """
    completed = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    marker = next(
        line.removeprefix("__RESULT__")
        for line in completed.stdout.splitlines()
        if line.startswith("__RESULT__")
    )
    result = json.loads(marker)
    assert result["TaskName"] == "JobApplyAgent-WhatIf-Test"
    assert result["Classification"] == "Absent"
    assert result["State"] == "NotInstalled"
    assert result["Result"] == "WhatIf"
    assert result["Applied"] is False
