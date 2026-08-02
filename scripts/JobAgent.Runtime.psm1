#Requires -Version 7.2

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:ComposeProjectName = 'job-apply-agent'
$script:RunnerTaskName = 'JobApplyAgent-PrivateRunner'
$script:RunnerTaskPath = '\'
$script:RunnerTaskOwnershipMarker = 'JobApplyAgent.ManagedPrivateRunner.v1'
$script:RuntimeProtocolVersion = 'submission-control.v1'
$script:RuntimeSchemaVersion = '1'
$script:CoreServices = @(
    'postgres',
    'redis',
    'web-api',
    'celery-worker',
    'celery-beat',
    'prometheus',
    'grafana'
)
$script:KnownServices = @($script:CoreServices + 'nginx')
$script:HeldMutexNames = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::Ordinal
)

function ConvertTo-JobAgentCanonicalPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath,

        [switch]$RequireExisting
    )

    $fullPath = [System.IO.Path]::GetFullPath($LiteralPath)
    if ($RequireExisting) {
        $fullPath = (Resolve-Path -LiteralPath $fullPath -ErrorAction Stop).Path
    }
    return $fullPath.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Test-JobAgentPathWithin {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ChildPath,

        [Parameter(Mandatory = $true)]
        [string]$ParentPath
    )

    $parent = (ConvertTo-JobAgentCanonicalPath -LiteralPath $ParentPath) +
        [System.IO.Path]::DirectorySeparatorChar
    $child = ConvertTo-JobAgentCanonicalPath -LiteralPath $ChildPath
    return $child.StartsWith($parent, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-JobAgentPathRelated {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$FirstPath,

        [Parameter(Mandatory = $true)]
        [string]$SecondPath
    )

    $first = ConvertTo-JobAgentCanonicalPath -LiteralPath $FirstPath
    $second = ConvertTo-JobAgentCanonicalPath -LiteralPath $SecondPath
    return (
        [string]::Equals(
            $first,
            $second,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or
        (Test-JobAgentPathWithin -ChildPath $first -ParentPath $second) -or
        (Test-JobAgentPathWithin -ChildPath $second -ParentPath $first)
    )
}

function Resolve-JobAgentPathThroughExistingAncestor {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    $cursor = [System.IO.Path]::GetFullPath($LiteralPath)
    $missing = [System.Collections.Generic.List[string]]::new()
    while (-not (Test-Path -LiteralPath $cursor)) {
        $leaf = Split-Path -Leaf $cursor
        $parent = Split-Path -Parent $cursor
        if ([string]::IsNullOrWhiteSpace($leaf) -or $parent -eq $cursor) {
            throw 'EXTERNAL_ROOT_RESOLUTION_FAILED'
        }
        $missing.Add($leaf)
        $cursor = $parent
    }
    $resolved = (Resolve-Path -LiteralPath $cursor -ErrorAction Stop).Path
    for ($index = $missing.Count - 1; $index -ge 0; $index--) {
        $resolved = Join-Path $resolved $missing[$index]
    }
    return ConvertTo-JobAgentCanonicalPath -LiteralPath $resolved
}

function Test-JobAgentRawReparseAncestor {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    $cursor = [System.IO.Path]::GetFullPath($LiteralPath)
    while ($true) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
            if (
                ([int]$item.Attributes -band
                    [int][System.IO.FileAttributes]::ReparsePoint) -ne 0
            ) {
                return $true
            }
        }
        $parent = Split-Path -Parent $cursor
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
            return $false
        }
        $cursor = $parent
    }
}

function Test-JobAgentNetworkPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    if ($LiteralPath.StartsWith('\\') -or $LiteralPath.StartsWith('//')) {
        return $true
    }
    $pathRoot = [System.IO.Path]::GetPathRoot($LiteralPath)
    if ($pathRoot -match '^(?<drive>[A-Za-z]):\\?$') {
        $drive = Get-PSDrive -Name $Matches['drive'] -ErrorAction SilentlyContinue
        if (
            $null -ne $drive -and
            -not [string]::IsNullOrWhiteSpace([string]$drive.DisplayRoot) -and
            (
                ([string]$drive.DisplayRoot).StartsWith('\\') -or
                ([string]$drive.DisplayRoot).StartsWith('//')
            )
        ) {
            return $true
        }
    }
    return $false
}

function Test-JobAgentOneDrivePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$CandidatePaths
    )

    $onedriveRoots = foreach ($name in @(
        'OneDrive',
        'OneDriveCommercial',
        'OneDriveConsumer'
    )) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            ConvertTo-JobAgentCanonicalPath -LiteralPath $value
            Resolve-JobAgentPathThroughExistingAncestor -LiteralPath $value
        }
    }
    foreach ($candidate in $CandidatePaths) {
        $parts = @($candidate -split '[\\/]' | Where-Object { $_ })
        if (@($parts | Where-Object {
            $_.StartsWith('OneDrive', [System.StringComparison]::OrdinalIgnoreCase)
        }).Count -gt 0) {
            return $true
        }
        foreach ($onedriveRoot in @($onedriveRoots)) {
            if (Test-JobAgentPathRelated -FirstPath $candidate -SecondPath $onedriveRoot) {
                return $true
            }
        }
    }
    return $false
}

function Assert-JobAgentExternalLayout {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$LocalAppDataRoot,

        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath
    )

    $rawLocal = ConvertTo-JobAgentCanonicalPath -LiteralPath $LocalAppDataRoot
    $rawRoot = ConvertTo-JobAgentCanonicalPath -LiteralPath $Layout.Root
    foreach ($candidate in @($rawLocal, $rawRoot)) {
        if (Test-JobAgentNetworkPath -LiteralPath $candidate) {
            throw 'EXTERNAL_ROOT_NOT_LOCAL'
        }
        if (Test-JobAgentRawReparseAncestor -LiteralPath $candidate) {
            throw 'EXTERNAL_ROOT_REPARSE_POINT'
        }
    }
    $resolvedLocal = Resolve-JobAgentPathThroughExistingAncestor -LiteralPath $rawLocal
    $resolvedRoot = Resolve-JobAgentPathThroughExistingAncestor -LiteralPath $rawRoot
    foreach ($candidate in @($resolvedLocal, $resolvedRoot)) {
        if (Test-JobAgentNetworkPath -LiteralPath $candidate) {
            throw 'EXTERNAL_ROOT_NOT_LOCAL'
        }
    }
    $repository = ConvertTo-JobAgentCanonicalPath `
        -LiteralPath $RepositoryPath `
        -RequireExisting
    foreach ($candidate in @($rawLocal, $rawRoot, $resolvedLocal, $resolvedRoot)) {
        if (Test-JobAgentPathRelated -FirstPath $candidate -SecondPath $repository) {
            throw 'EXTERNAL_ROOT_REPOSITORY_RELATED'
        }
    }
    if (Test-JobAgentOneDrivePath -CandidatePaths @(
        $rawLocal,
        $rawRoot,
        $resolvedLocal,
        $resolvedRoot
    )) {
        throw 'EXTERNAL_ROOT_IN_ONEDRIVE'
    }
    return $true
}

function ConvertTo-JobAgentComposePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    return (ConvertTo-JobAgentCanonicalPath -LiteralPath $LiteralPath).Replace('\', '/')
}

function Get-JobAgentRuntimeConstants {
    [CmdletBinding()]
    param()

    return [pscustomobject]@{
        ComposeProjectName = $script:ComposeProjectName
        RunnerTaskName = $script:RunnerTaskName
        RunnerTaskPath = $script:RunnerTaskPath
        RunnerTaskOwnershipMarker = $script:RunnerTaskOwnershipMarker
        RuntimeProtocolVersion = $script:RuntimeProtocolVersion
        RuntimeSchemaVersion = $script:RuntimeSchemaVersion
        CoreServices = @($script:CoreServices)
        KnownServices = @($script:KnownServices)
    }
}

function Get-JobAgentLayout {
    [CmdletBinding()]
    param(
        [string]$LocalAppDataRoot = $env:LOCALAPPDATA
    )

    if ([string]::IsNullOrWhiteSpace($LocalAppDataRoot)) {
        throw 'LOCALAPPDATA_UNAVAILABLE'
    }
    if (-not [System.IO.Path]::IsPathFullyQualified($LocalAppDataRoot)) {
        throw 'LOCALAPPDATA_NOT_ABSOLUTE'
    }
    $localRoot = ConvertTo-JobAgentCanonicalPath -LiteralPath $LocalAppDataRoot
    if ($localRoot.StartsWith('\\') -or $localRoot.StartsWith('//')) {
        throw 'LOCALAPPDATA_NOT_LOCAL'
    }
    $root = Join-Path $localRoot 'JobApplyAgent'
    if (-not (Test-JobAgentPathWithin -ChildPath $root -ParentPath $localRoot)) {
        throw 'JOB_AGENT_ROOT_INVALID'
    }
    $runtime = Join-Path $root 'runtime'
    $identity = Join-Path $root 'control-plane'
    return [pscustomobject]@{
        Root = $root
        Runtime = $runtime
        RuntimeEnv = Join-Path $runtime 'runtime.env'
        ProfileData = Join-Path $root 'profile-data'
        BrowserState = Join-Path $root 'browser-state'
        Tls = Join-Path $root 'tls'
        Identity = $identity
        IdentityCurrent = Join-Path $identity 'current.json'
        Logs = Join-Path $root 'logs'
    }
}

function New-JobAgentSecret {
    [CmdletBinding()]
    param(
        [ValidateRange(32, 128)]
        [int]$ByteCount = 48
    )

    $bytes = [byte[]]::new($ByteCount)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function ConvertTo-JobAgentEnvValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Value
    )

    if ($Value.Contains("`r") -or $Value.Contains("`n") -or $Value.Contains([char]0)) {
        throw 'RUNTIME_ENV_VALUE_INVALID'
    }
    if ($Value -match '^[A-Za-z0-9_./:@+-]*$') {
        return $Value
    }
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function New-JobAgentRuntimeEnvironmentText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Layout,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}(?:[0-9a-f]{24})?$')]
        [string]$BuildSha,

        [ValidateRange(1024, 65535)]
        [int]$ApiPort = 8000,

        [ValidateRange(1024, 65535)]
        [int]$PostgresPort = 55432,

        [ValidateRange(1024, 65535)]
        [int]$RedisPort = 56379,

        [ValidateRange(1024, 65535)]
        [int]$PrometheusPort = 59090,

        [ValidateRange(1024, 65535)]
        [int]$GrafanaPort = 53001,

        [string]$SecretKey = (New-JobAgentSecret),

        [string]$WebhookSecret = (New-JobAgentSecret),

        [string]$PostgresPassword = (New-JobAgentSecret -ByteCount 36),

        [string]$GrafanaPassword = (New-JobAgentSecret -ByteCount 36)
    )

    $ports = @($ApiPort, $PostgresPort, $RedisPort, $PrometheusPort, $GrafanaPort)
    if (@($ports | Sort-Object -Unique).Count -ne $ports.Count) {
        throw 'RUNTIME_PORT_COLLISION'
    }
    foreach ($secret in @($SecretKey, $WebhookSecret, $PostgresPassword, $GrafanaPassword)) {
        if ($secret.Length -lt 32 -or $secret -match '[\r\n:@]') {
            throw 'RUNTIME_SECRET_INVALID'
        }
    }

    $profile = ConvertTo-JobAgentComposePath -LiteralPath $Layout.ProfileData
    $browser = ConvertTo-JobAgentComposePath -LiteralPath $Layout.BrowserState
    $tls = ConvertTo-JobAgentComposePath -LiteralPath $Layout.Tls
    $runtimeEnv = ConvertTo-JobAgentComposePath -LiteralPath $Layout.RuntimeEnv
    $values = [ordered]@{
        JOB_AGENT_RUNTIME_SCHEMA = $script:RuntimeSchemaVersion
        JOB_AGENT_ENV_FILE = $runtimeEnv
        JOB_AGENT_PROFILE_DATA_DIR = $profile
        JOB_AGENT_BROWSER_STATE_DIR = $browser
        JOB_AGENT_TLS_DIR = $tls
        APP_BUILD_SHA = $BuildSha
        APP_ENV = 'production'
        DRY_RUN = 'true'
        DRAFT_ONLY = 'true'
        AUTO_APPLY = 'false'
        PORTAL_FINAL_SUBMIT_ENABLED = 'false'
        LIVE_AUTOMATION_ACKNOWLEDGED = 'false'
        TASKS_ALWAYS_EAGER = 'false'
        SECRET_KEY = $SecretKey
        WHATSAPP_APP_SECRET = $WebhookSecret
        CORS_ORIGINS = "http://127.0.0.1:$ApiPort"
        TRUSTED_PROXIES = ''
        POSTGRES_USER = 'jobagent'
        POSTGRES_PASSWORD = $PostgresPassword
        POSTGRES_DB = 'job_agent'
        POSTGRES_PORT = [string]$PostgresPort
        DATABASE_URL = "postgresql://jobagent:$PostgresPassword@127.0.0.1:$PostgresPort/job_agent"
        REDIS_PORT = [string]$RedisPort
        REDIS_URL = "redis://127.0.0.1:$RedisPort/0"
        API_PORT = [string]$ApiPort
        PROMETHEUS_PORT = [string]$PrometheusPort
        GRAFANA_PORT = [string]$GrafanaPort
        GRAFANA_USER = 'jobagent'
        GRAFANA_PASSWORD = $GrafanaPassword
        LLM_PROVIDER = 'ollama'
        LLM_MODEL = 'qwen2.5:7b'
        OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
        OLLAMA_BASE_URL_DOCKER = 'http://host.docker.internal:11434'
        OLLAMA_NO_CLOUD = '1'
        CLOUD_VISION_ENABLED = '0'
        OLLAMA_EXPECTED_MODEL_DIGEST = (
            'sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e'
        )
        USER_PROFILE_PATH = "$profile/user_profile.yaml"
        APPLICATION_DATA_DIR = $profile
        CV_ROUTING_PATH = "$profile/cv_routing.yaml"
        CV_DIRECTORY = "$profile/cvs"
        EMPLOYER_WORKFLOW_PATH = "$profile/employer_workflows.yaml"
        LINKEDIN_BROWSER_PROFILE_DIR = "$browser/linkedin"
        PORTAL_BROWSER_PROFILE_ROOT = "$browser/portals"
        PORTAL_BROWSER_HEADLESS = 'true'
        PUBLIC_DISCOVERY_ENABLED = 'true'
    }
    $lines = foreach ($entry in $values.GetEnumerator()) {
        '{0}={1}' -f $entry.Key, (ConvertTo-JobAgentEnvValue -Value ([string]$entry.Value))
    }
    return ($lines -join "`n") + "`n"
}

