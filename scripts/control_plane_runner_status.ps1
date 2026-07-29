#Requires -Version 7.2

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryPath,

    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [ValidatePattern('^[A-Za-z0-9_.-]{1,64}$')]
    [string]$TaskName = 'JobApplyAgent-PrivateRunner'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'JobAgent.Runtime.psm1') -Force -ErrorAction Stop
$expected = Get-JobAgentExpectedTaskAction `
    -RepositoryPath $RepositoryPath `
    -PythonExecutable $PythonExecutable `
    -ConfigPath $ConfigPath
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$ownership = Get-JobAgentTaskOwnership `
    -Task $task `
    -ExpectedAction $expected `
    -ExpectedUser $currentUser

if ($ownership.Classification -eq 'Absent') {
    [pscustomobject]@{
        TaskName = $TaskName
        Installed = $false
        Classification = 'Absent'
        State = 'NotInstalled'
    }
    return
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
[pscustomobject]@{
    TaskName = $TaskName
    Installed = $true
    Classification = $ownership.Classification
    Owned = $ownership.Owned
    Exact = $ownership.Exact
    State = [string]$task.State
    LastRunTime = if ($info) { $info.LastRunTime } else { $null }
    LastTaskResult = if ($info) { $info.LastTaskResult } else { $null }
    NextRunTime = if ($info) { $info.NextRunTime } else { $null }
}
