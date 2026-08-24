[CmdletBinding()]
param(
    [ValidateSet('all', 'normal', 'reversed', 'oracle', 'pristine')]
    [string]$Lane = 'all',
    [string]$OutputRoot = '',
    [string]$Python = 'python',
    [switch]$ListOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$hostName = if ($env:PROAM_RIG_HOST) { $env:PROAM_RIG_HOST.Trim() } else { '127.0.0.1' }
$allowedHosts = @('localhost', '127.0.0.1', '::1')
if ($allowedHosts -notcontains $hostName.ToLowerInvariant()) {
    throw "Refusing non-local PostgreSQL host '$hostName'. This runner is local-only."
}

$laneDefinitions = [ordered]@{
    normal = @{
        Template = 'proam_prod_mirror_p0'
        Target = 'proam_regression'
    }
    reversed = @{
        Template = 'proam_prod_mirror_p0rev'
        Target = 'proam_regression'
    }
    oracle = @{
        Template = 'proam_prod_mirror_mt'
        Target = 'proam_regression'
    }
    pristine = @{
        Template = 'proam_prod_mirror_2026pristine'
        Target = 'proam_regression/test_college_id_reseed.py'
    }
}
$selectedLanes = if ($Lane -eq 'all') { @($laneDefinitions.Keys) } else { @($Lane) }

if ($ListOnly) {
    foreach ($name in $selectedLanes) {
        $definition = $laneDefinitions[$name]
        Write-Output "$name`t$($definition.Template)`t$($definition.Target)"
    }
    exit 0
}

$gitSha = (& git -c "safe.directory=$repoRoot" -C $repoRoot rev-parse HEAD 2>&1).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not read the Git revision: $gitSha"
}
$gitBranch = (& git -c "safe.directory=$repoRoot" -C $repoRoot branch --show-current 2>&1).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not read the Git branch: $gitBranch"
}

if (-not $OutputRoot) {
    $OutputRoot = Join-Path ([System.IO.Path]::GetTempPath()) 'proam-regression-receipts'
}
$runStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$shortSha = $gitSha.Substring(0, [Math]::Min(12, $gitSha.Length))
$runRoot = Join-Path $OutputRoot "$runStamp-$shortSha"
$tempRoot = Join-Path $runRoot 'temp'
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

$receiptPath = Join-Path $runRoot 'RECEIPT.md'
@(
    '# Missoula Pro-Am regression receipt'
    ''
    "- Started: $((Get-Date).ToString('o'))"
    "- Git SHA: $gitSha"
    "- Branch: $gitBranch"
    "- PostgreSQL host: $hostName"
    "- Requested lane: $Lane"
    ''
    '| Lane | Template | Exit | Remaining run clones | Log |'
    '|---|---|---:|---:|---|'
) | Set-Content -LiteralPath $receiptPath -Encoding utf8

$variableNames = @(
    'PROAM_APP_ROOT', 'PROAM_RIG_HOST', 'PROAM_RIG_TEMPLATE',
    'PROAM_RIG_RUN_TOKEN', 'SECRET_KEY', 'PGPASSWORD'
)
$savedEnvironment = @{}
foreach ($variableName in $variableNames) {
    $savedEnvironment[$variableName] = [Environment]::GetEnvironmentVariable(
        $variableName, 'Process'
    )
}

$userName = if ($env:PROAM_RIG_USER) { $env:PROAM_RIG_USER } else { 'proam' }
$password = if ($env:PROAM_RIG_PASS) { $env:PROAM_RIG_PASS } else { 'proam' }
$port = if ($env:PROAM_RIG_PORT) { $env:PROAM_RIG_PORT } else { '5432' }
$failed = $false

try {
    $env:PROAM_APP_ROOT = $repoRoot
    $env:PROAM_RIG_HOST = $hostName
    $env:SECRET_KEY = 'local-regression-receipt-only-secret-key-2026'
    $env:PGPASSWORD = $password

    foreach ($name in $selectedLanes) {
        $definition = $laneDefinitions[$name]
        $runToken = [Guid]::NewGuid().ToString('N').Substring(0, 12)
        $laneTemp = Join-Path $tempRoot $name
        $baseTemp = Join-Path $laneTemp 'pytest-base'
        $cacheDir = Join-Path $laneTemp 'pytest-cache'
        $logPath = Join-Path $runRoot "$name.log"
        New-Item -ItemType Directory -Path $laneTemp -Force | Out-Null

        $env:PROAM_RIG_TEMPLATE = $definition.Template
        $env:PROAM_RIG_RUN_TOKEN = $runToken

        Write-Host "Running $name lane against $($definition.Template)..."
        $pytestArguments = @(
            '-m', 'pytest',
            '-o', 'addopts=--tb=short',
            '-o', "cache_dir=$cacheDir",
            "--basetemp=$baseTemp",
            '-p', 'no:randomly',
            '-q', $definition.Target
        )
        & $Python @pytestArguments 2>&1 | Tee-Object -FilePath $logPath
        $laneExit = $LASTEXITCODE

        $cloneQuery = "SELECT datname FROM pg_database WHERE datname LIKE 'proam_rt_$runToken`_%';"
        $remainingOutput = @(
            & psql -X -qAt -h $hostName -p $port -U $userName -d postgres -c $cloneQuery 2>&1
        )
        $cloneCheckExit = $LASTEXITCODE
        $remainingClones = @($remainingOutput | Where-Object { $_.ToString().Trim() })
        if ($cloneCheckExit -ne 0) {
            $remainingCount = 'CHECK FAILED'
            $laneExit = if ($laneExit -eq 0) { 1 } else { $laneExit }
            $remainingOutput | Add-Content -LiteralPath $logPath
        } else {
            $remainingCount = $remainingClones.Count
            if ($remainingClones.Count -gt 0) {
                $laneExit = if ($laneExit -eq 0) { 1 } else { $laneExit }
                "Remaining run-owned clones: $($remainingClones -join ', ')" |
                    Add-Content -LiteralPath $logPath
            }
        }

        $logName = Split-Path -Leaf $logPath
        "| $name | $($definition.Template) | $laneExit | $remainingCount | $logName |" |
            Add-Content -LiteralPath $receiptPath
        if ($laneExit -ne 0) {
            $failed = $true
        }
    }
}
finally {
    foreach ($variableName in $variableNames) {
        [Environment]::SetEnvironmentVariable(
            $variableName, $savedEnvironment[$variableName], 'Process'
        )
    }
    @(
        ''
        "Finished: $((Get-Date).ToString('o'))"
    ) | Add-Content -LiteralPath $receiptPath
}

Write-Output "Receipt: $receiptPath"
if ($failed) {
    exit 1
}