function ConvertFrom-JobAgentEnvironmentText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Text
    )

    $values = @{}
    foreach ($rawLine in ($Text -split "`r?`n")) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) {
            continue
        }
        $separator = $line.IndexOf('=')
        if ($separator -lt 1) {
            throw 'RUNTIME_ENV_INVALID'
        }
        $name = $line.Substring(0, $separator).Trim()
        if ($name -notmatch '^[A-Z][A-Z0-9_]{0,63}$' -or $values.ContainsKey($name)) {
            throw 'RUNTIME_ENV_INVALID'
        }
        $value = $line.Substring($separator + 1).Trim()
        if ($value.Length -ge 2 -and $value[0] -eq '"' -and $value[-1] -eq '"') {
            $value = $value.Substring(1, $value.Length - 2)
            $value = $value.Replace('\"', '"').Replace('\\', '\')
        }
        if ($value.Contains("`r") -or $value.Contains("`n") -or $value.Contains([char]0)) {
            throw 'RUNTIME_ENV_INVALID'
        }
        $values[$name] = $value
    }
    return $values
}

function Read-JobAgentRuntimeEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $fullPath = ConvertTo-JobAgentCanonicalPath -LiteralPath $Path -RequireExisting
    $item = Get-Item -LiteralPath $fullPath -ErrorAction Stop
    if (-not $item.PSIsContainer -and $item.Length -le 64KB) {
        return ConvertFrom-JobAgentEnvironmentText -Text (
            Get-Content -LiteralPath $fullPath -Raw -Encoding UTF8
        )
    }
    throw 'RUNTIME_ENV_UNAVAILABLE'
}

function Assert-JobAgentSafeRuntimeEnvironment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Values,

        [Parameter(Mandatory = $true)]
        [pscustomobject]$Layout
    )

    $required = @{
        JOB_AGENT_RUNTIME_SCHEMA = $script:RuntimeSchemaVersion
        APP_ENV = 'production'
        DRY_RUN = 'true'
        DRAFT_ONLY = 'true'
        AUTO_APPLY = 'false'
        PORTAL_FINAL_SUBMIT_ENABLED = 'false'
        LIVE_AUTOMATION_ACKNOWLEDGED = 'false'
        TASKS_ALWAYS_EAGER = 'false'
        LLM_PROVIDER = 'ollama'
        LLM_MODEL = 'qwen2.5:7b'
        OLLAMA_NO_CLOUD = '1'
        CLOUD_VISION_ENABLED = '0'
        PORTAL_BROWSER_HEADLESS = 'true'
        OLLAMA_BASE_URL_DOCKER = 'http://host.docker.internal:11434'
        OLLAMA_EXPECTED_MODEL_DIGEST = (
            'sha256:845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e'
        )
    }
    foreach ($entry in $required.GetEnumerator()) {
        if (-not $Values.ContainsKey($entry.Key) -or
            -not [string]::Equals(
                [string]$Values[$entry.Key],
                [string]$entry.Value,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            throw "RUNTIME_ENV_UNSAFE_$($entry.Key)"
        }
    }
    foreach ($secretName in @(
        'SECRET_KEY',
        'WHATSAPP_APP_SECRET',
        'POSTGRES_PASSWORD',
        'GRAFANA_PASSWORD'
    )) {
        $secret = [string]$Values[$secretName]
        if ($secret.Length -lt 32 -or $secret -match '[\r\n]') {
            throw "RUNTIME_ENV_SECRET_INVALID_$secretName"
        }
    }
    foreach ($endpointName in @('DATABASE_URL', 'REDIS_URL', 'OLLAMA_BASE_URL')) {
        try {
            $endpoint = [uri][string]$Values[$endpointName]
        }
        catch {
            throw "RUNTIME_ENV_ENDPOINT_INVALID_$endpointName"
        }
        if ($endpoint.Host -ne '127.0.0.1') {
            throw "RUNTIME_ENV_ENDPOINT_NOT_LOOPBACK_$endpointName"
        }
        $expectedScheme = @{
            DATABASE_URL = 'postgresql'
            REDIS_URL = 'redis'
            OLLAMA_BASE_URL = 'http'
        }[$endpointName]
        if ($endpoint.Scheme -ne $expectedScheme) {
            throw "RUNTIME_ENV_ENDPOINT_SCHEME_INVALID_$endpointName"
        }
    }
    $ports = @{}
    foreach ($portName in @(
        'API_PORT',
        'POSTGRES_PORT',
        'REDIS_PORT',
        'PROMETHEUS_PORT',
        'GRAFANA_PORT'
    )) {
        $port = 0
        if (
            -not [int]::TryParse([string]$Values[$portName], [ref]$port) -or
            $port -lt 1024 -or
            $port -gt 65535
        ) {
            throw "RUNTIME_ENV_PORT_INVALID_$portName"
        }
        $ports[$portName] = $port
    }
    if (@($ports.Values | Sort-Object -Unique).Count -ne $ports.Count) {
        throw 'RUNTIME_ENV_PORT_COLLISION'
    }
    if (
        [string]$Values['CORS_ORIGINS'] -ne
        "http://127.0.0.1:$($ports['API_PORT'])"
    ) {
        throw 'RUNTIME_ENV_CORS_NOT_LOOPBACK'
    }
    $databaseEndpoint = [uri][string]$Values['DATABASE_URL']
    $redisEndpoint = [uri][string]$Values['REDIS_URL']
    if (
        $databaseEndpoint.Port -ne $ports['POSTGRES_PORT'] -or
        $redisEndpoint.Port -ne $ports['REDIS_PORT']
    ) {
        throw 'RUNTIME_ENV_ENDPOINT_PORT_MISMATCH'
    }
    $expectedPaths = @{
        JOB_AGENT_ENV_FILE = $Layout.RuntimeEnv
        JOB_AGENT_PROFILE_DATA_DIR = $Layout.ProfileData
        JOB_AGENT_BROWSER_STATE_DIR = $Layout.BrowserState
        JOB_AGENT_TLS_DIR = $Layout.Tls
    }
    foreach ($entry in $expectedPaths.GetEnumerator()) {
        $actual = [string]$Values[$entry.Key]
        if (-not [string]::Equals(
            (ConvertTo-JobAgentCanonicalPath -LiteralPath $actual),
            (ConvertTo-JobAgentCanonicalPath -LiteralPath ([string]$entry.Value)),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw "RUNTIME_ENV_PATH_MISMATCH_$($entry.Key)"
        }
    }
    $build = [string]$Values['APP_BUILD_SHA']
    if ($build -notmatch '^[0-9a-f]{40}(?:[0-9a-f]{24})?$') {
        throw 'RUNTIME_ENV_BUILD_INVALID'
    }
    return $true
}

function Set-JobAgentPrivateAcl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not $IsWindows) {
        return
    }
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ([string]::IsNullOrWhiteSpace($identity)) {
        throw 'WINDOWS_IDENTITY_UNAVAILABLE'
    }
    & icacls.exe $Path '/inheritance:r' '/grant:r' "$identity`:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'RUNTIME_ACL_FAILED'
    }
}

function Initialize-JobAgentExternalLayout {
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Layout,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}(?:[0-9a-f]{24})?$')]
        [string]$BuildSha,

        [switch]$UpgradeRelease
    )

    if (-not $PSCmdlet.ShouldProcess(
        $Layout.Root,
        'Create private runtime directories and a fail-closed runtime environment'
    )) {
        return [pscustomobject]@{
            Applied = $false
            Root = $Layout.Root
            RuntimeEnv = $Layout.RuntimeEnv
        }
    }

    foreach ($directory in @(
        $Layout.Root,
        $Layout.Runtime,
        $Layout.ProfileData,
        $Layout.BrowserState,
        $Layout.Tls,
        $Layout.Logs
    )) {
        [System.IO.Directory]::CreateDirectory($directory) | Out-Null
    }
    Set-JobAgentPrivateAcl -Path $Layout.Root
    if (Test-Path -LiteralPath $Layout.RuntimeEnv) {
        $existing = Read-JobAgentRuntimeEnvironment -Path $Layout.RuntimeEnv
        Assert-JobAgentSafeRuntimeEnvironment -Values $existing -Layout $Layout | Out-Null
        $releaseUpdate = $null
        if (
            $UpgradeRelease -and
            -not [string]::Equals(
                [string]$existing['APP_BUILD_SHA'],
                $BuildSha,
                [System.StringComparison]::Ordinal
            )
        ) {
            $releaseUpdate = Update-JobAgentRuntimeRelease `
                -Path $Layout.RuntimeEnv `
                -Layout $Layout `
                -BuildSha $BuildSha `
                -Confirm:$false
        }
        return [pscustomobject]@{
            Applied = $null -ne $releaseUpdate -and $releaseUpdate.Applied
            Existing = $true
            Root = $Layout.Root
            RuntimeEnv = $Layout.RuntimeEnv
            ReleaseUpdated = $null -ne $releaseUpdate -and $releaseUpdate.Applied
        }
    }

    $content = New-JobAgentRuntimeEnvironmentText -Layout $Layout -BuildSha $BuildSha
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    $bytes = $utf8.GetBytes($content)
    $stream = [System.IO.File]::Open(
        $Layout.RuntimeEnv,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    }
    finally {
        $stream.Dispose()
    }
    Set-JobAgentPrivateAcl -Path $Layout.Runtime
    return [pscustomobject]@{
        Applied = $true
        Existing = $false
        Root = $Layout.Root
        RuntimeEnv = $Layout.RuntimeEnv
    }
}

function Get-JobAgentBuildSha {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [switch]$RequireClean,

        [switch]$RequireMain
    )

    $repository = ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath -RequireExisting
    if ($RequireClean) {
        $status = & git -C $repository status --porcelain=v1 --untracked-files=all 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw 'GIT_STATUS_UNAVAILABLE'
        }
        Assert-JobAgentGitPorcelainClean -Lines @($status) | Out-Null
    }
    $result = & git -C $repository rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'GIT_BUILD_ID_UNAVAILABLE'
    }
    $build = ([string]$result).Trim().ToLowerInvariant()
    if ($build -notmatch '^[0-9a-f]{40}(?:[0-9a-f]{24})?$') {
        throw 'GIT_BUILD_ID_INVALID'
    }
    if ($RequireMain) {
        $mainBuild = & git -C $repository rev-parse --verify refs/remotes/origin/main 2>$null
        if ($LASTEXITCODE -ne 0) {
            $mainBuild = & git -C $repository rev-parse --verify refs/heads/main 2>$null
        }
        if ($LASTEXITCODE -ne 0) {
            throw 'MAIN_RELEASE_REF_UNAVAILABLE'
        }
        Assert-JobAgentMainRelease `
            -HeadBuildSha $build `
            -MainBuildSha (([string]$mainBuild).Trim().ToLowerInvariant()) | Out-Null
    }
    return $build
}

function Assert-JobAgentGitPorcelainClean {
    [CmdletBinding()]
    param(
        [AllowEmptyCollection()]
        [string[]]$Lines = @()
    )

    if (@($Lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count -gt 0) {
        throw 'REPOSITORY_NOT_CLEAN'
    }
    return $true
}

function Assert-JobAgentMainRelease {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}(?:[0-9a-f]{24})?$')]
        [string]$HeadBuildSha,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}(?:[0-9a-f]{24})?$')]
        [string]$MainBuildSha
    )

    if (-not [string]::Equals(
        $HeadBuildSha,
        $MainBuildSha,
        [System.StringComparison]::Ordinal
    )) {
        throw 'RELEASE_NOT_MAIN_DERIVED'
    }
    return $true
}

function Assert-JobAgentRuntimeRelease {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Values,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}(?:[0-9a-f]{24})?$')]
        [string]$ExpectedBuildSha
    )

    if (-not [string]::Equals(
        [string]$Values['APP_BUILD_SHA'],
        $ExpectedBuildSha,
        [System.StringComparison]::Ordinal
    )) {
        throw 'RUNTIME_RELEASE_STALE'
    }
    return $true
}

function Update-JobAgentRuntimeRelease {
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [pscustomobject]$Layout,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}(?:[0-9a-f]{24})?$')]
        [string]$BuildSha
    )

    $runtimePath = ConvertTo-JobAgentCanonicalPath -LiteralPath $Path -RequireExisting
    $values = Read-JobAgentRuntimeEnvironment -Path $runtimePath
    Assert-JobAgentSafeRuntimeEnvironment -Values $values -Layout $Layout | Out-Null
    if ([string]::Equals(
        [string]$values['APP_BUILD_SHA'],
        $BuildSha,
        [System.StringComparison]::Ordinal
    )) {
        return [pscustomobject]@{ Applied = $false; Reason = 'AlreadyCurrent' }
    }
    if (-not $PSCmdlet.ShouldProcess(
        $runtimePath,
        'Atomically update only the runtime release binding'
    )) {
        return [pscustomobject]@{ Applied = $false; Reason = 'WhatIf' }
    }

    $text = Get-Content -LiteralPath $runtimePath -Raw -Encoding UTF8
    $pattern = '(?m)^APP_BUILD_SHA=[^\r\n]*$'
    if ([regex]::Matches($text, $pattern).Count -ne 1) {
        throw 'RUNTIME_RELEASE_FIELD_INVALID'
    }
    $updated = [regex]::Replace(
        $text,
        $pattern,
        "APP_BUILD_SHA=$BuildSha",
        1
    )
    $temporary = Join-Path (Split-Path -Parent $runtimePath) (
        '.runtime.env.' + [guid]::NewGuid().ToString('N') + '.tmp'
    )
    try {
        $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($updated)
        $stream = [System.IO.File]::Open(
            $temporary,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }
        [System.IO.File]::Move($temporary, $runtimePath, $true)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
    $verified = Read-JobAgentRuntimeEnvironment -Path $runtimePath
    Assert-JobAgentSafeRuntimeEnvironment -Values $verified -Layout $Layout | Out-Null
    Assert-JobAgentRuntimeRelease -Values $verified -ExpectedBuildSha $BuildSha | Out-Null
    return [pscustomobject]@{ Applied = $true; Reason = 'ReleaseUpdated' }
}

function Get-JobAgentMutexName {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath
    )

    $canonical = ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($canonical.ToLowerInvariant())
    $hash = [System.Security.Cryptography.SHA256]::HashData($bytes)
    $suffix = [Convert]::ToHexString($hash).Substring(0, 24).ToLowerInvariant()
    return "Local\JobApplyAgent.Runtime.$suffix"
}

function Enter-JobAgentRuntimeMutex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath
    )

    $name = Get-JobAgentMutexName -RepositoryPath $RepositoryPath
    if ($script:HeldMutexNames.Contains($name)) {
        throw 'JOB_AGENT_COMMAND_ALREADY_RUNNING'
    }
    $mutex = [System.Threading.Mutex]::new($false, $name)
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne(0)
        }
        catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if (-not $acquired) {
            throw 'JOB_AGENT_COMMAND_ALREADY_RUNNING'
        }
        if (-not $script:HeldMutexNames.Add($name)) {
            throw 'JOB_AGENT_COMMAND_ALREADY_RUNNING'
        }
        return [pscustomobject]@{
            Name = $name
            Mutex = $mutex
            Acquired = $true
        }
    }
    catch {
        if ($acquired) {
            try {
                $mutex.ReleaseMutex()
            }
            catch {
                # Preserve the original acquisition failure.
            }
        }
        [void]$script:HeldMutexNames.Remove($name)
        $mutex.Dispose()
        throw
    }
}

