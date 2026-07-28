[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepositoryPath,

    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [ValidatePattern('^[A-Za-z0-9_.-]{1,64}$')]
    [string]$TaskName = 'JobApplyAgent-PrivateRunner',

    [switch]$Replace
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-RequiredFile {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $resolved = (Resolve-Path -LiteralPath $LiteralPath -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
        throw "Required file is unavailable."
    }
    return [System.IO.Path]::GetFullPath($resolved)
}

function Resolve-RequiredDirectory {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $resolved = (Resolve-Path -LiteralPath $LiteralPath -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) {
        throw "Required directory is unavailable."
    }
    return [System.IO.Path]::GetFullPath($resolved)
}

function Test-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$ChildPath,
        [Parameter(Mandatory = $true)][string]$ParentPath
    )

    $parent = [System.IO.Path]::GetFullPath($ParentPath).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    $child = [System.IO.Path]::GetFullPath($ChildPath)
    return $child.StartsWith($parent, [System.StringComparison]::OrdinalIgnoreCase)
}

$repository = Resolve-RequiredDirectory -LiteralPath $RepositoryPath
$python = Resolve-RequiredFile -LiteralPath $PythonExecutable
$config = Resolve-RequiredFile -LiteralPath $ConfigPath

$runnerModule = Join-Path (Join-Path $repository 'worker') 'control_plane_runner.py'
if (-not (Test-Path -LiteralPath $runnerModule -PathType Leaf)) {
    throw "Repository does not contain the private control-plane runner."
}
if (Test-PathWithin -ChildPath $config -ParentPath $repository) {
    throw "Runner configuration must remain outside the repository."
}

$configuration = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
foreach ($property in @('private_key_path', 'control_plane_public_key_path')) {
    $value = [string]$configuration.$property
    if ([string]::IsNullOrWhiteSpace($value) -or -not [System.IO.Path]::IsPathFullyQualified($value)) {
        throw "Runner key paths must be absolute."
    }
    $keyPath = Resolve-RequiredFile -LiteralPath $value
    if (Test-PathWithin -ChildPath $keyPath -ParentPath $repository) {
        throw "Runner key files must remain outside the repository."
    }
    if ($property -eq 'private_key_path') {
        foreach ($oneDriveVariable in @('OneDrive', 'OneDriveCommercial', 'OneDriveConsumer')) {
            $oneDriveRoot = [Environment]::GetEnvironmentVariable($oneDriveVariable)
            if (-not [string]::IsNullOrWhiteSpace($oneDriveRoot) -and
                (Test-PathWithin -ChildPath $keyPath -ParentPath $oneDriveRoot)) {
                throw "Runner private key must remain outside OneDrive."
            }
        }
    }
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing -and -not $Replace) {
    throw "Scheduled task already exists. Re-run with -Replace to replace this exact task."
}

$applied = $PSCmdlet.ShouldProcess(
    $TaskName,
    'Install and start private control-plane runner'
)
if ($applied) {
    $quotedConfig = '"' + $config.Replace('"', '""') + '"'
    $action = New-ScheduledTaskAction `
        -Execute $python `
        -Argument "-m worker.control_plane_runner run --config $quotedConfig" `
        -WorkingDirectory $repository
    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal = New-ScheduledTaskPrincipal `
        -UserId $currentUser `
        -LogonType Interactive `
        -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -StartWhenAvailable
    if ($null -ne $existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description 'Outbound-only private Job Apply Agent control-plane runner.' | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    $installedTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $resultState = [string]$installedTask.State
    $result = 'Installed'
}
else {
    $resultState = 'Simulated'
    $result = 'SkippedByShouldProcess'
}

[pscustomobject]@{
    TaskName = $TaskName
    State = $resultState
    Result = $result
    Applied = $applied
    ExistingTask = $null -ne $existing
    Repository = $repository
    Config = $config
}
