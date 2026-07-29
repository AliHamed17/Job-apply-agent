[CmdletBinding()]
param()

#Requires -Version 7.2

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repository = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$modulePath = Join-Path $repository 'scripts\JobAgent.Runtime.psm1'
Import-Module $modulePath -Force -ErrorAction Stop

function Assert-True {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not $Condition) {
        throw "ASSERT_TRUE_FAILED:$Message"
    }
}

function Assert-Equal {
    param(
        [AllowNull()]
        [object]$Actual,

        [AllowNull()]
        [object]$Expected,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not [object]::Equals($Actual, $Expected)) {
        throw "ASSERT_EQUAL_FAILED:$Message"
    }
}

function Assert-Throws {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Action,

        [Parameter(Mandatory = $true)]
        [string]$Pattern
    )

    try {
        & $Action
    }
    catch {
        if ($_.Exception.Message -match $Pattern) {
            return
        }
        throw
    }
    throw "ASSERT_THROWS_FAILED:$Pattern"
}

$build = ('a' * 40) -join ''
$fakeLocalAppData = Join-Path ([System.IO.Path]::GetTempPath()) (
    'job-agent-whatif-' + [guid]::NewGuid()
)
$layout = Get-JobAgentLayout -LocalAppDataRoot $fakeLocalAppData
Assert-True `
    -Condition (Test-JobAgentPathWithin -ChildPath $layout.RuntimeEnv -ParentPath $layout.Root) `
    -Message 'runtime env remains below external root'
$environmentText = New-JobAgentRuntimeEnvironmentText -Layout $layout -BuildSha $build
$environment = ConvertFrom-JobAgentEnvironmentText -Text $environmentText
Assert-True `
    -Condition (Assert-JobAgentSafeRuntimeEnvironment -Values $environment -Layout $layout) `
    -Message 'generated runtime environment is fail closed'
Assert-Equal $environment['DRY_RUN'] 'true' 'dry run is mandatory'
Assert-Equal $environment['DRAFT_ONLY'] 'true' 'draft only is mandatory'
Assert-Equal $environment['AUTO_APPLY'] 'false' 'unattended automation is disabled'
Assert-Equal `
    $environment['PORTAL_FINAL_SUBMIT_ENABLED'] `
    'false' `
    'final submit is disabled'
Assert-True `
    -Condition (-not (Test-Path -LiteralPath $fakeLocalAppData)) `
    -Message 'pure layout/environment generation writes nothing'

$unsafe = @{} + $environment
$unsafe['DRY_RUN'] = 'false'
Assert-Throws `
    -Action {
        Assert-JobAgentSafeRuntimeEnvironment -Values $unsafe -Layout $layout | Out-Null
    } `
    -Pattern 'RUNTIME_ENV_UNSAFE_DRY_RUN'
Assert-True `
    -Condition (Assert-JobAgentGitPorcelainClean -Lines @()) `
    -Message 'empty porcelain output is clean'
Assert-Throws `
    -Action { Assert-JobAgentGitPorcelainClean -Lines @(' M core/config.py') | Out-Null } `
    -Pattern 'REPOSITORY_NOT_CLEAN'
Assert-True `
    -Condition (Assert-JobAgentMainRelease -HeadBuildSha $build -MainBuildSha $build) `
    -Message 'exact main build is accepted'
Assert-Throws `
    -Action {
        Assert-JobAgentMainRelease `
            -HeadBuildSha $build `
            -MainBuildSha (('e' * 40) -join '') | Out-Null
    } `
    -Pattern 'RELEASE_NOT_MAIN_DERIVED'
Assert-True `
    -Condition (
        Assert-JobAgentRuntimeRelease -Values $environment -ExpectedBuildSha $build
    ) `
    -Message 'runtime release matches current build'
$staleEnvironment = @{} + $environment
$staleEnvironment['APP_BUILD_SHA'] = ('f' * 40) -join ''
Assert-Throws `
    -Action {
        Assert-JobAgentRuntimeRelease `
            -Values $staleEnvironment `
            -ExpectedBuildSha $build | Out-Null
    } `
    -Pattern 'RUNTIME_RELEASE_STALE'

Assert-Throws `
    -Action {
        $repositoryLayout = Get-JobAgentLayout -LocalAppDataRoot $repository
        Assert-JobAgentExternalLayout `
            -Layout $repositoryLayout `
            -LocalAppDataRoot $repository `
            -RepositoryPath $repository | Out-Null
    } `
    -Pattern 'EXTERNAL_ROOT_(REPOSITORY_RELATED|REPARSE_POINT)'
$syntheticOneDrive = Join-Path ([System.IO.Path]::GetTempPath()) (
    'OneDrive - Synthetic-' + [guid]::NewGuid()
)
Assert-Throws `
    -Action {
        $oneDriveLayout = Get-JobAgentLayout -LocalAppDataRoot $syntheticOneDrive
        Assert-JobAgentExternalLayout `
            -Layout $oneDriveLayout `
            -LocalAppDataRoot $syntheticOneDrive `
            -RepositoryPath $repository | Out-Null
    } `
    -Pattern 'EXTERNAL_ROOT_(IN_ONEDRIVE|REPARSE_POINT)'

$reparseFixture = Join-Path ([System.IO.Path]::GetTempPath()) (
    'job-agent-reparse-' + [guid]::NewGuid()
)
$reparseTarget = Join-Path $reparseFixture 'target'
$reparseLink = Join-Path $reparseFixture 'link'
[System.IO.Directory]::CreateDirectory($reparseTarget) | Out-Null
try {
    New-Item -ItemType Junction -Path $reparseLink -Target $reparseTarget | Out-Null
    Assert-Throws `
        -Action {
            $reparseLayout = Get-JobAgentLayout -LocalAppDataRoot $reparseLink
            Assert-JobAgentExternalLayout `
                -Layout $reparseLayout `
                -LocalAppDataRoot $reparseLink `
                -RepositoryPath $repository | Out-Null
        } `
        -Pattern 'EXTERNAL_ROOT_REPARSE_POINT'
}
finally {
    if (Test-Path -LiteralPath $reparseLink) {
        Remove-Item -LiteralPath $reparseLink -Force
    }
    if (Test-Path -LiteralPath $reparseFixture) {
        Remove-Item -LiteralPath $reparseFixture -Recurse -Force
    }
}

$releaseTestRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    'job-agent-release-' + [guid]::NewGuid()
)
$releaseLayout = Get-JobAgentLayout -LocalAppDataRoot $releaseTestRoot
[System.IO.Directory]::CreateDirectory($releaseLayout.Runtime) | Out-Null
[System.IO.File]::WriteAllText(
    $releaseLayout.RuntimeEnv,
    (New-JobAgentRuntimeEnvironmentText -Layout $releaseLayout -BuildSha $build),
    [System.Text.UTF8Encoding]::new($false)
)
try {
    $beforeUpgrade = Read-JobAgentRuntimeEnvironment -Path $releaseLayout.RuntimeEnv
    $nextBuild = ('b' * 40) -join ''
    $upgrade = Update-JobAgentRuntimeRelease `
        -Path $releaseLayout.RuntimeEnv `
        -Layout $releaseLayout `
        -BuildSha $nextBuild `
        -Confirm:$false
    $afterUpgrade = Read-JobAgentRuntimeEnvironment -Path $releaseLayout.RuntimeEnv
    Assert-True -Condition $upgrade.Applied -Message 'release upgrade is applied atomically'
    Assert-Equal $afterUpgrade['APP_BUILD_SHA'] $nextBuild 'release binding is updated'
    foreach ($secretName in @(
        'SECRET_KEY',
        'WHATSAPP_APP_SECRET',
        'POSTGRES_PASSWORD',
        'GRAFANA_PASSWORD'
    )) {
        Assert-Equal `
            $afterUpgrade[$secretName] `
            $beforeUpgrade[$secretName] `
            "release upgrade preserves $secretName"
    }

    $identityVersion = [guid]::NewGuid().ToString()
    $identityBundle = Join-Path (
        Join-Path $releaseLayout.Identity 'versions'
    ) $identityVersion
    [System.IO.Directory]::CreateDirectory($identityBundle) | Out-Null
    $deviceId = [guid]::NewGuid().ToString()
    $controlSigningKeyId = [guid]::NewGuid().ToString()
    $runnerPublicValue = ('r' * 43) -join ''
    $controlPublicValue = ('p' * 43) -join ''
    $runnerPrivate = Join-Path $identityBundle 'runner-private.key'
    $runnerPublic = Join-Path $identityBundle 'runner-public.key'
    $controlPublic = Join-Path $identityBundle 'control-public.key'
    $secretBundle = Join-Path $identityBundle 'control-secrets.dpapi'
    [System.IO.File]::WriteAllText($runnerPrivate, ('k' * 43))
    [System.IO.File]::WriteAllText($runnerPublic, $runnerPublicValue)
    [System.IO.File]::WriteAllText($controlPublic, $controlPublicValue)
    [System.IO.File]::WriteAllBytes($secretBundle, [byte[]](1, 2, 3, 4))
    $runnerConfigPath = Join-Path $identityBundle 'runner.json'
    $runnerConfig = @{
        control_plane_url = 'https://control.example'
        device_id = $deviceId
        control_signing_key_id = $controlSigningKeyId
        control_plane_audience = 'job-apply-control-plane'
        runtime_env_path = $releaseLayout.RuntimeEnv
        private_key_path = $runnerPrivate
        control_plane_public_key_path = $controlPublic
        poll_interval_seconds = 10
        heartbeat_interval_seconds = 10
        offline_after_seconds = 30
    }
    [System.IO.File]::WriteAllText(
        $runnerConfigPath,
        ($runnerConfig | ConvertTo-Json -Depth 5)
    )
    $manifestPath = Join-Path $identityBundle 'manifest.json'
    $manifest = @{
        schema_version = 2
        version_id = $identityVersion
        created_at = '2026-07-29T10:00:00Z'
        device_id = $deviceId
        device_public_key = $runnerPublicValue
        control_signing_key_id = $controlSigningKeyId
        control_public_key = $controlPublicValue
        control_audience = 'job-apply-control-plane'
        runner_audience = 'job-apply-private-runner'
        control_plane_url = 'https://control.example'
        vercel_environment = 'production'
        vercel_project_id = 'prj_12345678abcdef'
        vercel_scope_id = 'team_12345678abcdef'
        runner_config_path = $runnerConfigPath
        secret_bundle_path = $secretBundle
    }
    [System.IO.File]::WriteAllText(
        $manifestPath,
        ($manifest | ConvertTo-Json -Depth 5)
    )
    [System.IO.File]::WriteAllText(
        $releaseLayout.IdentityCurrent,
        (@{
            schema_version = 2
            version_id = $identityVersion
            bundle_path = $identityBundle
        } | ConvertTo-Json -Depth 5)
    )
    # Structural-only coverage is explicit here. Every production command path
    # requests the same-user Python private-binding validation below.
    $identitySelection = Get-JobAgentIdentitySelection `
        -Layout $releaseLayout `
        -ExpectedControlPlaneUrl 'https://control.example/' `
        -ExpectedVercelEnvironment 'production' `
        -ExpectedVercelProjectId 'prj_12345678abcdef' `
        -ExpectedVercelScopeId 'team_12345678abcdef'
    Assert-Equal `
        $identitySelection.RunnerConfigPath `
        $runnerConfigPath `
        'identity selection is bound to expected URL and runtime'
    Assert-Equal `
        $identitySelection.VercelEnvironment `
        'production' `
        'identity selection exposes the validated Vercel environment'
    Assert-Equal `
        $identitySelection.VercelProjectId `
        'prj_12345678abcdef' `
        'identity selection exposes the validated Vercel project'
    Assert-Equal `
        $identitySelection.VercelScopeId `
        'team_12345678abcdef' `
        'identity selection exposes the validated Vercel scope'
    Assert-True `
        -Condition (-not $identitySelection.PrivateBindingValidated) `
        -Message 'structural-only selection is not represented as privately validated'
    $privateValidationState = [pscustomobject]@{ Calls = 0 }
    $assertEqualCommand = ${function:Assert-Equal}
    $privateValidator = {
        param(
            [string]$PythonExecutable,
            [string]$IdentityScriptPath,
            [string]$IdentityRoot,
            [string]$RepositoryPath
        )

        & $assertEqualCommand `
            $PythonExecutable `
            'python-private-validator-test.exe' `
            'private validator receives the exact Python executable'
        & $assertEqualCommand `
            $IdentityScriptPath `
            (Join-Path $repository 'scripts\control_plane_identity.py') `
            'private validator receives the trusted repository script'
        & $assertEqualCommand `
            $IdentityRoot `
            $releaseLayout.Identity `
            'private validator receives the external identity root'
        & $assertEqualCommand `
            $RepositoryPath `
            $repository `
            'private validator receives the canonical repository root'
        $privateValidationState.Calls++
        return @{
            control_plane_url = 'https://control.example'
            device_id = $deviceId
            vercel_environment = 'production'
            vercel_project_id = 'prj_12345678abcdef'
            vercel_scope_id = 'team_12345678abcdef'
            version_id = $identityVersion
        }
    }.GetNewClosure()
    $privateIdentitySelection = Get-JobAgentIdentitySelection `
        -Layout $releaseLayout `
        -ExpectedControlPlaneUrl 'https://control.example/' `
        -ExpectedVercelEnvironment 'production' `
        -ExpectedVercelProjectId 'prj_12345678abcdef' `
        -ExpectedVercelScopeId 'team_12345678abcdef' `
        -RepositoryPath $repository `
        -PythonExecutable 'python-private-validator-test.exe' `
        -RequirePrivateValidation `
        -PrivateValidatorInvoker $privateValidator
    Assert-Equal `
        $privateValidationState.Calls `
        1 `
        'private validator is invoked exactly once'
    Assert-True `
        -Condition $privateIdentitySelection.PrivateBindingValidated `
        -Message 'matching private validator evidence is represented explicitly'
    $mismatchedPrivateValidator = {
        param(
            [string]$PythonExecutable,
            [string]$IdentityScriptPath,
            [string]$IdentityRoot,
            [string]$RepositoryPath
        )

        return @{
            control_plane_url = 'https://control.example'
            device_id = $deviceId
            vercel_environment = 'production'
            vercel_project_id = 'prj_wrongtarget123456'
            vercel_scope_id = 'team_12345678abcdef'
            version_id = $identityVersion
        }
    }.GetNewClosure()
    Assert-Throws `
        -Action {
            Get-JobAgentIdentitySelection `
                -Layout $releaseLayout `
                -RepositoryPath $repository `
                -PythonExecutable 'python-private-validator-test.exe' `
                -RequirePrivateValidation `
                -PrivateValidatorInvoker $mismatchedPrivateValidator | Out-Null
        } `
        -Pattern 'IDENTITY_PRIVATE_VALIDATION_MISMATCH'
    $failedPrivateValidator = {
        param(
            [string]$PythonExecutable,
            [string]$IdentityScriptPath,
            [string]$IdentityRoot,
            [string]$RepositoryPath
        )

        throw 'SIMULATED_PRIVATE_KEY_VALIDATION_FAILURE'
    }
    Assert-Throws `
        -Action {
            Get-JobAgentIdentitySelection `
                -Layout $releaseLayout `
                -RepositoryPath $repository `
                -PythonExecutable 'python-private-validator-test.exe' `
                -RequirePrivateValidation `
                -PrivateValidatorInvoker $failedPrivateValidator | Out-Null
        } `
        -Pattern 'IDENTITY_PRIVATE_VALIDATION_FAILED'
    $moduleSource = [System.IO.File]::ReadAllText($modulePath)
    Assert-True `
        -Condition $moduleSource.Contains(
            '& $python -I -B $identityScript validate-selection',
            [System.StringComparison]::Ordinal
        ) `
        -Message 'private validator uses the isolated Python validate-selection command'
    Assert-True `
        -Condition (
            $moduleSource.Contains(
                '--root $IdentityRoot',
                [System.StringComparison]::Ordinal
            ) -and
            $moduleSource.Contains(
                '--repository-root $repository',
                [System.StringComparison]::Ordinal
            )
        ) `
        -Message 'private validator binds the external root and canonical repository'
    foreach ($productionFunction in @(
        'Invoke-JobAgentBootstrap',
        'Get-JobAgentLocalStatus',
        'Invoke-JobAgentStart',
        'Invoke-JobAgentOpen'
    )) {
        $functionMarker = "function $productionFunction"
        $functionStart = $moduleSource.IndexOf(
            $functionMarker,
            [System.StringComparison]::Ordinal
        )
        Assert-True `
            -Condition ($functionStart -ge 0) `
            -Message "$productionFunction exists for private-binding coverage"
        $nextFunction = $moduleSource.IndexOf(
            "`nfunction ",
            $functionStart + $functionMarker.Length,
            [System.StringComparison]::Ordinal
        )
        $functionLength = if ($nextFunction -lt 0) {
            $moduleSource.Length - $functionStart
        }
        else {
            $nextFunction - $functionStart
        }
        $functionBody = $moduleSource.Substring($functionStart, $functionLength)
        Assert-True `
            -Condition $functionBody.Contains(
                '-RequirePrivateValidation',
                [System.StringComparison]::Ordinal
            ) `
            -Message "$productionFunction requires same-user private validation"
        if ($productionFunction -eq 'Invoke-JobAgentBootstrap') {
            Assert-Equal `
                ([regex]::Matches(
                    $functionBody,
                    [regex]::Escape('-RequirePrivateValidation')
                ).Count) `
                2 `
                'bootstrap validates both reused and newly provisioned identities'
        }
    }
    $stopMarker = 'function Invoke-JobAgentStop'
    $stopStart = $moduleSource.IndexOf(
        $stopMarker,
        [System.StringComparison]::Ordinal
    )
    Assert-True `
        -Condition ($stopStart -ge 0) `
        -Message 'Invoke-JobAgentStop exists for recoverable-shutdown coverage'
    $stopBody = $moduleSource.Substring($stopStart)
    Assert-True `
        -Condition (-not $stopBody.Contains(
            '-RequirePrivateValidation',
            [System.StringComparison]::Ordinal
        )) `
        -Message 'stop remains available when DPAPI or private identity is corrupt'
    foreach ($ownershipGuard in @(
        'RUNNER_TASK_NOT_OWNED_EXACT',
        'COMPOSE_PROJECT_NOT_OWNED_EXACT'
    )) {
        Assert-True `
            -Condition $stopBody.Contains(
                $ownershipGuard,
                [System.StringComparison]::Ordinal
            ) `
            -Message "stop retains the $ownershipGuard refusal"
    }
    Assert-Throws `
        -Action {
            Get-JobAgentIdentitySelection `
                -Layout $releaseLayout `
                -ExpectedControlPlaneUrl 'https://other.example' | Out-Null
        } `
        -Pattern 'IDENTITY_CONTROL_PLANE_URL_MISMATCH'
    Assert-Throws `
        -Action {
            Get-JobAgentIdentitySelection `
                -Layout $releaseLayout `
                -ExpectedVercelEnvironment 'preview' `
                -ExpectedVercelProjectId 'prj_12345678abcdef' `
                -ExpectedVercelScopeId 'team_12345678abcdef' | Out-Null
        } `
        -Pattern 'IDENTITY_VERCEL_ENVIRONMENT_MISMATCH'
    Assert-Throws `
        -Action {
            Get-JobAgentIdentitySelection `
                -Layout $releaseLayout `
                -ExpectedVercelEnvironment 'production' `
                -ExpectedVercelProjectId 'prj_abcdefgh12345678' `
                -ExpectedVercelScopeId 'team_12345678abcdef' | Out-Null
        } `
        -Pattern 'IDENTITY_VERCEL_PROJECT_MISMATCH'
    Assert-Throws `
        -Action {
            Get-JobAgentIdentitySelection `
                -Layout $releaseLayout `
                -ExpectedVercelEnvironment 'production' `
                -ExpectedVercelProjectId 'prj_12345678abcdef' `
                -ExpectedVercelScopeId 'team_abcdefgh12345678' | Out-Null
        } `
        -Pattern 'IDENTITY_VERCEL_SCOPE_MISMATCH'
    Assert-Throws `
        -Action {
            Get-JobAgentIdentitySelection `
                -Layout $releaseLayout `
                -ExpectedVercelEnvironment 'production' | Out-Null
        } `
        -Pattern 'IDENTITY_VERCEL_TARGET_EXPECTATION_INCOMPLETE'
    $incompleteManifest = @{} + $manifest
    $incompleteManifest.Remove('vercel_scope_id') | Out-Null
    [System.IO.File]::WriteAllText(
        $manifestPath,
        ($incompleteManifest | ConvertTo-Json -Depth 5)
    )
    Assert-Throws `
        -Action {
            Get-JobAgentIdentitySelection -Layout $releaseLayout | Out-Null
        } `
        -Pattern 'IDENTITY_MANIFEST_INVALID'
    [System.IO.File]::WriteAllText(
        $manifestPath,
        ($manifest | ConvertTo-Json -Depth 5)
    )
    $mismatchedRunner = @{} + $runnerConfig
    $mismatchedRunner['runtime_env_path'] = Join-Path $releaseTestRoot 'other.env'
    [System.IO.File]::WriteAllText(
        $runnerConfigPath,
        ($mismatchedRunner | ConvertTo-Json -Depth 5)
    )
    Assert-Throws `
        -Action {
            Get-JobAgentIdentitySelection -Layout $releaseLayout | Out-Null
        } `
        -Pattern 'IDENTITY_RUNTIME_ENV_MISMATCH'
    [System.IO.File]::WriteAllText(
        $runnerConfigPath,
        ($runnerConfig | ConvertTo-Json -Depth 5)
    )

    $originalDryRun = [Environment]::GetEnvironmentVariable('DRY_RUN', 'Process')
    $originalDatabase = [Environment]::GetEnvironmentVariable('DATABASE_URL', 'Process')
    $originalComposeFile = [Environment]::GetEnvironmentVariable('COMPOSE_FILE', 'Process')
    $originalDockerHost = [Environment]::GetEnvironmentVariable('DOCKER_HOST', 'Process')
    $originalDockerContext = [Environment]::GetEnvironmentVariable('DOCKER_CONTEXT', 'Process')
    $originalDockerTls = [Environment]::GetEnvironmentVariable('DOCKER_TLS_VERIFY', 'Process')
    $originalDockerCertPath = [Environment]::GetEnvironmentVariable(
        'DOCKER_CERT_PATH',
        'Process'
    )
    $runtimeEnvironmentBeforeComposeProbe = [System.IO.File]::ReadAllText(
        $releaseLayout.RuntimeEnv
    )
    try {
        [System.IO.File]::AppendAllText(
            $releaseLayout.RuntimeEnv,
            (
                [Environment]::NewLine +
                'DOCKER_HOST=tcp://runtime-file.example:2376' +
                [Environment]::NewLine +
                'DOCKER_CONTEXT=runtime-file-remote' +
                [Environment]::NewLine +
                'DOCKER_TLS_VERIFY=1' +
                [Environment]::NewLine +
                'DOCKER_CERT_PATH=C:\runtime-file-certs' +
                [Environment]::NewLine
            )
        )
        $env:DRY_RUN = 'false'
        $env:DATABASE_URL = 'sqlite:///inherited.db'
        $env:COMPOSE_FILE = 'C:\foreign-compose.yml'
        $env:DOCKER_HOST = 'tcp://remote.example:2376'
        $env:DOCKER_CONTEXT = 'remote-production'
        $env:DOCKER_TLS_VERIFY = '1'
        $env:DOCKER_CERT_PATH = 'C:\remote-certs'
        $assertEqualCommand = ${function:Assert-Equal}
        $assertTrueCommand = ${function:Assert-True}
        $composeProbe = {
            param([string]$Executable, [string[]]$CommandArguments)

            & $assertEqualCommand `
                $Executable `
                'docker-test.exe' `
                'test docker executable is explicit'
            & $assertEqualCommand `
                $env:DRY_RUN `
                'true' `
                'runtime file overrides inherited dry-run'
            & $assertEqualCommand `
                $env:DATABASE_URL `
                $afterUpgrade['DATABASE_URL'] `
                'runtime file overrides inherited database'
            & $assertTrueCommand `
                -Condition ([string]::IsNullOrEmpty($env:COMPOSE_FILE)) `
                -Message 'compose control variable is cleared'
            & $assertTrueCommand `
                -Condition ($CommandArguments -contains '--env-file') `
                -Message 'compose receives the trusted env file'
            & $assertTrueCommand `
                -Condition (
                    $CommandArguments[0] -eq '--context' -and
                    $CommandArguments[1] -eq 'default' -and
                    $CommandArguments[2] -eq 'compose'
                ) `
                -Message 'docker is pinned to the local default context'
            foreach ($transportName in @(
                'DOCKER_HOST',
                'DOCKER_CONTEXT',
                'DOCKER_TLS_VERIFY',
                'DOCKER_CERT_PATH'
            )) {
                & $assertTrueCommand `
                    -Condition ([string]::IsNullOrEmpty(
                        [Environment]::GetEnvironmentVariable($transportName, 'Process')
                    )) `
                    -Message "$transportName is cleared inside the Docker probe"
            }
            return [pscustomobject]@{ ExitCode = 0; Output = @('ok') }
        }.GetNewClosure()
        $composeOutput = Invoke-JobAgentCompose `
            -RepositoryPath $repository `
            -RuntimeEnvPath $releaseLayout.RuntimeEnv `
            -Arguments @('config') `
            -DockerExecutable 'docker-test.exe' `
            -CommandInvoker $composeProbe
        Assert-Equal ([string]$composeOutput) 'ok' 'compose probe completed'
        Assert-Equal $env:DRY_RUN 'false' 'parent dry-run env is restored'
        Assert-Equal $env:DATABASE_URL 'sqlite:///inherited.db' 'parent database env is restored'
        Assert-Equal $env:COMPOSE_FILE 'C:\foreign-compose.yml' 'compose control env is restored'
        Assert-Equal `
            $env:DOCKER_HOST `
            'tcp://remote.example:2376' `
            'remote Docker host is restored only after the probe'
        Assert-Equal `
            $env:DOCKER_CONTEXT `
            'remote-production' `
            'remote Docker context is restored only after the probe'
    }
    finally {
        [System.IO.File]::WriteAllText(
            $releaseLayout.RuntimeEnv,
            $runtimeEnvironmentBeforeComposeProbe
        )
        [Environment]::SetEnvironmentVariable('DRY_RUN', $originalDryRun, 'Process')
        [Environment]::SetEnvironmentVariable('DATABASE_URL', $originalDatabase, 'Process')
        [Environment]::SetEnvironmentVariable('COMPOSE_FILE', $originalComposeFile, 'Process')
        [Environment]::SetEnvironmentVariable('DOCKER_HOST', $originalDockerHost, 'Process')
        [Environment]::SetEnvironmentVariable(
            'DOCKER_CONTEXT',
            $originalDockerContext,
            'Process'
        )
        [Environment]::SetEnvironmentVariable(
            'DOCKER_TLS_VERIFY',
            $originalDockerTls,
            'Process'
        )
        [Environment]::SetEnvironmentVariable(
            'DOCKER_CERT_PATH',
            $originalDockerCertPath,
            'Process'
        )
    }
}
finally {
    if (Test-Path -LiteralPath $releaseTestRoot) {
        Remove-Item -LiteralPath $releaseTestRoot -Recurse -Force
    }
}

$constants = Get-JobAgentRuntimeConstants
$expected = Get-JobAgentExpectedTaskAction `
    -RepositoryPath 'C:\repo' `
    -PythonExecutable 'C:\Python313\python.exe' `
    -ConfigPath 'C:\private\runner.json'
$principal = [pscustomobject]@{
    UserId = 'EXAMPLE\operator'
    RunLevel = 'Limited'
    LogonType = 'Interactive'
}
$trigger = [pscustomobject]@{
    Type = 'Logon'
    UserId = $principal.UserId
    Enabled = $true
}
$taskSettings = [pscustomobject]@{
    Enabled = $true
    MultipleInstances = 'IgnoreNew'
    RestartCount = 999
    RestartInterval = 'PT1M'
    ExecutionTimeLimit = 'PT0S'
    StartWhenAvailable = $true
}
$exactAction = [pscustomobject]@{
    Execute = $expected.Execute
    Arguments = $expected.Arguments
    WorkingDirectory = $expected.WorkingDirectory
}
$ownedTask = [pscustomobject]@{
    Description = $constants.RunnerTaskOwnershipMarker
    Principal = $principal
    Actions = @($exactAction)
    Triggers = @($trigger)
    Settings = $taskSettings
    State = 'Ready'
}
$owned = Get-JobAgentTaskOwnership `
    -Task $ownedTask `
    -ExpectedAction $expected `
    -ExpectedUser $principal.UserId
Assert-Equal $owned.Classification 'OwnedExact' 'marker plus exact action is owned'

$legacyTask = [pscustomobject]@{
    Description = 'legacy task'
    Principal = $principal
    Actions = @($exactAction)
    Triggers = @($trigger)
    Settings = $taskSettings
    State = 'Ready'
}
$legacy = Get-JobAgentTaskOwnership `
    -Task $legacyTask `
    -ExpectedAction $expected `
    -ExpectedUser $principal.UserId
Assert-Equal $legacy.Classification 'LegacyAdoptable' 'exact legacy task requires adoption'

$driftedTask = [pscustomobject]@{
    Description = $constants.RunnerTaskOwnershipMarker
    Principal = $principal
    Actions = @(
        [pscustomobject]@{
            Execute = $expected.Execute
            Arguments = '-m unexpected.module'
            WorkingDirectory = $expected.WorkingDirectory
        }
    )
    Triggers = @($trigger)
    Settings = $taskSettings
    State = 'Ready'
}
$drifted = Get-JobAgentTaskOwnership `
    -Task $driftedTask `
    -ExpectedAction $expected `
    -ExpectedUser $principal.UserId
Assert-Equal $drifted.Classification 'OwnedDrifted' 'marker-owned task drift is explicit'

$settingsDriftTask = [pscustomobject]@{
    Description = $constants.RunnerTaskOwnershipMarker
    Principal = $principal
    Actions = @($exactAction)
    Triggers = @($trigger)
    Settings = [pscustomobject]@{
        Enabled = $true
        MultipleInstances = 'Parallel'
        RestartCount = 999
        RestartInterval = 'PT1M'
        ExecutionTimeLimit = 'PT0S'
        StartWhenAvailable = $true
    }
    State = 'Ready'
}
$settingsDrift = Get-JobAgentTaskOwnership `
    -Task $settingsDriftTask `
    -ExpectedAction $expected `
    -ExpectedUser $principal.UserId
Assert-Equal `
    $settingsDrift.Classification `
    'OwnedDrifted' `
    'task settings drift prevents exact ownership'

$disabledSettingsTask = [pscustomobject]@{
    Description = $constants.RunnerTaskOwnershipMarker
    Principal = $principal
    Actions = @($exactAction)
    Triggers = @($trigger)
    Settings = [pscustomobject]@{
        Enabled = $false
        MultipleInstances = 'IgnoreNew'
        RestartCount = 999
        RestartInterval = 'PT1M'
        ExecutionTimeLimit = 'PT0S'
        StartWhenAvailable = $true
    }
    State = 'Disabled'
}
$disabledSettings = Get-JobAgentTaskOwnership `
    -Task $disabledSettingsTask `
    -ExpectedAction $expected `
    -ExpectedUser $principal.UserId
Assert-Equal `
    $disabledSettings.Classification `
    'OwnedDrifted' `
    'disabled registration is never exact ownership'

$disabledStateTask = [pscustomobject]@{
    Description = $constants.RunnerTaskOwnershipMarker
    Principal = $principal
    Actions = @($exactAction)
    Triggers = @($trigger)
    Settings = $taskSettings
    State = 'Disabled'
}
$disabledState = Get-JobAgentTaskOwnership `
    -Task $disabledStateTask `
    -ExpectedAction $expected `
    -ExpectedUser $principal.UserId
Assert-Equal `
    $disabledState.Classification `
    'OwnedDrifted' `
    'disabled task state is never exact ownership'

$composePath = Join-Path $repository 'docker-compose.yml'
$labels = (
    "com.docker.compose.project=$($constants.ComposeProjectName)," +
    "com.docker.compose.project.working_dir=$repository," +
    "com.docker.compose.project.config_files=$composePath"
)
$containers = @(
    [pscustomobject]@{
        Project = $constants.ComposeProjectName
        Service = 'web-api'
        State = 'running'
        Status = 'Up 10 seconds'
        Labels = $labels
        Publishers = @(
            [pscustomobject]@{
                URL = '127.0.0.1'
                PublishedPort = 8000
                TargetPort = 8000
            }
        )
    }
)
$composeOwnership = Get-JobAgentComposeOwnership `
    -Containers $containers `
    -RepositoryPath $repository
Assert-Equal $composeOwnership.Classification 'OwnedExact' 'compose labels bind exact project'
$foreignContainers = @(
    [pscustomobject]@{
        Project = $constants.ComposeProjectName
        Service = 'web-api'
        Labels = $labels.Replace($repository, 'C:\foreign')
        Publishers = @()
    }
)
$foreignCompose = Get-JobAgentComposeOwnership `
    -Containers $foreignContainers `
    -RepositoryPath $repository
Assert-Equal $foreignCompose.Classification 'Foreign' 'foreign compose path is never owned'

$listeners = @(
    [pscustomobject]@{
        LocalAddress = '127.0.0.1'
        LocalPort = 8000
        OwningProcess = 4321
    }
)
$endpoint = Get-JobAgentEndpointOwnership `
    -Listeners $listeners `
    -Containers $containers `
    -Port 8000
Assert-Equal `
    $endpoint.Classification `
    'Unverifiable' `
    'publisher metadata plus an arbitrary listener is not exact ownership'
Assert-True `
    -Condition $endpoint.MetadataMatched `
    -Message 'running publisher metadata is retained for authenticated verification'
Assert-True `
    -Condition (-not $endpoint.Exact) `
    -Message 'unverified publisher metadata remains fail closed'
$verifiedEndpoint = Get-JobAgentEndpointOwnership `
    -Listeners $listeners `
    -Containers $containers `
    -Port 8000 `
    -AuthenticatedRuntimeVerified
Assert-Equal `
    $verifiedEndpoint.Classification `
    'OwnedExact' `
    'authenticated runtime identity upgrades matching publisher metadata'
Assert-Equal `
    $verifiedEndpoint.Proof `
    'AuthenticatedRuntimeIdentity' `
    'exact ownership records the authenticated proof source'
$wildcard = @(
    [pscustomobject]@{
        LocalAddress = '0.0.0.0'
        LocalPort = 8000
        OwningProcess = 4321
    }
)
$unsafeEndpoint = Get-JobAgentEndpointOwnership `
    -Listeners $wildcard `
    -Containers $containers `
    -Port 8000
Assert-Equal $unsafeEndpoint.Classification 'Foreign' 'wildcard listener is never owned'

$stoppedContainers = @(
    [pscustomobject]@{
        Project = $constants.ComposeProjectName
        Service = 'web-api'
        State = 'exited'
        Status = 'Exited (0)'
        Labels = $labels
        Publishers = $containers[0].Publishers
    }
)
$staleEndpoint = Get-JobAgentEndpointOwnership `
    -Listeners $listeners `
    -Containers $stoppedContainers `
    -Port 8000
Assert-Equal `
    $staleEndpoint.Classification `
    'Unverifiable' `
    'stopped container metadata cannot claim a live listener'
$missingPublisherContainers = @(
    [pscustomobject]@{
        Project = $constants.ComposeProjectName
        Service = 'web-api'
        State = 'running'
        Status = 'Up 10 seconds'
        Labels = $labels
    }
)
$missingPublisherEndpoint = Get-JobAgentEndpointOwnership `
    -Listeners $listeners `
    -Containers $missingPublisherContainers `
    -Port 8000
Assert-Equal `
    $missingPublisherEndpoint.Classification `
    'Unverifiable' `
    'running metadata without a publisher cannot claim a live listener'

$firstMutex = Enter-JobAgentRuntimeMutex -RepositoryPath $repository
try {
    Assert-Throws `
        -Action { Enter-JobAgentRuntimeMutex -RepositoryPath $repository | Out-Null } `
        -Pattern 'JOB_AGENT_COMMAND_ALREADY_RUNNING'
}
finally {
    Exit-JobAgentRuntimeMutex -Handle $firstMutex
}

$digestUi = 'sha256:' + (('b' * 64) -join '')
$digestSource = 'sha256:' + (('c' * 64) -join '')
$bootId = '00000000-0000-4000-8000-000000000123'
$startedAt = '2026-07-29T10:00:00Z'
$runtimePayload = @{
    release = @{
        build_sha = $build
        ui_asset_digest = $digestUi
        source_digest = $digestSource
        release_id = (('d' * 64) -join '')
        protocol_version = $constants.RuntimeProtocolVersion
        boot_id = $bootId
        started_at = $startedAt
    }
    mode = @{
        name = 'dry_run'
        dry_run = $true
        draft_only = $true
        live_submit_enabled = $false
    }
    readiness = @{ status = 'ready'; checks = @{ database = $true } }
    submission = @{ allowed = $false; reasons = @('DRY_RUN_ENABLED') }
    worker = @{
        build_sha = $build
        source_digest = $digestSource
        release_id = (('d' * 64) -join '')
        protocol_version = $constants.RuntimeProtocolVersion
        compatible = $true
    }
} | ConvertTo-Json -Depth 10 -Compress
$dashboardHtml = @"
<!doctype html>
<meta name="job-agent-build-sha" content="$build">
<meta name="job-agent-ui-digest" content="$digestUi">
<meta name="job-agent-source-digest" content="$digestSource">
<meta name="job-agent-protocol" content="$($constants.RuntimeProtocolVersion)">
<meta name="job-agent-boot-id" content="$bootId">
"@
$httpState = [pscustomobject]@{ Requests = 0; Opens = 0 }
$assertEqualCommand = ${function:Assert-Equal}
$assertTrueCommand = ${function:Assert-True}
$requestInvoker = {
    param([uri]$Uri, [hashtable]$Headers)

    $httpState.Requests++
    switch ($Uri.AbsolutePath) {
        '/health/live' {
            return [pscustomobject]@{ StatusCode = 200; Content = '{"status":"ok"}' }
        }
        '/health/ready' {
            return [pscustomobject]@{ StatusCode = 200; Content = '{"status":"ready"}' }
        }
        '/api/runtime/capabilities' {
            & $assertTrueCommand `
                -Condition ($Headers.Authorization -like 'Bearer *') `
                -Message 'runtime request is authenticated'
            return [pscustomobject]@{ StatusCode = 200; Content = $runtimePayload }
        }
        '/' {
            return [pscustomobject]@{ StatusCode = 200; Content = $dashboardHtml }
        }
        default {
            throw "UNEXPECTED_TEST_URI:$($Uri.AbsolutePath)"
        }
    }
}.GetNewClosure()
$delayInvoker = { param([int]$Milliseconds) }.GetNewClosure()
$browserLauncher = {
    param([string]$Url)

    & $assertEqualCommand `
        $Url `
        'http://127.0.0.1:8000/' `
        'browser receives exact loopback URL'
    $httpState.Opens++
}.GetNewClosure()
$whatIfOpen = Open-JobAgentDashboard `
    -DashboardUrl 'http://127.0.0.1:8000/' `
    -OperatorToken (('x' * 40) -join '') `
    -ExpectedBuildSha $build `
    -TimeoutSeconds 10 `
    -RequestInvoker $requestInvoker `
    -DelayInvoker $delayInvoker `
    -BrowserLauncher $browserLauncher `
    -WhatIf
Assert-True -Condition (-not $whatIfOpen.Opened) -Message 'WhatIf does not open browser'
Assert-Equal $httpState.Requests 0 'WhatIf performs no endpoint request'
Assert-Equal $httpState.Opens 0 'WhatIf performs no browser launch'

$opened = Open-JobAgentDashboard `
    -DashboardUrl 'http://127.0.0.1:8000/' `
    -OperatorToken (('x' * 40) -join '') `
    -ExpectedBuildSha $build `
    -TimeoutSeconds 10 `
    -RequestInvoker $requestInvoker `
    -DelayInvoker $delayInvoker `
    -BrowserLauncher $browserLauncher `
    -Confirm:$false
Assert-True -Condition $opened.Verified -Message 'runtime is verified before opening'
Assert-True -Condition $opened.Opened -Message 'verified dashboard opens'
Assert-Equal $httpState.Opens 1 'browser opens exactly once'

$bootstrapRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    'job-agent-bootstrap-whatif-' + [guid]::NewGuid()
)
$bootstrap = & (Join-Path $repository 'scripts\job_agent.ps1') bootstrap `
    -RepositoryPath $repository `
    -ControlPlaneUrl 'https://control.example' `
    -VercelProjectId 'prj_12345678abcdef' `
    -VercelScopeId 'team_12345678abcdef' `
    -LocalAppDataRoot $bootstrapRoot `
    -TaskName ('JobApplyAgent-Test-' + [guid]::NewGuid().ToString('N')) `
    -WhatIf
Assert-True -Condition (-not $bootstrap.Applied) -Message 'bootstrap WhatIf is not applied'
Assert-True `
    -Condition (-not (Test-Path -LiteralPath $bootstrapRoot)) `
    -Message 'bootstrap WhatIf creates no files'

Write-Output 'JobAgent Windows runtime tests passed.'