function Exit-JobAgentRuntimeMutex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Handle
    )

    if ($Handle.Acquired) {
        $Handle.Mutex.ReleaseMutex()
        $Handle.Acquired = $false
    }
    [void]$script:HeldMutexNames.Remove([string]$Handle.Name)
    $Handle.Mutex.Dispose()
}

function Get-JobAgentExpectedTaskAction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [string]$PythonExecutable,

        [Parameter(Mandatory = $true)]
        [string]$ConfigPath
    )

    $repository = ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath
    $python = ConvertTo-JobAgentCanonicalPath -LiteralPath $PythonExecutable
    $config = ConvertTo-JobAgentCanonicalPath -LiteralPath $ConfigPath
    return [pscustomobject]@{
        Execute = $python
        WorkingDirectory = $repository
        Arguments = "-B -m worker.control_plane_runner run --config `"$config`""
    }
}

function Test-JobAgentWindowsIdentityMatch {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$First,

        [Parameter(Mandatory = $true)]
        [string]$Second
    )

    $firstIdentity = $First.Trim()
    $secondIdentity = $Second.Trim()
    if (
        [string]::IsNullOrWhiteSpace($firstIdentity) -or
        [string]::IsNullOrWhiteSpace($secondIdentity)
    ) {
        return $false
    }
    if ([string]::Equals(
        $firstIdentity,
        $secondIdentity,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return $true
    }
    if (-not $IsWindows) {
        return $false
    }

    try {
        $sidType = [System.Security.Principal.SecurityIdentifier]
        $firstSid = (
            [System.Security.Principal.NTAccount]::new($firstIdentity)
        ).Translate($sidType).Value
        $secondSid = (
            [System.Security.Principal.NTAccount]::new($secondIdentity)
        ).Translate($sidType).Value
        return [string]::Equals(
            $firstSid,
            $secondSid,
            [System.StringComparison]::Ordinal
        )
    }
    catch {
        return $false
    }
}

function Get-JobAgentTaskOwnership {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [object]$Task,

        [Parameter(Mandatory = $true)]
        [pscustomobject]$ExpectedAction,

        [string]$ExpectedUser = ''
    )

    if ($null -eq $Task) {
        return [pscustomobject]@{
            Classification = 'Absent'
            Owned = $false
            Exact = $false
            Adoptable = $false
        }
    }
    $actions = @($Task.Actions)
    $action = if ($actions.Count -eq 1) { $actions[0] } else { $null }
    $actionExact = $null -ne $action
    if ($actionExact) {
        try {
            $execute = ConvertTo-JobAgentCanonicalPath -LiteralPath ([string]$action.Execute)
            $working = ConvertTo-JobAgentCanonicalPath -LiteralPath (
                [string]$action.WorkingDirectory
            )
            $actionExact = (
                [string]::Equals(
                    $execute,
                    $ExpectedAction.Execute,
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -and
                [string]::Equals(
                    $working,
                    $ExpectedAction.WorkingDirectory,
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -and
                [string]::Equals(
                    ([string]$action.Arguments).Trim(),
                    $ExpectedAction.Arguments,
                    [System.StringComparison]::Ordinal
                )
            )
        }
        catch {
            $actionExact = $false
        }
    }
    $principalExact = $true
    if (-not [string]::IsNullOrWhiteSpace($ExpectedUser)) {
        $logonType = if (
            $null -ne $Task.Principal -and
            $null -ne $Task.Principal.PSObject.Properties['LogonType']
        ) {
            [string]$Task.Principal.LogonType
        }
        else {
            ''
        }
        $principalExact = (
            $null -ne $Task.Principal -and
            (Test-JobAgentWindowsIdentityMatch `
                -First ([string]$Task.Principal.UserId) `
                -Second $ExpectedUser) -and
            ([string]$Task.Principal.RunLevel) -eq 'Limited' -and
            $logonType -in @('Interactive', 'InteractiveToken')
        )
    }
    $triggers = if ($null -ne $Task.PSObject.Properties['Triggers']) {
        @($Task.Triggers)
    }
    else {
        @()
    }
    $trigger = if ($triggers.Count -eq 1) { $triggers[0] } else { $null }
    $triggerExact = $null -ne $trigger
    if ($triggerExact) {
        $triggerType = if (
            $null -ne $trigger.PSObject.Properties['CimClass'] -and
            $null -ne $trigger.CimClass -and
            $null -ne $trigger.CimClass.PSObject.Properties['CimClassName']
        ) {
            [string]$trigger.CimClass.CimClassName
        }
        elseif ($null -ne $trigger.PSObject.Properties['Type']) {
            [string]$trigger.Type
        }
        else {
            ''
        }
        $triggerUser = if ($null -ne $trigger.PSObject.Properties['UserId']) {
            [string]$trigger.UserId
        }
        else {
            ''
        }
        $triggerEnabled = (
            $null -ne $trigger.PSObject.Properties['Enabled'] -and
            [bool]$trigger.Enabled
        )
        $triggerExact = (
            $triggerType -in @('MSFT_TaskLogonTrigger', 'Logon') -and
            $triggerEnabled -and
            (Test-JobAgentWindowsIdentityMatch `
                -First $triggerUser `
                -Second $ExpectedUser)
        )
    }
    $settingsExact = (
        $null -ne $Task.PSObject.Properties['Settings'] -and
        $null -ne $Task.Settings
    )
    if ($settingsExact) {
        $requiredSettings = @{
            Enabled = 'True'
            MultipleInstances = 'IgnoreNew'
            RestartCount = '999'
            RestartInterval = 'PT1M'
            ExecutionTimeLimit = 'PT0S'
            StartWhenAvailable = 'True'
        }
        foreach ($entry in $requiredSettings.GetEnumerator()) {
            $property = $Task.Settings.PSObject.Properties[$entry.Key]
            if (
                $null -eq $property -or
                -not [string]::Equals(
                    ([string]$property.Value).Trim(),
                    [string]$entry.Value,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            ) {
                $settingsExact = $false
            }
        }
    }
    $stateExact = (
        $null -ne $Task.PSObject.Properties['State'] -and
        [string]$Task.State -in @('Ready', 'Running', 'Queued')
    )
    $exact = (
        $actionExact -and
        $principalExact -and
        $triggerExact -and
        $settingsExact -and
        $stateExact
    )
    $owned = [string]::Equals(
        ([string]$Task.Description).Trim(),
        $script:RunnerTaskOwnershipMarker,
        [System.StringComparison]::Ordinal
    )
    $classification = if ($owned -and $exact) {
        'OwnedExact'
    }
    elseif ($owned) {
        'OwnedDrifted'
    }
    elseif ($exact) {
        'LegacyAdoptable'
    }
    else {
        'Foreign'
    }
    return [pscustomobject]@{
        Classification = $classification
        Owned = $owned
        Exact = $exact
        Adoptable = $classification -eq 'LegacyAdoptable'
    }
}

function Get-JobAgentEmergencyTaskOwnership {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [object]$Task
    )

    if ($null -eq $Task) {
        return [pscustomobject]@{
            Classification = 'Absent'
            Owned = $false
            MarkerMatched = $false
        }
    }
    $description = if (
        $null -ne $Task.PSObject.Properties['Description']
    ) {
        [string]$Task.Description
    }
    else {
        ''
    }
    $owned = [string]::Equals(
        $description.Trim(),
        $script:RunnerTaskOwnershipMarker,
        [System.StringComparison]::Ordinal
    )
    return [pscustomobject]@{
        Classification = if ($owned) { 'MarkerOwned' } else { 'Foreign' }
        Owned = $owned
        MarkerMatched = $owned
    }
}

function Assert-JobAgentEmergencyTaskTarget {
    [CmdletBinding()]
    param(
        [AllowEmptyString()]
        [string]$TaskName
    )

    if (-not [string]::Equals(
        $TaskName,
        $script:RunnerTaskName,
        [System.StringComparison]::Ordinal
    )) {
        throw 'RUNNER_TASK_TARGET_NOT_CANONICAL'
    }
    return $true
}

function ConvertFrom-JobAgentComposeLabels {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [object]$Labels
    )

    $result = @{}
    if ($null -eq $Labels) {
        return $result
    }
    if ($Labels -is [System.Collections.IDictionary]) {
        foreach ($key in $Labels.Keys) {
            $result[[string]$key] = [string]$Labels[$key]
        }
        return $result
    }
    foreach ($pair in ([string]$Labels -split ',')) {
        $separator = $pair.IndexOf('=')
        if ($separator -gt 0) {
            $result[$pair.Substring(0, $separator)] = $pair.Substring($separator + 1)
        }
    }
    return $result
}

function Get-JobAgentComposeOwnership {
    [CmdletBinding()]
    param(
        [AllowEmptyCollection()]
        [object[]]$Containers,

        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [string]$ProjectName = $script:ComposeProjectName
    )

    if ($null -eq $Containers -or @($Containers).Count -eq 0) {
        return [pscustomobject]@{
            Classification = 'Absent'
            Owned = $false
            Exact = $false
            ContainerCount = 0
        }
    }
    $repository = ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath
    $composeFile = Join-Path $repository 'docker-compose.yml'
    $known = [System.Collections.Generic.HashSet[string]]::new(
        [string[]]$script:KnownServices,
        [System.StringComparer]::OrdinalIgnoreCase
    )
    $pathMismatch = $false
    $unknownService = $false
    $projectMismatch = $false
    foreach ($container in @($Containers)) {
        $labels = ConvertFrom-JobAgentComposeLabels -Labels $container.Labels
        $project = [string]$container.Project
        if (-not $project) {
            $project = [string]$labels['com.docker.compose.project']
        }
        if (-not [string]::Equals(
            $project,
            $ProjectName,
            [System.StringComparison]::Ordinal
        )) {
            $projectMismatch = $true
        }
        $workingDirectory = [string]$labels['com.docker.compose.project.working_dir']
        $configFiles = [string]$labels['com.docker.compose.project.config_files']
        try {
            if (-not [string]::Equals(
                (ConvertTo-JobAgentCanonicalPath -LiteralPath $workingDirectory),
                $repository,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                $pathMismatch = $true
            }
            $configCandidates = @($configFiles -split ';' | Where-Object { $_ })
            if ($configCandidates.Count -ne 1 -or -not [string]::Equals(
                (ConvertTo-JobAgentCanonicalPath -LiteralPath $configCandidates[0]),
                (ConvertTo-JobAgentCanonicalPath -LiteralPath $composeFile),
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                $pathMismatch = $true
            }
        }
        catch {
            $pathMismatch = $true
        }
        if (-not $known.Contains([string]$container.Service)) {
            $unknownService = $true
        }
    }
    $classification = if ($projectMismatch -or $pathMismatch) {
        'Foreign'
    }
    elseif ($unknownService) {
        'OwnedDrifted'
    }
    else {
        'OwnedExact'
    }
    return [pscustomobject]@{
        Classification = $classification
        Owned = $classification.StartsWith('Owned')
        Exact = $classification -eq 'OwnedExact'
        ContainerCount = @($Containers).Count
    }
}

function Get-JobAgentEndpointOwnership {
    [CmdletBinding()]
    param(
        [AllowEmptyCollection()]
        [object[]]$Listeners,

        [AllowEmptyCollection()]
        [object[]]$Containers,

        [Parameter(Mandatory = $true)]
        [ValidateRange(1024, 65535)]
        [int]$Port,

        [switch]$AuthenticatedRuntimeVerified
    )

    $matchingListeners = @($Listeners | Where-Object { [int]$_.LocalPort -eq $Port })
    if ($matchingListeners.Count -eq 0) {
        return [pscustomobject]@{
            Classification = 'Absent'
            Owned = $false
            Exact = $false
            MetadataMatched = $false
            Proof = 'None'
        }
    }
    if (@($matchingListeners | Where-Object {
        [string]$_.LocalAddress -notin @('127.0.0.1', '::1')
    }).Count -gt 0) {
        return [pscustomobject]@{
            Classification = 'Foreign'
            Owned = $false
            Exact = $false
            MetadataMatched = $false
            Proof = 'None'
        }
    }
    $listenerProcesses = @(
        $matchingListeners |
            ForEach-Object {
                if ($null -ne $_.PSObject.Properties['OwningProcess']) {
                    [int]$_.OwningProcess
                }
                else {
                    0
                }
            } |
            Sort-Object -Unique
    )
    if (
        $listenerProcesses.Count -ne 1 -or
        $listenerProcesses[0] -le 0
    ) {
        return [pscustomobject]@{
            Classification = 'Unverifiable'
            Owned = $false
            Exact = $false
            MetadataMatched = $false
            Proof = 'None'
        }
    }
    $web = @($Containers | Where-Object { [string]$_.Service -eq 'web-api' })
    $runningWeb = @($web | Where-Object {
        $null -ne $_.PSObject.Properties['State'] -and
        [string]$_.State -eq 'running'
    })
    if ($runningWeb.Count -ne 1) {
        return [pscustomobject]@{
            Classification = if ($web.Count -eq 0) { 'Foreign' } else { 'Unverifiable' }
            Owned = $false
            Exact = $false
            MetadataMatched = $false
            Proof = 'None'
        }
    }
    $publishers = if (
        $null -ne $runningWeb[0].PSObject.Properties['Publishers']
    ) {
        @($runningWeb[0].Publishers)
    }
    else {
        @()
    }
    $matchingPublishers = @($publishers | Where-Object {
        [int]$_.PublishedPort -eq $Port -and
        [int]$_.TargetPort -eq 8000 -and
        [string]$_.URL -eq '127.0.0.1'
    })
    $published = $matchingPublishers.Count -eq 1
    # Docker Desktop's host publisher process name/PID is not stable across
    # backends or releases. Metadata alone therefore stays unverified; the
    # managed callers first recheck this same metadata, then bind the switch
    # only after Test-JobAgentStableRuntime authenticates the exact build.
    $verified = $published -and $AuthenticatedRuntimeVerified
    return [pscustomobject]@{
        Classification = if ($verified) { 'OwnedExact' } else { 'Unverifiable' }
        Owned = $verified
        Exact = $verified
        MetadataMatched = $published
        Proof = if ($verified) { 'AuthenticatedRuntimeIdentity' } else { 'None' }
    }
}

function Get-JobAgentComposeArguments {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [string]$RuntimeEnvPath,

        [string[]]$Arguments = @()
    )

    $repository = ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath
    $runtimeEnv = ConvertTo-JobAgentCanonicalPath -LiteralPath $RuntimeEnvPath
    return @(
        '--context',
        'default',
        'compose',
        '--ansi',
        'never',
        '--project-name',
        $script:ComposeProjectName,
        '--project-directory',
        $repository,
        '--env-file',
        $runtimeEnv,
        '--file',
        (Join-Path $repository 'docker-compose.yml')
    ) + @($Arguments)
}

function Invoke-JobAgentCompose {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [string]$RuntimeEnvPath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [string]$DockerExecutable = '',

        [scriptblock]$CommandInvoker = {
            param([string]$Executable, [string[]]$CommandArguments)

            $captured = & $Executable @CommandArguments 2>&1
            return [pscustomobject]@{
                ExitCode = $LASTEXITCODE
                Output = @($captured)
            }
        }
    )

    if ([string]::IsNullOrWhiteSpace($DockerExecutable)) {
        $docker = Get-Command docker -ErrorAction SilentlyContinue
        if ($null -eq $docker) {
            throw 'DOCKER_CLI_UNAVAILABLE'
        }
        $DockerExecutable = $docker.Source
    }
    $allArguments = Get-JobAgentComposeArguments `
        -RepositoryPath $RepositoryPath `
        -RuntimeEnvPath $RuntimeEnvPath `
        -Arguments $Arguments

    $runtimePath = ConvertTo-JobAgentCanonicalPath `
        -LiteralPath $RuntimeEnvPath `
        -RequireExisting
    $jobAgentRoot = Split-Path -Parent (Split-Path -Parent $runtimePath)
    $layout = Get-JobAgentLayout -LocalAppDataRoot (Split-Path -Parent $jobAgentRoot)
    if (-not [string]::Equals(
        $runtimePath,
        (ConvertTo-JobAgentCanonicalPath -LiteralPath $layout.RuntimeEnv),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'COMPOSE_RUNTIME_ENV_PATH_INVALID'
    }
    $runtimeValues = Read-JobAgentRuntimeEnvironment -Path $runtimePath
    Assert-JobAgentSafeRuntimeEnvironment -Values $runtimeValues -Layout $layout | Out-Null

    $composeFile = Join-Path (
        ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath
    ) 'docker-compose.yml'
    $composeText = Get-Content -LiteralPath $composeFile -Raw -Encoding UTF8
    $environmentNames = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($name in $runtimeValues.Keys) {
        [void]$environmentNames.Add([string]$name)
    }
    foreach ($match in [regex]::Matches(
        $composeText,
        '\$\{(?<name>[A-Za-z_][A-Za-z0-9_]*)'
    )) {
        [void]$environmentNames.Add([string]$match.Groups['name'].Value)
    }
    $forcedClearNames = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($name in @(
        'COMPOSE_PROJECT_NAME',
        'COMPOSE_FILE',
        'COMPOSE_PROFILES',
        'COMPOSE_PATH_SEPARATOR',
        'COMPOSE_ENV_FILES',
        'COMPOSE_DISABLE_ENV_FILE',
        'DOCKER_HOST',
        'DOCKER_CONTEXT',
        'DOCKER_TLS',
        'DOCKER_TLS_VERIFY',
        'DOCKER_CERT_PATH',
        'DOCKER_CONFIG'
    )) {
        [void]$forcedClearNames.Add($name)
        [void]$environmentNames.Add($name)
    }

    $processEnvironment = [Environment]::GetEnvironmentVariables('Process')
    $previous = @{}
    try {
        foreach ($name in $environmentNames) {
            $previous[$name] = [pscustomobject]@{
                Present = $processEnvironment.Contains($name)
                Value = [Environment]::GetEnvironmentVariable($name, 'Process')
            }
            $safeValue = if ($forcedClearNames.Contains($name)) {
                $null
            }
            elseif ($runtimeValues.ContainsKey($name)) {
                [string]$runtimeValues[$name]
            }
            else {
                $null
            }
            [Environment]::SetEnvironmentVariable($name, $safeValue, 'Process')
        }
        $result = & $CommandInvoker $DockerExecutable ([string[]]$allArguments)
    }
    finally {
        foreach ($name in $previous.Keys) {
            $prior = $previous[$name]
            [Environment]::SetEnvironmentVariable(
                $name,
                $(if ($prior.Present) { [string]$prior.Value } else { $null }),
                'Process'
            )
        }
    }
    if ($null -eq $result -or [int]$result.ExitCode -ne 0) {
        throw 'COMPOSE_COMMAND_FAILED'
    }
    return @($result.Output)
}

function Get-JobAgentComposeContainers {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [string]$RuntimeEnvPath
    )

    $output = Invoke-JobAgentCompose `
        -RepositoryPath $RepositoryPath `
        -RuntimeEnvPath $RuntimeEnvPath `
        -Arguments @('ps', '--all', '--format', 'json')
    $text = ($output -join "`n").Trim()
    if (-not $text) {
        return @()
    }
    try {
        return @($text | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        $containers = foreach ($line in ($text -split "`r?`n")) {
            if ($line.Trim()) {
                $line | ConvertFrom-Json -ErrorAction Stop
            }
        }
        return @($containers)
    }
}

function Get-JobAgentListeners {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateRange(1024, 65535)]
        [int]$Port
    )

    return @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Assert-JobAgentLoopbackUrl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url
    )

    try {
        $uri = [uri]$Url
    }
    catch {
        throw 'DASHBOARD_URL_INVALID'
    }
    if (
        $uri.Scheme -ne 'http' -or
        $uri.Host -ne '127.0.0.1' -or
        $uri.UserInfo -or
        $uri.Query -or
        $uri.Fragment -or
        $uri.AbsolutePath -ne '/'
    ) {
        throw 'DASHBOARD_URL_NOT_EXACT_LOOPBACK'
    }
    return $uri
}

