#Requires -Version 7.2

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
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
        Removed = $false
        Classification = 'Absent'
        Reason = 'NotInstalled'
    }
    return
}
if ($ownership.Classification -ne 'OwnedExact') {
    throw "TASK_REMOVAL_REFUSED_$($ownership.Classification)"
}
if (-not $PSCmdlet.ShouldProcess(
    $TaskName,
    'Stop and unregister exact marker-owned private runner task'
)) {
    [pscustomobject]@{
        TaskName = $TaskName
        Removed = $false
        Classification = $ownership.Classification
        Reason = 'WhatIf'
    }
    return
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
[pscustomobject]@{
    TaskName = $TaskName
    Removed = $true
    Classification = $ownership.Classification
}
