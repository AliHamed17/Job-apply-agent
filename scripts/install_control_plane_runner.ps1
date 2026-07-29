#Requires -Version 7.2

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryPath,

    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [ValidatePattern('^[A-Za-z0-9_.-]{1,64}$')]
    [string]$TaskName = 'JobApplyAgent-PrivateRunner',

    [switch]$AdoptExisting,

    [switch]$RepairOwned,

    [switch]$NoStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'JobAgent.Runtime.psm1') -Force -ErrorAction Stop
$constants = Get-JobAgentRuntimeConstants

function Resolve-RequiredFile {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $resolved = ConvertTo-JobAgentCanonicalPath -LiteralPath $LiteralPath -RequireExisting
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw 'REQUIRED_FILE_UNAVAILABLE'
    }
    return $resolved
}

function Resolve-RequiredDirectory {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $resolved = ConvertTo-JobAgentCanonicalPath -LiteralPath $LiteralPath -RequireExisting
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw 'REQUIRED_DIRECTORY_UNAVAILABLE'
    }
    return $resolved
}

$repository = Resolve-RequiredDirectory -LiteralPath $RepositoryPath
$python = Resolve-RequiredFile -LiteralPath $PythonExecutable
$config = Resolve-RequiredFile -LiteralPath $ConfigPath
$runnerModule = Join-Path (Join-Path $repository 'worker') 'control_plane_runner.py'
if (-not (Test-Path -LiteralPath $runnerModule -PathType Leaf)) {
    throw 'RUNNER_MODULE_UNAVAILABLE'
}
if (Test-JobAgentPathWithin -ChildPath $config -ParentPath $repository) {
    throw 'RUNNER_CONFIG_NOT_EXTERNAL'
}

try {
    $configuration = Get-Content -LiteralPath $config -Raw -Encoding UTF8 |
        ConvertFrom-Json -ErrorAction Stop
}
catch {
    throw 'RUNNER_CONFIG_INVALID'
}
foreach ($property in @(
    'private_key_path',
    'control_plane_public_key_path',
    'runtime_env_path'
)) {
    $value = [string]$configuration.$property
    if (
        [string]::IsNullOrWhiteSpace($value) -or
        -not [System.IO.Path]::IsPathFullyQualified($value)
    ) {
        throw "RUNNER_CONFIG_PATH_INVALID_$property"
    }
    $externalPath = Resolve-RequiredFile -LiteralPath $value
    if (Test-JobAgentPathWithin -ChildPath $externalPath -ParentPath $repository) {
        throw "RUNNER_CONFIG_PATH_NOT_EXTERNAL_$property"
    }
    if ($property -eq 'private_key_path') {
        foreach ($oneDriveVariable in @(
            'OneDrive',
            'OneDriveCommercial',
            'OneDriveConsumer'
        )) {
            $oneDriveRoot = [Environment]::GetEnvironmentVariable($oneDriveVariable)
            if (
                -not [string]::IsNullOrWhiteSpace($oneDriveRoot) -and
                (Test-JobAgentPathWithin -ChildPath $externalPath -ParentPath $oneDriveRoot)
            ) {
                throw 'RUNNER_PRIVATE_KEY_IN_ONEDRIVE'
            }
        }
    }
}

$runtimeEnv = Read-JobAgentRuntimeEnvironment -Path ([string]$configuration.runtime_env_path)
$layoutRoot = Split-Path -Parent (Split-Path -Parent ([string]$configuration.runtime_env_path))
$layout = Get-JobAgentLayout -LocalAppDataRoot (Split-Path -Parent $layoutRoot)
Assert-JobAgentSafeRuntimeEnvironment -Values $runtimeEnv -Layout $layout | Out-Null

$expected = Get-JobAgentExpectedTaskAction `
    -RepositoryPath $repository `
    -PythonExecutable $python `
    -ConfigPath $config
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$currentUser = ''
if ($null -ne $existing) {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}
$ownership = Get-JobAgentTaskOwnership `
    -Task $existing `
    -ExpectedAction $expected `
    -ExpectedUser $currentUser

switch ($ownership.Classification) {
    'Foreign' {
        throw 'FOREIGN_TASK_CONFLICT'
    }
    'LegacyAdoptable' {
        if (-not $AdoptExisting) {
            throw 'EXISTING_TASK_REQUIRES_EXPLICIT_ADOPTION'
        }
    }
    'OwnedDrifted' {
        if (-not $RepairOwned) {
            throw 'OWNED_TASK_DRIFT_REQUIRES_EXPLICIT_REPAIR'
        }
    }
    'OwnedExact' {
        if ($NoStart -or [string]$existing.State -eq 'Running') {
            [pscustomobject]@{
                TaskName = $TaskName
                Classification = $ownership.Classification
                Applied = $false
                State = [string]$existing.State
                Result = 'AlreadyOwned'
            }
            return
        }
        if ($PSCmdlet.ShouldProcess($TaskName, 'Start exact owned private runner task')) {
            Start-ScheduledTask -TaskName $TaskName
            [pscustomobject]@{
                TaskName = $TaskName
                Classification = $ownership.Classification
                Applied = $true
                State = 'StartRequested'
                Result = 'StartedOwned'
            }
        }
        else {
            [pscustomobject]@{
                TaskName = $TaskName
                Classification = $ownership.Classification
                Applied = $false
                State = [string]$existing.State
                Result = 'WhatIf'
            }
        }
        return
    }
}

$operation = if ($ownership.Classification -eq 'Absent') {
    'Register owned private runner task'
}
elseif ($ownership.Classification -eq 'LegacyAdoptable') {
    'Adopt exact legacy private runner task'
}
else {
    'Repair marker-owned private runner task'
}

if (-not $PSCmdlet.ShouldProcess($TaskName, $operation)) {
    [pscustomobject]@{
        TaskName = $TaskName
        Classification = $ownership.Classification
        Applied = $false
        State = if ($existing) { [string]$existing.State } else { 'NotInstalled' }
        Result = 'WhatIf'
    }
    return
}

if ([string]::IsNullOrWhiteSpace($currentUser)) {
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

if ($null -ne $existing -and [string]$existing.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName
    $stopped = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        if ([string]$existing.State -ne 'Running') {
            $stopped = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $stopped) {
        throw 'RUNNER_TASK_ACTIVE_DRIFT_REPAIR_REFUSED'
    }
}

$action = New-ScheduledTaskAction `
    -Execute $expected.Execute `
    -Argument $expected.Arguments `
    -WorkingDirectory $expected.WorkingDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -Disable:$false `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -StartWhenAvailable
$registration = @{
    TaskName = $TaskName
    Action = $action
    Trigger = $trigger
    Principal = $principal
    Settings = $settings
    Description = $constants.RunnerTaskOwnershipMarker
}
if ($ownership.Classification -ne 'Absent') {
    $registration['Force'] = $true
}
Register-ScheduledTask @registration | Out-Null
if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
}
$installed = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$installedOwnership = Get-JobAgentTaskOwnership `
    -Task $installed `
    -ExpectedAction $expected `
    -ExpectedUser $currentUser
if ($installedOwnership.Classification -ne 'OwnedExact') {
    throw 'RUNNER_TASK_POSTCONDITION_FAILED'
}

[pscustomobject]@{
    TaskName = $TaskName
    Classification = $installedOwnership.Classification
    Applied = $true
    State = [string]$installed.State
    Result = $operation
    Started = -not $NoStart
}