function Invoke-JobAgentHttpRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [uri]$Uri,

        [hashtable]$Headers = @{}
    )

    $response = Invoke-WebRequest `
        -Uri $Uri `
        -Headers $Headers `
        -Method Get `
        -TimeoutSec 10 `
        -SkipHttpErrorCheck `
        -MaximumRedirection 0
    return [pscustomobject]@{
        StatusCode = [int]$response.StatusCode
        Content = [string]$response.Content
    }
}

function ConvertFrom-JobAgentJsonResponse {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [object]$Response,

        [Parameter(Mandatory = $true)]
        [int]$ExpectedStatus
    )

    if ([int]$Response.StatusCode -ne $ExpectedStatus) {
        throw 'RUNTIME_HTTP_STATUS_INVALID'
    }
    try {
        return ([string]$Response.Content) | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'RUNTIME_HTTP_JSON_INVALID'
    }
}

function Get-JobAgentHtmlMeta {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Html,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $escaped = [regex]::Escape($Name)
    $match = [regex]::Match(
        $Html,
        '<meta\s+name="' + $escaped + '"\s+content="([^"]+)"\s*/?>',
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if (-not $match.Success) {
        throw "RUNTIME_META_MISSING_$Name"
    }
    return [System.Net.WebUtility]::HtmlDecode($match.Groups[1].Value)
}

function Test-JobAgentStableRuntime {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$DashboardUrl,

        [Parameter(Mandatory = $true)]
        [string]$OperatorToken,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}(?:[0-9a-f]{24})?$')]
        [string]$ExpectedBuildSha,

        [scriptblock]$RequestInvoker = ${function:Invoke-JobAgentHttpRequest},

        [scriptblock]$DelayInvoker = { param([int]$Milliseconds) Start-Sleep -Milliseconds $Milliseconds }
    )

    $base = Assert-JobAgentLoopbackUrl -Url $DashboardUrl
    if ($OperatorToken.Length -lt 32 -or $OperatorToken -match '[\r\n]') {
        throw 'OPERATOR_TOKEN_INVALID'
    }
    $headers = @{ Authorization = "Bearer $OperatorToken" }
    $live = ConvertFrom-JobAgentJsonResponse `
        -Response (& $RequestInvoker ([uri]::new($base, '/health/live')) @{}) `
        -ExpectedStatus 200
    if ([string]$live.status -ne 'ok') {
        throw 'RUNTIME_LIVENESS_FAILED'
    }
    $ready = ConvertFrom-JobAgentJsonResponse `
        -Response (& $RequestInvoker ([uri]::new($base, '/health/ready')) @{}) `
        -ExpectedStatus 200
    if ([string]$ready.status -ne 'ready') {
        throw 'RUNTIME_READINESS_FAILED'
    }

    $runtimeUri = [uri]::new($base, '/api/runtime/capabilities')
    $first = ConvertFrom-JobAgentJsonResponse `
        -Response (& $RequestInvoker $runtimeUri $headers) `
        -ExpectedStatus 200
    & $DelayInvoker 250
    $second = ConvertFrom-JobAgentJsonResponse `
        -Response (& $RequestInvoker $runtimeUri $headers) `
        -ExpectedStatus 200
    $fields = @(
        'build_sha',
        'ui_asset_digest',
        'source_digest',
        'protocol_version',
        'boot_id',
        'started_at'
    )
    foreach ($field in $fields) {
        $firstValue = [string]$first.release.$field
        $secondValue = [string]$second.release.$field
        if (-not $firstValue -or -not [string]::Equals(
            $firstValue,
            $secondValue,
            [System.StringComparison]::Ordinal
        )) {
            throw "RUNTIME_IDENTITY_UNSTABLE_$field"
        }
    }
    if ([string]$first.release.build_sha -ne $ExpectedBuildSha) {
        throw 'RUNTIME_BUILD_MISMATCH'
    }
    foreach ($digestField in @('ui_asset_digest', 'source_digest')) {
        if ([string]$first.release.$digestField -notmatch '^sha256:[0-9a-f]{64}$') {
            throw "RUNTIME_DIGEST_INVALID_$digestField"
        }
    }
    if ([string]$first.release.protocol_version -ne $script:RuntimeProtocolVersion) {
        throw 'RUNTIME_PROTOCOL_MISMATCH'
    }
    try {
        [void][guid]::Parse([string]$first.release.boot_id)
        [void][datetimeoffset]::Parse([string]$first.release.started_at)
    }
    catch {
        throw 'RUNTIME_BOOT_IDENTITY_INVALID'
    }
    if (
        [string]$first.readiness.status -ne 'ready' -or
        -not [bool]$first.worker.compatible -or
        -not [bool]$first.mode.dry_run -or
        -not [bool]$first.mode.draft_only -or
        [bool]$first.mode.live_submit_enabled -or
        [bool]$first.submission.allowed
    ) {
        throw 'RUNTIME_CAPABILITY_UNSAFE'
    }

    $dashboardResponse = & $RequestInvoker $base @{}
    if ([int]$dashboardResponse.StatusCode -ne 200) {
        throw 'DASHBOARD_HTTP_STATUS_INVALID'
    }
    $metaMap = @{
        build_sha = 'job-agent-build-sha'
        ui_asset_digest = 'job-agent-ui-digest'
        source_digest = 'job-agent-source-digest'
        protocol_version = 'job-agent-protocol'
        boot_id = 'job-agent-boot-id'
    }
    foreach ($entry in $metaMap.GetEnumerator()) {
        $meta = Get-JobAgentHtmlMeta -Html ([string]$dashboardResponse.Content) -Name $entry.Value
        if (-not [string]::Equals(
            $meta,
            [string]$first.release.($entry.Key),
            [System.StringComparison]::Ordinal
        )) {
            throw "DASHBOARD_IDENTITY_MISMATCH_$($entry.Key)"
        }
    }
    return [pscustomobject]@{
        Status = 'Verified'
        BuildSha = [string]$first.release.build_sha
        UiAssetDigest = [string]$first.release.ui_asset_digest
        SourceDigest = [string]$first.release.source_digest
        ProtocolVersion = [string]$first.release.protocol_version
        BootId = [string]$first.release.boot_id
        StartedAt = [string]$first.release.started_at
    }
}

