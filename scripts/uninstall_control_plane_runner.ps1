[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
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
        Removed = $false
        Reason = 'NotInstalled'
    }
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, 'Stop and unregister private control-plane runner')) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    [pscustomobject]@{
        TaskName = $TaskName
        Removed = $true
    }
}
