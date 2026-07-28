[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9_.-]{1,64}$')]
    [string]$TaskName = 'JobApplyAgent-PrivateRunner'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    [pscustomobject]@{
        TaskName = $TaskName
        Installed = $false
        State = 'NotInstalled'
    }
    exit 1
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{
    TaskName = $TaskName
    Installed = $true
    State = [string]$task.State
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    NextRunTime = $info.NextRunTime
}