function Wait-JobAgentStableRuntime {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$DashboardUrl,

        [Parameter(Mandatory = $true)]
        [string]$OperatorToken,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedBuildSha,

        [ValidateRange(10, 900)]
        [int]$TimeoutSeconds = 300,

        [scriptblock]$RequestInvoker = ${function:Invoke-JobAgentHttpRequest},

        [scriptblock]$DelayInvoker = { param([int]$Milliseconds) Start-Sleep -Milliseconds $Milliseconds }
    )

    $deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastCode = 'RUNTIME_NOT_READY'
    do {
        try {
            return Test-JobAgentStableRuntime `
                -DashboardUrl $DashboardUrl `
                -OperatorToken $OperatorToken `
                -ExpectedBuildSha $ExpectedBuildSha `
                -RequestInvoker $RequestInvoker `
                -DelayInvoker $DelayInvoker
        }
        catch {
            $lastCode = $_.Exception.Message
        }
        & $DelayInvoker 2000
    } while ([datetime]::UtcNow -lt $deadline)
    throw "RUNTIME_VERIFICATION_TIMEOUT:$lastCode"
}

function Open-JobAgentDashboard {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [Parameter(Mandatory = $true)]
        [string]$DashboardUrl,

        [Parameter(Mandatory = $true)]
        [string]$OperatorToken,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedBuildSha,

        [ValidateRange(10, 900)]
        [int]$TimeoutSeconds = 300,

        [scriptblock]$RequestInvoker = ${function:Invoke-JobAgentHttpRequest},

        [scriptblock]$DelayInvoker = { param([int]$Milliseconds) Start-Sleep -Milliseconds $Milliseconds },

        [scriptblock]$BrowserLauncher = { param([string]$Url) Start-Process $Url }
    )

    if ($WhatIfPreference) {
        $PSCmdlet.ShouldProcess($DashboardUrl, 'Verify runtime and open dashboard once') | Out-Null
        return [pscustomobject]@{
            Opened = $false
            Verified = $false
            Reason = 'WhatIf'
        }
    }
    $snapshot = Wait-JobAgentStableRuntime `
        -DashboardUrl $DashboardUrl `
        -OperatorToken $OperatorToken `
        -ExpectedBuildSha $ExpectedBuildSha `
        -TimeoutSeconds $TimeoutSeconds `
        -RequestInvoker $RequestInvoker `
        -DelayInvoker $DelayInvoker
    if (-not $PSCmdlet.ShouldProcess($DashboardUrl, 'Open verified local dashboard once')) {
        return [pscustomobject]@{
            Opened = $false
            Verified = $true
            Snapshot = $snapshot
        }
    }
    & $BrowserLauncher $DashboardUrl
    return [pscustomobject]@{
        Opened = $true
        Verified = $true
        Snapshot = $snapshot
    }
}

function Read-JobAgentStrictIdentityJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath,

        [Parameter(Mandatory = $true)]
        [string[]]$ExpectedProperties,

        [Parameter(Mandatory = $true)]
        [string]$ErrorCode,

        [ValidateRange(1, 65536)]
        [int]$MaximumBytes = 65536
    )

    if (
        -not (Test-Path -LiteralPath $LiteralPath -PathType Leaf) -or
        (Test-JobAgentRawReparseAncestor -LiteralPath $LiteralPath)
    ) {
        throw $ErrorCode
    }
    try {
        $item = Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
        if ($item.Length -lt 2 -or $item.Length -gt $MaximumBytes) {
            throw $ErrorCode
        }
        $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
        $text = [System.IO.File]::ReadAllText($LiteralPath, $strictUtf8)
        $document = [System.Text.Json.JsonDocument]::Parse($text)
        try {
            if (
                $document.RootElement.ValueKind -ne
                [System.Text.Json.JsonValueKind]::Object
            ) {
                throw $ErrorCode
            }
            $expected = [System.Collections.Generic.HashSet[string]]::new(
                [System.StringComparer]::Ordinal
            )
            foreach ($name in $ExpectedProperties) {
                if (-not $expected.Add($name)) {
                    throw $ErrorCode
                }
            }
            $seen = [System.Collections.Generic.HashSet[string]]::new(
                [System.StringComparer]::Ordinal
            )
            $values = @{}
            foreach ($property in $document.RootElement.EnumerateObject()) {
                if (-not $seen.Add($property.Name)) {
                    throw $ErrorCode
                }
                switch ($property.Value.ValueKind) {
                    ([System.Text.Json.JsonValueKind]::String) {
                        $values[$property.Name] = $property.Value.GetString()
                    }
                    ([System.Text.Json.JsonValueKind]::Number) {
                        $values[$property.Name] = $property.Value.GetInt64()
                    }
                    ([System.Text.Json.JsonValueKind]::True) {
                        $values[$property.Name] = $true
                    }
                    ([System.Text.Json.JsonValueKind]::False) {
                        $values[$property.Name] = $false
                    }
                    default {
                        throw $ErrorCode
                    }
                }
            }
            if (-not $expected.SetEquals($seen)) {
                throw $ErrorCode
            }
        }
        finally {
            $document.Dispose()
        }
    }
    catch {
        throw $ErrorCode
    }
    return $values
}

function ConvertFrom-JobAgentStrictIdentityJsonText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Text,

        [Parameter(Mandatory = $true)]
        [string[]]$ExpectedProperties,

        [Parameter(Mandatory = $true)]
        [string]$ErrorCode,

        [ValidateRange(1, 16384)]
        [int]$MaximumCharacters = 16384
    )

    try {
        if (
            [string]::IsNullOrWhiteSpace($Text) -or
            $Text.Length -gt $MaximumCharacters
        ) {
            throw $ErrorCode
        }
        $document = [System.Text.Json.JsonDocument]::Parse($Text)
        try {
            if (
                $document.RootElement.ValueKind -ne
                [System.Text.Json.JsonValueKind]::Object
            ) {
                throw $ErrorCode
            }
            $expected = [System.Collections.Generic.HashSet[string]]::new(
                [System.StringComparer]::Ordinal
            )
            foreach ($name in $ExpectedProperties) {
                if (-not $expected.Add($name)) {
                    throw $ErrorCode
                }
            }
            $seen = [System.Collections.Generic.HashSet[string]]::new(
                [System.StringComparer]::Ordinal
            )
            $values = @{}
            foreach ($property in $document.RootElement.EnumerateObject()) {
                if (
                    -not $seen.Add($property.Name) -or
                    $property.Value.ValueKind -ne
                        [System.Text.Json.JsonValueKind]::String
                ) {
                    throw $ErrorCode
                }
                $values[$property.Name] = $property.Value.GetString()
            }
            if (-not $expected.SetEquals($seen)) {
                throw $ErrorCode
            }
        }
        finally {
            $document.Dispose()
        }
    }
    catch {
        throw $ErrorCode
    }
    return $values
}

function Invoke-JobAgentIdentityPrivateValidation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonExecutable,

        [Parameter(Mandatory = $true)]
        [string]$IdentityScriptPath,

        [Parameter(Mandatory = $true)]
        [string]$IdentityRoot,

        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath
    )

    try {
        $repository = ConvertTo-JobAgentCanonicalPath `
            -LiteralPath $RepositoryPath `
            -RequireExisting
        $identityScript = ConvertTo-JobAgentCanonicalPath `
            -LiteralPath $IdentityScriptPath `
            -RequireExisting
        $expectedIdentityScript = ConvertTo-JobAgentCanonicalPath `
            -LiteralPath (Join-Path $repository 'scripts\control_plane_identity.py') `
            -RequireExisting
        if (
            -not [string]::Equals(
                $identityScript,
                $expectedIdentityScript,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            throw 'IDENTITY_PRIVATE_VALIDATOR_INVALID'
        }
        $python = (
            Get-Command `
                -Name $PythonExecutable `
                -CommandType Application `
                -ErrorAction Stop
        ).Source
        $output = @(
            & $python -I -B $identityScript validate-selection `
                --root $IdentityRoot `
                --repository-root $repository 2>&1
        )
        if ($LASTEXITCODE -ne 0) {
            throw 'IDENTITY_PRIVATE_VALIDATION_FAILED'
        }
        $publicResult = ConvertFrom-JobAgentStrictIdentityJsonText `
            -Text ($output -join "`n") `
            -ExpectedProperties @(
                'control_plane_url',
                'device_id',
                'vercel_environment',
                'vercel_project_id',
                'vercel_scope_id',
                'version_id'
            ) `
            -ErrorCode 'IDENTITY_PRIVATE_VALIDATION_OUTPUT_INVALID'
    }
    catch {
        if ($_.Exception.Message -like 'IDENTITY_PRIVATE_*') {
            throw
        }
        throw 'IDENTITY_PRIVATE_VALIDATION_FAILED'
    }
    return $publicResult
}

function Assert-JobAgentIdentityPrivateBinding {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Selection,

        [Parameter(Mandatory = $true)]
        [pscustomobject]$Layout,

        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [string]$PythonExecutable,

        [Parameter(Mandatory = $true)]
        [scriptblock]$ValidatorInvoker
    )

    if (
        [string]::IsNullOrWhiteSpace($RepositoryPath) -or
        [string]::IsNullOrWhiteSpace($PythonExecutable)
    ) {
        throw 'IDENTITY_PRIVATE_VALIDATION_REQUIRED'
    }
    $repository = ConvertTo-JobAgentCanonicalPath `
        -LiteralPath $RepositoryPath `
        -RequireExisting
    $identityScript = Join-Path $repository 'scripts\control_plane_identity.py'
    try {
        $results = @(
            & $ValidatorInvoker `
                $PythonExecutable `
                $identityScript `
                $Layout.Identity `
                $repository
        )
    }
    catch {
        throw 'IDENTITY_PRIVATE_VALIDATION_FAILED'
    }
    if (
        $results.Count -ne 1 -or
        $results[0] -isnot [System.Collections.IDictionary]
    ) {
        throw 'IDENTITY_PRIVATE_VALIDATION_OUTPUT_INVALID'
    }
    $validated = $results[0]
    $expectedProperties = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($name in @(
        'control_plane_url',
        'device_id',
        'vercel_environment',
        'vercel_project_id',
        'vercel_scope_id',
        'version_id'
    )) {
        $expectedProperties.Add($name) | Out-Null
    }
    $actualProperties = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($name in $validated.Keys) {
        if (
            $name -isnot [string] -or
            -not $actualProperties.Add($name) -or
            $validated[$name] -isnot [string]
        ) {
            throw 'IDENTITY_PRIVATE_VALIDATION_OUTPUT_INVALID'
        }
    }
    if (-not $expectedProperties.SetEquals($actualProperties)) {
        throw 'IDENTITY_PRIVATE_VALIDATION_OUTPUT_INVALID'
    }
    if (
        -not [string]::Equals(
            $validated['version_id'],
            $Selection.VersionId,
            [System.StringComparison]::Ordinal
        ) -or
        -not [string]::Equals(
            $validated['device_id'],
            $Selection.DeviceId,
            [System.StringComparison]::Ordinal
        ) -or
        -not [string]::Equals(
            $validated['control_plane_url'],
            $Selection.ControlPlaneUrl,
            [System.StringComparison]::Ordinal
        ) -or
        -not [string]::Equals(
            $validated['vercel_environment'],
            $Selection.VercelEnvironment,
            [System.StringComparison]::Ordinal
        ) -or
        -not [string]::Equals(
            $validated['vercel_project_id'],
            $Selection.VercelProjectId,
            [System.StringComparison]::Ordinal
        ) -or
        -not [string]::Equals(
            $validated['vercel_scope_id'],
            $Selection.VercelScopeId,
            [System.StringComparison]::Ordinal
        )
    ) {
        throw 'IDENTITY_PRIVATE_VALIDATION_MISMATCH'
    }
    return $true
}

function Get-JobAgentIdentitySelection {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Layout,

        [string]$ExpectedControlPlaneUrl = '',

        [string]$ExpectedVercelEnvironment = '',

        [string]$ExpectedVercelProjectId = '',

        [string]$ExpectedVercelScopeId = '',

        [string]$RepositoryPath = '',

        [string]$PythonExecutable = '',

        [switch]$RequirePrivateValidation,

        [scriptblock]$PrivateValidatorInvoker = ${function:Invoke-JobAgentIdentityPrivateValidation}
    )

    if (-not (Test-Path -LiteralPath $Layout.IdentityCurrent -PathType Leaf)) {
        return $null
    }
    if (Test-JobAgentRawReparseAncestor -LiteralPath $Layout.IdentityCurrent) {
        throw 'IDENTITY_SELECTION_REPARSE_POINT'
    }
    $expectedTargetValues = @(
        $ExpectedVercelEnvironment,
        $ExpectedVercelProjectId,
        $ExpectedVercelScopeId
    )
    $specifiedTargetValues = @(
        $expectedTargetValues | Where-Object {
            -not [string]::IsNullOrWhiteSpace($_)
        }
    )
    if ($specifiedTargetValues.Count -notin @(0, 3)) {
        throw 'IDENTITY_VERCEL_TARGET_EXPECTATION_INCOMPLETE'
    }
    if ($specifiedTargetValues.Count -eq 3) {
        if ($ExpectedVercelEnvironment -notin @('production', 'preview')) {
            throw 'IDENTITY_VERCEL_ENVIRONMENT_INVALID'
        }
        if ($ExpectedVercelProjectId -notmatch '^prj_[A-Za-z0-9]{8,120}$') {
            throw 'IDENTITY_VERCEL_PROJECT_INVALID'
        }
        if ($ExpectedVercelScopeId -notmatch '^team_[A-Za-z0-9]{8,120}$') {
            throw 'IDENTITY_VERCEL_SCOPE_INVALID'
        }
    }
    $current = Read-JobAgentStrictIdentityJson `
        -LiteralPath $Layout.IdentityCurrent `
        -ExpectedProperties @('schema_version', 'version_id', 'bundle_path') `
        -ErrorCode 'IDENTITY_SELECTION_INVALID'
    if (
        $current['schema_version'] -isnot [long] -or
        $current['schema_version'] -ne 2 -or
        $current['version_id'] -isnot [string] -or
        $current['version_id'] -notmatch (
            '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        ) -or
        $current['bundle_path'] -isnot [string]
    ) {
        throw 'IDENTITY_SELECTION_INVALID'
    }
    $bundle = ConvertTo-JobAgentCanonicalPath -LiteralPath $current['bundle_path']
    $expectedBundle = ConvertTo-JobAgentCanonicalPath -LiteralPath (
        Join-Path (
            Join-Path $Layout.Identity 'versions'
        ) $current['version_id']
    )
    if (
        -not (Test-JobAgentPathWithin -ChildPath $bundle -ParentPath $Layout.Identity) -or
        -not [string]::Equals(
            $bundle,
            $expectedBundle,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -or
        -not (Test-Path -LiteralPath $bundle -PathType Container) -or
        (Test-JobAgentRawReparseAncestor -LiteralPath $bundle)
    ) {
        throw 'IDENTITY_SELECTION_EXTERNAL'
    }
    $manifestPath = Join-Path $bundle 'manifest.json'
    $manifest = Read-JobAgentStrictIdentityJson `
        -LiteralPath $manifestPath `
        -ExpectedProperties @(
            'schema_version',
            'version_id',
            'created_at',
            'device_id',
            'device_public_key',
            'control_signing_key_id',
            'control_public_key',
            'control_audience',
            'runner_audience',
            'control_plane_url',
            'vercel_environment',
            'vercel_project_id',
            'vercel_scope_id',
            'runner_config_path',
            'secret_bundle_path'
        ) `
        -ErrorCode 'IDENTITY_MANIFEST_INVALID'
    $config = Join-Path $bundle 'runner.json'
    $runner = Read-JobAgentStrictIdentityJson `
        -LiteralPath $config `
        -ExpectedProperties @(
            'control_plane_url',
            'device_id',
            'control_signing_key_id',
            'control_plane_audience',
            'private_key_path',
            'control_plane_public_key_path',
            'runtime_env_path',
            'poll_interval_seconds',
            'heartbeat_interval_seconds',
            'offline_after_seconds'
        ) `
        -ErrorCode 'RUNNER_CONFIG_INVALID'
    try {
        $uuidPattern = (
            '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-' +
            '[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        )
        $createdAt = [string]$manifest['created_at']
        $parsedCreatedAt = [System.DateTimeOffset]::MinValue
        if (
            $manifest['schema_version'] -isnot [long] -or
            $manifest['schema_version'] -ne 2 -or
            $manifest['version_id'] -isnot [string] -or
            $manifest['version_id'] -ne $current['version_id'] -or
            $manifest['created_at'] -isnot [string] -or
            $manifest['device_id'] -isnot [string] -or
            $manifest['device_id'] -notmatch $uuidPattern -or
            $manifest['control_signing_key_id'] -isnot [string] -or
            $manifest['control_signing_key_id'] -notmatch $uuidPattern -or
            $manifest['device_public_key'] -isnot [string] -or
            $manifest['device_public_key'] -notmatch '^[A-Za-z0-9_-]{43}$' -or
            $manifest['control_public_key'] -isnot [string] -or
            $manifest['control_public_key'] -notmatch '^[A-Za-z0-9_-]{43}$' -or
            $manifest['control_audience'] -isnot [string] -or
            $manifest['control_audience'] -ne 'job-apply-control-plane' -or
            $manifest['runner_audience'] -isnot [string] -or
            $manifest['runner_audience'] -ne 'job-apply-private-runner' -or
            $manifest['control_plane_url'] -isnot [string] -or
            $manifest['vercel_environment'] -isnot [string] -or
            $manifest['vercel_environment'] -notin @('production', 'preview') -or
            $manifest['vercel_project_id'] -isnot [string] -or
            $manifest['vercel_project_id'] -notmatch '^prj_[A-Za-z0-9]{8,120}$' -or
            $manifest['vercel_scope_id'] -isnot [string] -or
            $manifest['vercel_scope_id'] -notmatch '^team_[A-Za-z0-9]{8,120}$' -or
            $manifest['runner_config_path'] -isnot [string] -or
            $manifest['secret_bundle_path'] -isnot [string] -or
            $createdAt -notmatch (
                '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}' +
                '(?:\.\d{1,6})?Z$'
            ) -or
            -not [System.DateTimeOffset]::TryParse(
                $createdAt,
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::RoundtripKind,
                [ref]$parsedCreatedAt
            )
        ) {
            throw 'IDENTITY_MANIFEST_INVALID'
        }
        $configuredUrl = ([string]$manifest['control_plane_url']).TrimEnd('/')
        $configuredUri = [uri]$configuredUrl
        $manifestRunnerConfig = ConvertTo-JobAgentCanonicalPath `
            -LiteralPath ([string]$manifest['runner_config_path'])
        $secretBundle = ConvertTo-JobAgentCanonicalPath `
            -LiteralPath ([string]$manifest['secret_bundle_path'])
        $expectedSecretBundle = ConvertTo-JobAgentCanonicalPath `
            -LiteralPath (Join-Path $bundle 'control-secrets.dpapi')
        if (
            -not [string]::Equals(
                $manifestRunnerConfig,
                (ConvertTo-JobAgentCanonicalPath -LiteralPath $config),
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            -not [string]::Equals(
                $secretBundle,
                $expectedSecretBundle,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            -not (Test-Path -LiteralPath $secretBundle -PathType Leaf) -or
            (Test-JobAgentRawReparseAncestor -LiteralPath $secretBundle) -or
            (Get-Item -LiteralPath $secretBundle -Force).Length -lt 1 -or
            (Get-Item -LiteralPath $secretBundle -Force).Length -gt 32768
        ) {
            throw 'IDENTITY_MANIFEST_INVALID'
        }
        foreach ($publicKey in @(
            @{
                Path = Join-Path $bundle 'runner-public.key'
                Expected = $manifest['device_public_key']
            },
            @{
                Path = Join-Path $bundle 'control-public.key'
                Expected = $manifest['control_public_key']
            }
        )) {
            if (
                -not (Test-Path -LiteralPath $publicKey.Path -PathType Leaf) -or
                (Test-JobAgentRawReparseAncestor -LiteralPath $publicKey.Path)
            ) {
                throw 'IDENTITY_MANIFEST_INVALID'
            }
            $publicKeyText = (
                [System.IO.File]::ReadAllText(
                    $publicKey.Path,
                    [System.Text.Encoding]::ASCII
                )
            ).TrimEnd("`r", "`n")
            if (
                $publicKeyText -notmatch '^[A-Za-z0-9_-]{43}$' -or
                -not [string]::Equals(
                    $publicKeyText,
                    [string]$publicKey.Expected,
                    [System.StringComparison]::Ordinal
                )
            ) {
                throw 'IDENTITY_MANIFEST_INVALID'
            }
        }
        if (
            $runner['control_plane_url'] -isnot [string] -or
            $runner['device_id'] -isnot [string] -or
            $runner['device_id'] -ne $manifest['device_id'] -or
            $runner['control_signing_key_id'] -isnot [string] -or
            $runner['control_signing_key_id'] -ne
                $manifest['control_signing_key_id'] -or
            $runner['control_plane_audience'] -isnot [string] -or
            $runner['control_plane_audience'] -ne 'job-apply-control-plane' -or
            $runner['private_key_path'] -isnot [string] -or
            $runner['control_plane_public_key_path'] -isnot [string] -or
            $runner['runtime_env_path'] -isnot [string] -or
            $runner['poll_interval_seconds'] -isnot [long] -or
            $runner['poll_interval_seconds'] -ne 10 -or
            $runner['heartbeat_interval_seconds'] -isnot [long] -or
            $runner['heartbeat_interval_seconds'] -ne 10 -or
            $runner['offline_after_seconds'] -isnot [long] -or
            $runner['offline_after_seconds'] -ne 30
        ) {
            throw 'RUNNER_CONFIG_INVALID'
        }
        $runtimeEnv = ConvertTo-JobAgentCanonicalPath `
            -LiteralPath ([string]$runner['runtime_env_path'])
        if (-not [string]::Equals(
            $runtimeEnv,
            (ConvertTo-JobAgentCanonicalPath -LiteralPath $Layout.RuntimeEnv),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'IDENTITY_RUNTIME_ENV_MISMATCH'
        }
        foreach ($pathName in @(
            'private_key_path',
            'control_plane_public_key_path'
        )) {
            $keyPath = ConvertTo-JobAgentCanonicalPath `
                -LiteralPath ([string]$runner[$pathName])
            $expectedKeyPath = if ($pathName -eq 'private_key_path') {
                Join-Path $bundle 'runner-private.key'
            }
            else {
                Join-Path $bundle 'control-public.key'
            }
            if (
                -not [string]::Equals(
                    $keyPath,
                    (ConvertTo-JobAgentCanonicalPath -LiteralPath $expectedKeyPath),
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -or
                -not (Test-JobAgentPathWithin -ChildPath $keyPath -ParentPath $bundle) -or
                -not (Test-Path -LiteralPath $keyPath -PathType Leaf) -or
                (Test-JobAgentRawReparseAncestor -LiteralPath $keyPath)
            ) {
                throw "IDENTITY_KEY_PATH_INVALID_$pathName"
            }
        }
        if (
            $configuredUri.Scheme -ne 'https' -or
            -not $configuredUri.Host -or
            $configuredUri.UserInfo -or
            $configuredUri.Query -or
            $configuredUri.Fragment -or
            $configuredUri.AbsolutePath -ne '/'
        ) {
            throw 'IDENTITY_CONTROL_PLANE_URL_INVALID'
        }
        if (
            -not [string]::Equals(
                ([string]$runner['control_plane_url']).TrimEnd('/'),
                $configuredUrl,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            throw 'IDENTITY_CONTROL_PLANE_URL_MISMATCH'
        }
        if (
            -not [string]::IsNullOrWhiteSpace($ExpectedControlPlaneUrl) -and
            -not [string]::Equals(
                $configuredUrl,
                $ExpectedControlPlaneUrl.TrimEnd('/'),
                [System.StringComparison]::OrdinalIgnoreCase
            )
        ) {
            throw 'IDENTITY_CONTROL_PLANE_URL_MISMATCH'
        }
        if ($specifiedTargetValues.Count -eq 3) {
            if ($manifest['vercel_environment'] -ne $ExpectedVercelEnvironment) {
                throw 'IDENTITY_VERCEL_ENVIRONMENT_MISMATCH'
            }
            if ($manifest['vercel_project_id'] -ne $ExpectedVercelProjectId) {
                throw 'IDENTITY_VERCEL_PROJECT_MISMATCH'
            }
            if ($manifest['vercel_scope_id'] -ne $ExpectedVercelScopeId) {
                throw 'IDENTITY_VERCEL_SCOPE_MISMATCH'
            }
        }
    }
    catch {
        if (
            $_.Exception.Message -like 'IDENTITY_*' -or
            $_.Exception.Message -like 'RUNNER_*'
        ) {
            throw
        }
        throw 'RUNNER_CONFIG_INVALID'
    }
    $selection = [pscustomobject]@{
        VersionId = [string]$current['version_id']
        DeviceId = [string]$manifest['device_id']
        BundlePath = $bundle
        RunnerConfigPath = $config
        ControlPlaneUrl = $configuredUrl
        RuntimeEnvPath = $runtimeEnv
        VercelEnvironment = [string]$manifest['vercel_environment']
        VercelProjectId = [string]$manifest['vercel_project_id']
        VercelScopeId = [string]$manifest['vercel_scope_id']
        PrivateBindingValidated = $false
    }
    if ($RequirePrivateValidation) {
        Assert-JobAgentIdentityPrivateBinding `
            -Selection $selection `
            -Layout $Layout `
            -RepositoryPath $RepositoryPath `
            -PythonExecutable $PythonExecutable `
            -ValidatorInvoker $PrivateValidatorInvoker | Out-Null
        $selection.PrivateBindingValidated = $true
    }
    return $selection
}

function Invoke-JobAgentBootstrap {
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^https://[^/?#]+/?$')]
        [string]$ControlPlaneUrl,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^prj_[A-Za-z0-9]{8,120}$')]
        [string]$VercelProjectId,

        [Parameter(Mandatory = $true)]
        [ValidatePattern('^team_[A-Za-z0-9]{8,120}$')]
        [string]$VercelScopeId,

        [string]$PythonExecutable = (Get-Command python -ErrorAction Stop).Source,

        [string]$LocalAppDataRoot = $env:LOCALAPPDATA,

        [string]$TaskName = $script:RunnerTaskName,

        [switch]$AdoptExistingTask,

        [switch]$RepairOwnedTask,

        [switch]$UpgradeRelease
    )

    $repository = ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath -RequireExisting
    if (-not (Test-Path -LiteralPath (Join-Path $repository 'docker-compose.yml') -PathType Leaf)) {
        throw 'COMPOSE_FILE_UNAVAILABLE'
    }
    $layout = Get-JobAgentLayout -LocalAppDataRoot $LocalAppDataRoot
    Assert-JobAgentExternalLayout `
        -Layout $layout `
        -LocalAppDataRoot $LocalAppDataRoot `
        -RepositoryPath $repository | Out-Null
    if (-not $PSCmdlet.ShouldProcess(
        $layout.Root,
        'Provision external fail-closed runtime, device identity, and owned runner task'
    )) {
        return [pscustomobject]@{
            Applied = $false
            Reason = 'WhatIf'
            Root = $layout.Root
            RuntimeEnv = $layout.RuntimeEnv
            TaskName = $TaskName
        }
    }

    $build = Get-JobAgentBuildSha `
        -RepositoryPath $repository `
        -RequireClean `
        -RequireMain
    $mutex = Enter-JobAgentRuntimeMutex -RepositoryPath $repository
    try {
        $selection = Get-JobAgentIdentitySelection `
            -Layout $layout `
            -ExpectedControlPlaneUrl $ControlPlaneUrl `
            -ExpectedVercelEnvironment 'production' `
            -ExpectedVercelProjectId $VercelProjectId `
            -ExpectedVercelScopeId $VercelScopeId `
            -RepositoryPath $repository `
            -PythonExecutable $PythonExecutable `
            -RequirePrivateValidation
        Initialize-JobAgentExternalLayout `
            -Layout $layout `
            -BuildSha $build `
            -UpgradeRelease:$UpgradeRelease `
            -Confirm:$false | Out-Null
        $runtimeValues = Read-JobAgentRuntimeEnvironment -Path $layout.RuntimeEnv
        Assert-JobAgentRuntimeRelease `
            -Values $runtimeValues `
            -ExpectedBuildSha $build | Out-Null
        if ($null -eq $selection) {
            $identityScript = Join-Path $repository 'scripts\control_plane_identity.py'
            $identityOutput = & $PythonExecutable -B $identityScript create `
                --root $layout.Identity `
                --repository-root $repository `
                --control-plane-url $ControlPlaneUrl `
                --runtime-env-path $layout.RuntimeEnv `
                --vercel-environment production `
                --vercel-project-id $VercelProjectId `
                --vercel-scope-id $VercelScopeId 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw 'IDENTITY_PROVISIONING_FAILED'
            }
            try {
                $publicIdentity = ($identityOutput -join "`n") | ConvertFrom-Json -ErrorAction Stop
            }
            catch {
                throw 'IDENTITY_PROVISIONING_OUTPUT_INVALID'
            }
            $provisionedRunnerConfig = [string]$publicIdentity.runner_config_path
            $selection = Get-JobAgentIdentitySelection `
                -Layout $layout `
                -ExpectedControlPlaneUrl $ControlPlaneUrl `
                -ExpectedVercelEnvironment 'production' `
                -ExpectedVercelProjectId $VercelProjectId `
                -ExpectedVercelScopeId $VercelScopeId `
                -RepositoryPath $repository `
                -PythonExecutable $PythonExecutable `
                -RequirePrivateValidation
            if (
                $null -eq $selection -or
                -not [string]::Equals(
                    (ConvertTo-JobAgentCanonicalPath `
                        -LiteralPath $provisionedRunnerConfig),
                    $selection.RunnerConfigPath,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            ) {
                throw 'IDENTITY_PROVISIONING_OUTPUT_INVALID'
            }
            $runnerConfig = $selection.RunnerConfigPath
        }
        else {
            $runnerConfig = $selection.RunnerConfigPath
        }
        $installer = Join-Path $repository 'scripts\install_control_plane_runner.ps1'
        $installArguments = @{
            RepositoryPath = $repository
            PythonExecutable = $PythonExecutable
            ConfigPath = $runnerConfig
            TaskName = $TaskName
            NoStart = $true
            Confirm = $false
        }
        if ($AdoptExistingTask) {
            $installArguments['AdoptExisting'] = $true
        }
        if ($RepairOwnedTask) {
            $installArguments['RepairOwned'] = $true
        }
        & $installer @installArguments | Out-Null
        return [pscustomobject]@{
            Applied = $true
            Root = $layout.Root
            RuntimeEnv = $layout.RuntimeEnv
            RunnerConfig = $runnerConfig
            TaskName = $TaskName
            BuildSha = $build
        }
    }
    finally {
        Exit-JobAgentRuntimeMutex -Handle $mutex
    }
}

function Get-JobAgentTaskState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [string]$PythonExecutable,

        [Parameter(Mandatory = $true)]
        [string]$ConfigPath,

        [string]$TaskName = $script:RunnerTaskName
    )

    $expected = Get-JobAgentExpectedTaskAction `
        -RepositoryPath $RepositoryPath `
        -PythonExecutable $PythonExecutable `
        -ConfigPath $ConfigPath
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $ownership = Get-JobAgentTaskOwnership `
        -Task $task `
        -ExpectedAction $expected `
        -ExpectedUser $user
    return [pscustomobject]@{
        Task = $task
        Ownership = $ownership
    }
}

function Start-JobAgentOwnedRunnerTask {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [string]$PythonExecutable,

        [Parameter(Mandatory = $true)]
        [string]$ConfigPath,

        [string]$TaskName = $script:RunnerTaskName
    )

    $state = Get-JobAgentTaskState `
        -RepositoryPath $RepositoryPath `
        -PythonExecutable $PythonExecutable `
        -ConfigPath $ConfigPath `
        -TaskName $TaskName
    if ($state.Ownership.Classification -ne 'OwnedExact') {
        throw "RUNNER_TASK_NOT_OWNED_EXACT:$($state.Ownership.Classification)"
    }
    if ([string]$state.Task.State -eq 'Running') {
        return [pscustomobject]@{ Started = $false; State = 'Running' }
    }
    if ($PSCmdlet.ShouldProcess($TaskName, 'Start exact owned private runner task')) {
        Start-ScheduledTask -TaskName $TaskName
        return [pscustomobject]@{ Started = $true; State = 'StartRequested' }
    }
    return [pscustomobject]@{ Started = $false; State = 'WhatIf' }
}

function Stop-JobAgentOwnedRunnerTask {
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [Parameter(Mandatory = $true)]
        [string]$PythonExecutable,

        [Parameter(Mandatory = $true)]
        [string]$ConfigPath,

        [string]$TaskName = $script:RunnerTaskName
    )

    $state = Get-JobAgentTaskState `
        -RepositoryPath $RepositoryPath `
        -PythonExecutable $PythonExecutable `
        -ConfigPath $ConfigPath `
        -TaskName $TaskName
    if ($state.Ownership.Classification -eq 'Absent') {
        return [pscustomobject]@{ Stopped = $false; State = 'NotInstalled' }
    }
    if ($state.Ownership.Classification -ne 'OwnedExact') {
        throw "RUNNER_TASK_NOT_OWNED_EXACT:$($state.Ownership.Classification)"
    }
    if ([string]$state.Task.State -ne 'Running') {
        return [pscustomobject]@{ Stopped = $false; State = [string]$state.Task.State }
    }
    if ($PSCmdlet.ShouldProcess($TaskName, 'Stop exact owned private runner task')) {
        Stop-ScheduledTask -TaskName $TaskName
        return [pscustomobject]@{ Stopped = $true; State = 'StopRequested' }
    }
    return [pscustomobject]@{ Stopped = $false; State = 'WhatIf' }
}

function Get-JobAgentEmergencyTaskState {
    [CmdletBinding()]
    param(
        [string]$TaskName = $script:RunnerTaskName
    )

    Assert-JobAgentEmergencyTaskTarget -TaskName $TaskName | Out-Null
    try {
        # Enumerate the scheduler with terminating errors, then perform literal
        # name/path matching ourselves. This cleanly distinguishes a successful
        # enumeration with no exact task from an unavailable scheduler, without
        # sending wildcard-capable caller input to the ScheduledTasks module.
        $scheduledTasks = @(
            Get-ScheduledTask -ErrorAction Stop |
                Where-Object { $null -ne $_ }
        )
    }
    catch {
        throw 'RUNNER_TASK_QUERY_FAILED'
    }
    $nameMatches = [System.Collections.Generic.List[object]]::new()
    foreach ($candidate in $scheduledTasks) {
        $nameProperty = $candidate.PSObject.Properties['TaskName']
        $pathProperty = $candidate.PSObject.Properties['TaskPath']
        if ($null -eq $nameProperty -or $null -eq $pathProperty) {
            throw 'RUNNER_TASK_QUERY_RESULT_INVALID'
        }
        if ([string]::Equals(
            [string]$nameProperty.Value,
            $script:RunnerTaskName,
            [System.StringComparison]::Ordinal
        )) {
            $nameMatches.Add($candidate)
        }
    }
    $alternatePathMatches = @($nameMatches | Where-Object {
        -not [string]::Equals(
            [string]$_.TaskPath,
            $script:RunnerTaskPath,
            [System.StringComparison]::Ordinal
        )
    })
    if ($alternatePathMatches.Count -gt 0) {
        throw 'RUNNER_TASK_PATH_NOT_CANONICAL'
    }
    $exactMatches = @($nameMatches | Where-Object {
        [string]::Equals(
            [string]$_.TaskPath,
            $script:RunnerTaskPath,
            [System.StringComparison]::Ordinal
        )
    })
    if ($exactMatches.Count -gt 1) {
        throw 'RUNNER_TASK_QUERY_AMBIGUOUS'
    }
    $task = if ($exactMatches.Count -eq 1) {
        $exactMatches[0]
    }
    else {
        $null
    }
    return [pscustomobject]@{
        Task = $task
        Ownership = Get-JobAgentEmergencyTaskOwnership -Task $task
    }
}

function Stop-JobAgentEmergencyRunnerTask {
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
    param(
        [string]$TaskName = $script:RunnerTaskName
    )

    Assert-JobAgentEmergencyTaskTarget -TaskName $TaskName | Out-Null
    # Emergency shutdown deliberately avoids the mutable identity pointer. The
    # exact task name and install-time ownership marker authorize only the
    # reversible Stop-ScheduledTask action; start, repair, and removal retain
    # their full identity/action validation.
    $state = Get-JobAgentEmergencyTaskState -TaskName $TaskName
    if ($state.Ownership.Classification -eq 'Absent') {
        return [pscustomobject]@{ Stopped = $false; State = 'NotInstalled' }
    }
    if ($state.Ownership.Classification -ne 'MarkerOwned') {
        throw "RUNNER_TASK_NOT_OWNED_EXACT:$($state.Ownership.Classification)"
    }
    if ([string]$state.Task.State -ne 'Running') {
        return [pscustomobject]@{ Stopped = $false; State = [string]$state.Task.State }
    }
    if ($PSCmdlet.ShouldProcess($TaskName, 'Stop marker-owned private runner task')) {
        Stop-ScheduledTask -InputObject $state.Task -ErrorAction Stop
        return [pscustomobject]@{ Stopped = $true; State = 'StopRequested' }
    }
    return [pscustomobject]@{ Stopped = $false; State = 'WhatIf' }
}

function Get-JobAgentLocalStatus {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [string]$PythonExecutable = (Get-Command python -ErrorAction Stop).Source,

        [string]$LocalAppDataRoot = $env:LOCALAPPDATA,

        [string]$TaskName = $script:RunnerTaskName
    )

    $repository = ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath -RequireExisting
    $layout = Get-JobAgentLayout -LocalAppDataRoot $LocalAppDataRoot
    Assert-JobAgentExternalLayout `
        -Layout $layout `
        -LocalAppDataRoot $LocalAppDataRoot `
        -RepositoryPath $repository | Out-Null
    $selection = $null
    $identityState = 'Absent'
    try {
        $selection = Get-JobAgentIdentitySelection `
            -Layout $layout `
            -RepositoryPath $repository `
            -PythonExecutable $PythonExecutable `
            -RequirePrivateValidation
        if ($null -ne $selection) {
            $identityState = 'Valid'
        }
    }
    catch {
        $selection = $null
        $identityState = 'Invalid'
    }
    $taskClassification = 'Unverifiable'
    try {
        $rawTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $rawTask) {
            $taskClassification = 'Absent'
        }
        elseif ($null -ne $selection) {
            $taskState = Get-JobAgentTaskState `
                -RepositoryPath $repository `
                -PythonExecutable $PythonExecutable `
                -ConfigPath $selection.RunnerConfigPath `
                -TaskName $TaskName
            $taskClassification = $taskState.Ownership.Classification
        }
        elseif ([string]::Equals(
            ([string]$rawTask.Description).Trim(),
            $script:RunnerTaskOwnershipMarker,
            [System.StringComparison]::Ordinal
        )) {
            $taskClassification = 'OwnedUnverifiable'
        }
        else {
            $taskClassification = 'Foreign'
        }
    }
    catch {
        $taskClassification = 'Unverifiable'
    }
    $composeClassification = 'Unverifiable'
    $endpointClassification = 'Unverifiable'
    $runtimeState = 'Unavailable'
    $build = $null
    $repositoryClean = $false
    $mainDerived = $false
    $releaseMatches = $false
    $values = $null
    $currentBuild = $null
    try {
        $currentBuild = Get-JobAgentBuildSha -RepositoryPath $repository
        Get-JobAgentBuildSha -RepositoryPath $repository -RequireClean | Out-Null
        $repositoryClean = $true
    }
    catch {
        $repositoryClean = $false
    }
    try {
        Get-JobAgentBuildSha -RepositoryPath $repository -RequireMain | Out-Null
        $mainDerived = $true
    }
    catch {
        $mainDerived = $false
    }
    if (Test-Path -LiteralPath $layout.RuntimeEnv -PathType Leaf) {
        try {
            $values = Read-JobAgentRuntimeEnvironment -Path $layout.RuntimeEnv
            Assert-JobAgentSafeRuntimeEnvironment -Values $values -Layout $layout | Out-Null
        }
        catch {
            $runtimeState = 'Invalid'
            $values = $null
        }
    }
    if ($null -ne $values) {
        $build = [string]$values['APP_BUILD_SHA']
        if (-not [string]::IsNullOrWhiteSpace([string]$currentBuild)) {
            $releaseMatches = $currentBuild -eq $build
        }
        $port = [int]$values['API_PORT']
        $listeners = @()
        $listenersAvailable = $true
        try {
            $listeners = Get-JobAgentListeners -Port $port
        }
        catch {
            $listenersAvailable = $false
            $endpointClassification = 'Unverifiable'
        }
        if (Get-Command docker -ErrorAction SilentlyContinue) {
            try {
                $containers = Get-JobAgentComposeContainers `
                    -RepositoryPath $repository `
                    -RuntimeEnvPath $layout.RuntimeEnv
                $compose = Get-JobAgentComposeOwnership `
                    -Containers $containers `
                    -RepositoryPath $repository
                $composeClassification = $compose.Classification
                $endpointCandidate = $null
                if ($listenersAvailable) {
                    $endpointCandidate = Get-JobAgentEndpointOwnership `
                        -Listeners $listeners `
                        -Containers $containers `
                        -Port $port
                    $endpointClassification = $endpointCandidate.Classification
                }
                if (
                    $compose.Exact -and
                    $null -ne $endpointCandidate -and
                    $endpointCandidate.MetadataMatched
                ) {
                    try {
                        $verifiedContainers = Get-JobAgentComposeContainers `
                            -RepositoryPath $repository `
                            -RuntimeEnvPath $layout.RuntimeEnv
                        $verifiedCompose = Get-JobAgentComposeOwnership `
                            -Containers $verifiedContainers `
                            -RepositoryPath $repository
                        $composeClassification = $verifiedCompose.Classification
                        if ($verifiedCompose.Classification -ne 'OwnedExact') {
                            throw "COMPOSE_PROJECT_NOT_OWNED_EXACT:$($verifiedCompose.Classification)"
                        }
                        $verifiedListeners = Get-JobAgentListeners -Port $port
                        $verifiedCandidate = Get-JobAgentEndpointOwnership `
                            -Listeners $verifiedListeners `
                            -Containers $verifiedContainers `
                            -Port $port
                        if (-not $verifiedCandidate.MetadataMatched) {
                            throw "API_ENDPOINT_NOT_OWNED_EXACT:$($verifiedCandidate.Classification)"
                        }
                        Test-JobAgentStableRuntime `
                            -DashboardUrl "http://127.0.0.1:$port/" `
                            -OperatorToken ([string]$values['SECRET_KEY']) `
                            -ExpectedBuildSha $build | Out-Null
                        $endpoint = Get-JobAgentEndpointOwnership `
                            -Listeners $verifiedListeners `
                            -Containers $verifiedContainers `
                            -Port $port `
                            -AuthenticatedRuntimeVerified
                        if ($endpoint.Classification -ne 'OwnedExact') {
                            throw "API_ENDPOINT_NOT_OWNED_EXACT:$($endpoint.Classification)"
                        }
                        $endpointClassification = $endpoint.Classification
                        $runtimeState = 'Verified'
                    }
                    catch {
                        $runtimeState = 'NotReady'
                    }
                }
            }
            catch {
                $composeClassification = 'Unavailable'
                if ($listenersAvailable) {
                    $endpoint = Get-JobAgentEndpointOwnership `
                        -Listeners $listeners `
                        -Containers @() `
                        -Port $port
                    $endpointClassification = $endpoint.Classification
                }
            }
        }
        else {
            $composeClassification = 'Unavailable'
            if ($listenersAvailable) {
                $endpoint = Get-JobAgentEndpointOwnership `
                    -Listeners $listeners `
                    -Containers @() `
                    -Port $port
                $endpointClassification = $endpoint.Classification
            }
        }
    }
    else {
        try {
            $fallbackListeners = Get-JobAgentListeners -Port 8000
            $endpointClassification = if (@($fallbackListeners).Count -eq 0) {
                'Absent'
            }
            else {
                'Unverifiable'
            }
        }
        catch {
            $endpointClassification = 'Unverifiable'
        }
    }
    return [pscustomobject]@{
        RootPresent = Test-Path -LiteralPath $layout.Root -PathType Container
        RuntimeEnvironmentPresent = Test-Path -LiteralPath $layout.RuntimeEnv -PathType Leaf
        IdentityPresent = $null -ne $selection
        IdentityState = $identityState
        TaskOwnership = $taskClassification
        ComposeOwnership = $composeClassification
        EndpointOwnership = $endpointClassification
        Runtime = $runtimeState
        BuildSha = $build
        RepositoryClean = $repositoryClean
        MainDerived = $mainDerived
        ReleaseMatchesCurrentHead = $releaseMatches
    }
}

function Invoke-JobAgentStart {
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [string]$PythonExecutable = (Get-Command python -ErrorAction Stop).Source,

        [string]$LocalAppDataRoot = $env:LOCALAPPDATA,

        [string]$TaskName = $script:RunnerTaskName,

        [ValidateRange(10, 900)]
        [int]$TimeoutSeconds = 300
    )

    $repository = ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath -RequireExisting
    $layout = Get-JobAgentLayout -LocalAppDataRoot $LocalAppDataRoot
    Assert-JobAgentExternalLayout `
        -Layout $layout `
        -LocalAppDataRoot $LocalAppDataRoot `
        -RepositoryPath $repository | Out-Null
    if (-not (Test-Path -LiteralPath $layout.RuntimeEnv -PathType Leaf)) {
        throw 'RUNTIME_ENV_UNAVAILABLE'
    }
    $values = Read-JobAgentRuntimeEnvironment -Path $layout.RuntimeEnv
    Assert-JobAgentSafeRuntimeEnvironment -Values $values -Layout $layout | Out-Null
    $selection = Get-JobAgentIdentitySelection `
        -Layout $layout `
        -RepositoryPath $repository `
        -PythonExecutable $PythonExecutable `
        -RequirePrivateValidation
    if ($null -eq $selection) {
        throw 'RUNNER_IDENTITY_UNAVAILABLE'
    }
    if (-not $PSCmdlet.ShouldProcess(
        $script:ComposeProjectName,
        'Start exact Compose project and runner, verify runtime, and open dashboard once'
    )) {
        return [pscustomobject]@{ Started = $false; Opened = $false; Reason = 'WhatIf' }
    }

    $currentBuild = Get-JobAgentBuildSha `
        -RepositoryPath $repository `
        -RequireClean `
        -RequireMain
    Assert-JobAgentRuntimeRelease -Values $values -ExpectedBuildSha $currentBuild | Out-Null
    $mutex = Enter-JobAgentRuntimeMutex -RepositoryPath $repository
    try {
        $containers = Get-JobAgentComposeContainers `
            -RepositoryPath $repository `
            -RuntimeEnvPath $layout.RuntimeEnv
        $compose = Get-JobAgentComposeOwnership `
            -Containers $containers `
            -RepositoryPath $repository
        if ($compose.Classification -notin @('Absent', 'OwnedExact')) {
            throw "COMPOSE_PROJECT_NOT_OWNED_EXACT:$($compose.Classification)"
        }
        $port = [int]$values['API_PORT']
        $endpointCandidate = Get-JobAgentEndpointOwnership `
            -Listeners (Get-JobAgentListeners -Port $port) `
            -Containers $containers `
            -Port $port
        if ($endpointCandidate.Classification -ne 'Absent') {
            if (-not $endpointCandidate.MetadataMatched) {
                throw "API_ENDPOINT_NOT_OWNED_EXACT:$($endpointCandidate.Classification)"
            }
            $verifiedContainers = Get-JobAgentComposeContainers `
                -RepositoryPath $repository `
                -RuntimeEnvPath $layout.RuntimeEnv
            $verifiedCompose = Get-JobAgentComposeOwnership `
                -Containers $verifiedContainers `
                -RepositoryPath $repository
            if ($verifiedCompose.Classification -ne 'OwnedExact') {
                throw "COMPOSE_PROJECT_NOT_OWNED_EXACT:$($verifiedCompose.Classification)"
            }
            $verifiedListeners = Get-JobAgentListeners -Port $port
            $verifiedCandidate = Get-JobAgentEndpointOwnership `
                -Listeners $verifiedListeners `
                -Containers $verifiedContainers `
                -Port $port
            if (-not $verifiedCandidate.MetadataMatched) {
                throw "API_ENDPOINT_NOT_OWNED_EXACT:$($verifiedCandidate.Classification)"
            }
            Test-JobAgentStableRuntime `
                -DashboardUrl "http://127.0.0.1:$port/" `
                -OperatorToken ([string]$values['SECRET_KEY']) `
                -ExpectedBuildSha $currentBuild | Out-Null
            $endpoint = Get-JobAgentEndpointOwnership `
                -Listeners $verifiedListeners `
                -Containers $verifiedContainers `
                -Port $port `
                -AuthenticatedRuntimeVerified
            if ($endpoint.Classification -ne 'OwnedExact') {
                throw "API_ENDPOINT_NOT_OWNED_EXACT:$($endpoint.Classification)"
            }
        }
        Invoke-JobAgentCompose `
            -RepositoryPath $repository `
            -RuntimeEnvPath $layout.RuntimeEnv `
            -Arguments (@('up', '--detach', '--build') + $script:CoreServices) | Out-Null
        $postStartBuild = Get-JobAgentBuildSha `
            -RepositoryPath $repository `
            -RequireClean `
            -RequireMain
        if ($postStartBuild -ne $currentBuild) {
            throw 'REPOSITORY_CHANGED_DURING_START'
        }
        Start-JobAgentOwnedRunnerTask `
            -RepositoryPath $repository `
            -PythonExecutable $PythonExecutable `
            -ConfigPath $selection.RunnerConfigPath `
            -TaskName $TaskName `
            -Confirm:$false | Out-Null
        $opened = Open-JobAgentDashboard `
            -DashboardUrl "http://127.0.0.1:$port/" `
            -OperatorToken ([string]$values['SECRET_KEY']) `
            -ExpectedBuildSha ([string]$values['APP_BUILD_SHA']) `
            -TimeoutSeconds $TimeoutSeconds `
            -Confirm:$false
        return [pscustomobject]@{
            Started = $true
            Opened = $opened.Opened
            BuildSha = $opened.Snapshot.BuildSha
            BootId = $opened.Snapshot.BootId
        }
    }
    finally {
        Exit-JobAgentRuntimeMutex -Handle $mutex
    }
}

function Invoke-JobAgentOpen {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [string]$PythonExecutable = (Get-Command python -ErrorAction Stop).Source,

        [string]$LocalAppDataRoot = $env:LOCALAPPDATA,

        [string]$TaskName = $script:RunnerTaskName,

        [ValidateRange(10, 900)]
        [int]$TimeoutSeconds = 60
    )

    $repository = ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath -RequireExisting
    $layout = Get-JobAgentLayout -LocalAppDataRoot $LocalAppDataRoot
    Assert-JobAgentExternalLayout `
        -Layout $layout `
        -LocalAppDataRoot $LocalAppDataRoot `
        -RepositoryPath $repository | Out-Null
    $values = Read-JobAgentRuntimeEnvironment -Path $layout.RuntimeEnv
    Assert-JobAgentSafeRuntimeEnvironment -Values $values -Layout $layout | Out-Null
    if ($WhatIfPreference) {
        return Open-JobAgentDashboard `
            -DashboardUrl "http://127.0.0.1:$($values['API_PORT'])/" `
            -OperatorToken ([string]$values['SECRET_KEY']) `
            -ExpectedBuildSha ([string]$values['APP_BUILD_SHA']) `
            -TimeoutSeconds $TimeoutSeconds `
            -WhatIf
    }
    $currentBuild = Get-JobAgentBuildSha `
        -RepositoryPath $repository `
        -RequireClean `
        -RequireMain
    Assert-JobAgentRuntimeRelease -Values $values -ExpectedBuildSha $currentBuild | Out-Null
    $selection = Get-JobAgentIdentitySelection `
        -Layout $layout `
        -RepositoryPath $repository `
        -PythonExecutable $PythonExecutable `
        -RequirePrivateValidation
    if ($null -eq $selection) {
        throw 'RUNNER_IDENTITY_UNAVAILABLE'
    }
    $mutex = Enter-JobAgentRuntimeMutex -RepositoryPath $repository
    try {
        $taskState = Get-JobAgentTaskState `
            -RepositoryPath $repository `
            -PythonExecutable $PythonExecutable `
            -ConfigPath $selection.RunnerConfigPath `
            -TaskName $TaskName
        if (
            $taskState.Ownership.Classification -ne 'OwnedExact' -or
            [string]$taskState.Task.State -ne 'Running'
        ) {
            throw "RUNNER_TASK_NOT_RUNNING_OWNED_EXACT:$($taskState.Ownership.Classification)"
        }
        $containers = Get-JobAgentComposeContainers `
            -RepositoryPath $repository `
            -RuntimeEnvPath $layout.RuntimeEnv
        $compose = Get-JobAgentComposeOwnership `
            -Containers $containers `
            -RepositoryPath $repository
        if ($compose.Classification -ne 'OwnedExact') {
            throw "COMPOSE_PROJECT_NOT_OWNED_EXACT:$($compose.Classification)"
        }
        $port = [int]$values['API_PORT']
        $listeners = Get-JobAgentListeners -Port $port
        $endpointCandidate = Get-JobAgentEndpointOwnership `
            -Listeners $listeners `
            -Containers $containers `
            -Port $port
        if (-not $endpointCandidate.MetadataMatched) {
            throw "API_ENDPOINT_NOT_OWNED_EXACT:$($endpointCandidate.Classification)"
        }
        $verifiedContainers = Get-JobAgentComposeContainers `
            -RepositoryPath $repository `
            -RuntimeEnvPath $layout.RuntimeEnv
        $verifiedCompose = Get-JobAgentComposeOwnership `
            -Containers $verifiedContainers `
            -RepositoryPath $repository
        if ($verifiedCompose.Classification -ne 'OwnedExact') {
            throw "COMPOSE_PROJECT_NOT_OWNED_EXACT:$($verifiedCompose.Classification)"
        }
        $verifiedListeners = Get-JobAgentListeners -Port $port
        $verifiedCandidate = Get-JobAgentEndpointOwnership `
            -Listeners $verifiedListeners `
            -Containers $verifiedContainers `
            -Port $port
        if (-not $verifiedCandidate.MetadataMatched) {
            throw "API_ENDPOINT_NOT_OWNED_EXACT:$($verifiedCandidate.Classification)"
        }
        Test-JobAgentStableRuntime `
            -DashboardUrl "http://127.0.0.1:$port/" `
            -OperatorToken ([string]$values['SECRET_KEY']) `
            -ExpectedBuildSha $currentBuild | Out-Null
        $endpoint = Get-JobAgentEndpointOwnership `
            -Listeners $verifiedListeners `
            -Containers $verifiedContainers `
            -Port $port `
            -AuthenticatedRuntimeVerified
        if ($endpoint.Classification -ne 'OwnedExact') {
            throw "API_ENDPOINT_NOT_OWNED_EXACT:$($endpoint.Classification)"
        }
        return Open-JobAgentDashboard `
            -DashboardUrl "http://127.0.0.1:$port/" `
            -OperatorToken ([string]$values['SECRET_KEY']) `
            -ExpectedBuildSha ([string]$values['APP_BUILD_SHA']) `
            -TimeoutSeconds $TimeoutSeconds `
            -Confirm:$false
    }
    finally {
        Exit-JobAgentRuntimeMutex -Handle $mutex
    }
}

function Invoke-JobAgentStop {
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepositoryPath,

        [string]$PythonExecutable = (Get-Command python -ErrorAction Stop).Source,

        [string]$LocalAppDataRoot = $env:LOCALAPPDATA,

        [string]$TaskName = $script:RunnerTaskName
    )

    Assert-JobAgentEmergencyTaskTarget -TaskName $TaskName | Out-Null
    $repository = ConvertTo-JobAgentCanonicalPath -LiteralPath $RepositoryPath -RequireExisting
    $layout = Get-JobAgentLayout -LocalAppDataRoot $LocalAppDataRoot
    Assert-JobAgentExternalLayout `
        -Layout $layout `
        -LocalAppDataRoot $LocalAppDataRoot `
        -RepositoryPath $repository | Out-Null
    $values = Read-JobAgentRuntimeEnvironment -Path $layout.RuntimeEnv
    Assert-JobAgentSafeRuntimeEnvironment -Values $values -Layout $layout | Out-Null
    if (-not $PSCmdlet.ShouldProcess(
        $script:ComposeProjectName,
        'Stop only the marker-owned runner task and exact owned Compose project'
    )) {
        return [pscustomobject]@{ Stopped = $false; Reason = 'WhatIf' }
    }

    $mutex = Enter-JobAgentRuntimeMutex -RepositoryPath $repository
    try {
        $preflightTask = Get-JobAgentEmergencyTaskState -TaskName $TaskName
        if ($preflightTask.Ownership.Classification -notin @('Absent', 'MarkerOwned')) {
            throw "RUNNER_TASK_NOT_OWNED_EXACT:$($preflightTask.Ownership.Classification)"
        }
        $preflightContainers = Get-JobAgentComposeContainers `
            -RepositoryPath $repository `
            -RuntimeEnvPath $layout.RuntimeEnv
        $preflightCompose = Get-JobAgentComposeOwnership `
            -Containers $preflightContainers `
            -RepositoryPath $repository
        if ($preflightCompose.Classification -notin @('Absent', 'OwnedExact')) {
            throw "COMPOSE_PROJECT_NOT_OWNED_EXACT:$($preflightCompose.Classification)"
        }

        # Re-probe both resources after the preflight and validate the pair
        # before mutating either one. An owned resource may have appeared while
        # the stop command waited for the mutex; a foreign replacement must
        # still fail closed.
        $actionTask = Get-JobAgentEmergencyTaskState -TaskName $TaskName
        $actionContainers = Get-JobAgentComposeContainers `
            -RepositoryPath $repository `
            -RuntimeEnvPath $layout.RuntimeEnv
        $actionCompose = Get-JobAgentComposeOwnership `
            -Containers $actionContainers `
            -RepositoryPath $repository
        if ($actionTask.Ownership.Classification -notin @('Absent', 'MarkerOwned')) {
            throw "RUNNER_TASK_NOT_OWNED_EXACT:$($actionTask.Ownership.Classification)"
        }
        if ($actionCompose.Classification -notin @('Absent', 'OwnedExact')) {
            throw "COMPOSE_PROJECT_NOT_OWNED_EXACT:$($actionCompose.Classification)"
        }

        # These action paths deliberately re-probe once more immediately before
        # deciding whether to mutate. Never reuse an earlier Absent snapshot.
        Stop-JobAgentEmergencyRunnerTask `
            -TaskName $TaskName `
            -Confirm:$false | Out-Null
        $composeContainers = Get-JobAgentComposeContainers `
            -RepositoryPath $repository `
            -RuntimeEnvPath $layout.RuntimeEnv
        $compose = Get-JobAgentComposeOwnership `
            -Containers $composeContainers `
            -RepositoryPath $repository
        if ($compose.Classification -notin @('Absent', 'OwnedExact')) {
            throw "COMPOSE_PROJECT_NOT_OWNED_EXACT:$($compose.Classification)"
        }
        if ($compose.Classification -eq 'OwnedExact') {
            Invoke-JobAgentCompose `
                -RepositoryPath $repository `
                -RuntimeEnvPath $layout.RuntimeEnv `
                -Arguments @('down', '--remove-orphans') | Out-Null
        }

        # A successful command invocation is not evidence that either resource
        # stopped. Re-observe both resources and fail closed unless the task is
        # absent/non-running and Compose down removed every exact container.
        $finalTask = Get-JobAgentEmergencyTaskState -TaskName $TaskName
        if ($finalTask.Ownership.Classification -notin @('Absent', 'MarkerOwned')) {
            throw "RUNNER_TASK_NOT_OWNED_EXACT:$($finalTask.Ownership.Classification)"
        }
        if ($finalTask.Ownership.Classification -eq 'MarkerOwned') {
            $finalTaskState = [string]$finalTask.Task.State
            if ($finalTaskState -notin @('Ready', 'Disabled')) {
                throw "RUNNER_TASK_STOP_UNCONFIRMED:$finalTaskState"
            }
        }
        $finalContainers = Get-JobAgentComposeContainers `
            -RepositoryPath $repository `
            -RuntimeEnvPath $layout.RuntimeEnv
        $finalCompose = Get-JobAgentComposeOwnership `
            -Containers $finalContainers `
            -RepositoryPath $repository
        if ($finalCompose.Classification -notin @('Absent', 'OwnedExact')) {
            throw "COMPOSE_PROJECT_NOT_OWNED_EXACT:$($finalCompose.Classification)"
        }
        if ($finalCompose.Classification -ne 'Absent') {
            throw "COMPOSE_PROJECT_STOP_UNCONFIRMED:$($finalCompose.ContainerCount)"
        }
        return [pscustomobject]@{ Stopped = $true; DataVolumesPreserved = $true }
    }
    finally {
        Exit-JobAgentRuntimeMutex -Handle $mutex
    }
}

Export-ModuleMember -Function @(
    'Assert-JobAgentExternalLayout',
    'Assert-JobAgentGitPorcelainClean',
    'Assert-JobAgentLoopbackUrl',
    'Assert-JobAgentMainRelease',
    'Assert-JobAgentRuntimeRelease',
    'Assert-JobAgentSafeRuntimeEnvironment',
    'ConvertFrom-JobAgentComposeLabels',
    'ConvertFrom-JobAgentEnvironmentText',
    'ConvertTo-JobAgentCanonicalPath',
    'Enter-JobAgentRuntimeMutex',
    'Exit-JobAgentRuntimeMutex',
    'Get-JobAgentBuildSha',
    'Get-JobAgentComposeArguments',
    'Get-JobAgentComposeContainers',
    'Get-JobAgentComposeOwnership',
    'Get-JobAgentEndpointOwnership',
    'Get-JobAgentExpectedTaskAction',
    'Get-JobAgentIdentitySelection',
    'Get-JobAgentLayout',
    'Get-JobAgentListeners',
    'Get-JobAgentLocalStatus',
    'Get-JobAgentMutexName',
    'Get-JobAgentRuntimeConstants',
    'Get-JobAgentTaskOwnership',
    'Get-JobAgentTaskState',
    'Initialize-JobAgentExternalLayout',
    'Invoke-JobAgentBootstrap',
    'Invoke-JobAgentCompose',
    'Invoke-JobAgentOpen',
    'Invoke-JobAgentStart',
    'Invoke-JobAgentStop',
    'New-JobAgentRuntimeEnvironmentText',
    'Open-JobAgentDashboard',
    'Read-JobAgentRuntimeEnvironment',
    'Start-JobAgentOwnedRunnerTask',
    'Stop-JobAgentOwnedRunnerTask',
    'Test-JobAgentPathWithin',
    'Test-JobAgentStableRuntime',
    'Update-JobAgentRuntimeRelease',
    'Wait-JobAgentStableRuntime'
)
