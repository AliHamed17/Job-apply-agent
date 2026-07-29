#Requires -Version 7.2

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('bootstrap', 'start', 'status', 'open', 'stop')]
    [string]$Command,

    [string]$RepositoryPath = (Split-Path -Parent $PSScriptRoot),

    [string]$ControlPlaneUrl,

    [ValidatePattern('^prj_[A-Za-z0-9]{8,120}$')]
    [string]$VercelProjectId,

    [ValidatePattern('^team_[A-Za-z0-9]{8,120}$')]
    [string]$VercelScopeId,

    [string]$PythonExecutable = (Get-Command python -ErrorAction Stop).Source,

    [string]$LocalAppDataRoot = $env:LOCALAPPDATA,

    [ValidatePattern('^[A-Za-z0-9_.-]{1,64}$')]
    [string]$TaskName = 'JobApplyAgent-PrivateRunner',

    [ValidateRange(10, 900)]
    [int]$TimeoutSeconds = 300,

    [switch]$AdoptExistingTask,

    [switch]$RepairOwnedTask,

    [switch]$UpgradeRelease
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$modulePath = Join-Path $PSScriptRoot 'JobAgent.Runtime.psm1'
Import-Module $modulePath -Force -ErrorAction Stop

$common = @{
    RepositoryPath = $RepositoryPath
    LocalAppDataRoot = $LocalAppDataRoot
}

switch ($Command) {
    'bootstrap' {
        if ([string]::IsNullOrWhiteSpace($ControlPlaneUrl)) {
            throw 'CONTROL_PLANE_URL_REQUIRED'
        }
        if ([string]::IsNullOrWhiteSpace($VercelProjectId)) {
            throw 'VERCEL_PROJECT_ID_REQUIRED'
        }
        if ([string]::IsNullOrWhiteSpace($VercelScopeId)) {
            throw 'VERCEL_SCOPE_ID_REQUIRED'
        }
        $arguments = @{
            RepositoryPath = $RepositoryPath
            ControlPlaneUrl = $ControlPlaneUrl
            VercelProjectId = $VercelProjectId
            VercelScopeId = $VercelScopeId
            PythonExecutable = $PythonExecutable
            LocalAppDataRoot = $LocalAppDataRoot
            TaskName = $TaskName
            Confirm = $false
        }
        if ($AdoptExistingTask) {
            $arguments['AdoptExistingTask'] = $true
        }
        if ($RepairOwnedTask) {
            $arguments['RepairOwnedTask'] = $true
        }
        if ($UpgradeRelease) {
            $arguments['UpgradeRelease'] = $true
        }
        if ($WhatIfPreference) {
            $arguments['WhatIf'] = $true
        }
        Invoke-JobAgentBootstrap @arguments
    }
    'start' {
        $arguments = @{
            RepositoryPath = $RepositoryPath
            PythonExecutable = $PythonExecutable
            LocalAppDataRoot = $LocalAppDataRoot
            TaskName = $TaskName
            TimeoutSeconds = $TimeoutSeconds
            Confirm = $false
        }
        if ($WhatIfPreference) {
            $arguments['WhatIf'] = $true
        }
        Invoke-JobAgentStart @arguments
    }
    'status' {
        Get-JobAgentLocalStatus `
            @common `
            -PythonExecutable $PythonExecutable `
            -TaskName $TaskName
    }
    'open' {
        $arguments = @{
            RepositoryPath = $RepositoryPath
            PythonExecutable = $PythonExecutable
            LocalAppDataRoot = $LocalAppDataRoot
            TaskName = $TaskName
            TimeoutSeconds = $TimeoutSeconds
            Confirm = $false
        }
        if ($WhatIfPreference) {
            $arguments['WhatIf'] = $true
        }
        Invoke-JobAgentOpen @arguments
    }
    'stop' {
        $arguments = @{
            RepositoryPath = $RepositoryPath
            PythonExecutable = $PythonExecutable
            LocalAppDataRoot = $LocalAppDataRoot
            TaskName = $TaskName
            Confirm = $false
        }
        if ($WhatIfPreference) {
            $arguments['WhatIf'] = $true
        }
        Invoke-JobAgentStop @arguments
    }
}
