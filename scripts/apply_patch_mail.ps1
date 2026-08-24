[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string[]]$Patch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

function Invoke-GitCapture {
    param([string[]]$Arguments)

    $savedErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& git -c "safe.directory=$repoRoot" -C $repoRoot @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $savedErrorPreference
    }
    $lines = @($output | ForEach-Object { $_.ToString() })
    $lines = @($lines | Where-Object {
        $_ -notmatch "^warning: unable to access '.+[/\\]\.config[/\\]git[/\\]ignore': Permission denied$"
    })
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $lines -join [Environment]::NewLine
    }
}

$status = Invoke-GitCapture @('status', '--porcelain')
if ($status.ExitCode -ne 0) {
    throw "Could not inspect the working tree:`n$($status.Output)"
}
if ($status.Output) {
    throw "Working tree is not clean. No patches were applied:`n$($status.Output)"
}

$start = Invoke-GitCapture @('rev-parse', 'HEAD')
if ($start.ExitCode -ne 0) {
    throw "Could not read starting SHA:`n$($start.Output)"
}
$startSha = $start.Output.Trim()
Write-Output "Starting SHA: $startSha"

$configuredEmail = (Invoke-GitCapture @('config', '--local', 'user.email')).Output.Trim()
$configuredName = (Invoke-GitCapture @('config', '--local', 'user.name')).Output.Trim()
if (-not $configuredEmail -or -not $configuredName) {
    throw 'Local user.name and user.email must both be configured before applying mail patches.'
}

$results = @()
$failed = $false
$failureOutput = ''
foreach ($patchName in $Patch) {
    $candidate = if ([System.IO.Path]::IsPathRooted($patchName)) {
        $patchName
    } else {
        Join-Path $repoRoot $patchName
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        $results += [pscustomobject]@{ Patch = $patchName; Status = 'MISSING'; Commit = '' }
        continue
    }
    $patchPath = (Resolve-Path -LiteralPath $candidate).Path

    $reverse = Invoke-GitCapture @('apply', '--check', '--reverse', '--', $patchPath)
    if ($reverse.ExitCode -eq 0) {
        $results += [pscustomobject]@{
            Patch = (Split-Path -Leaf $patchPath)
            Status = 'ALREADY APPLIED'
            Commit = ''
        }
        continue
    }

    $applied = Invoke-GitCapture @('am', $patchPath)
    if ($applied.ExitCode -ne 0) {
        [void](Invoke-GitCapture @('am', '--abort'))
        $applied = Invoke-GitCapture @('am', '--3way', $patchPath)
    }
    if ($applied.ExitCode -ne 0) {
        [void](Invoke-GitCapture @('am', '--abort'))
        $results += [pscustomobject]@{
            Patch = (Split-Path -Leaf $patchPath)
            Status = 'FAILED'
            Commit = ''
        }
        $failureOutput = $applied.Output
        $failed = $true
        break
    }

    $amend = Invoke-GitCapture @('commit', '--amend', '--reset-author', '--no-edit')
    if ($amend.ExitCode -ne 0) {
        throw "Patch applied, but author reset failed:`n$($amend.Output)"
    }
    $authorEmail = (Invoke-GitCapture @('log', '-1', '--format=%ae')).Output.Trim()
    $authorName = (Invoke-GitCapture @('log', '-1', '--format=%an')).Output.Trim()
    if ($authorEmail -ne $configuredEmail -or $authorName -ne $configuredName) {
        throw (
            "Author verification failed. Expected '$configuredName <$configuredEmail>', " +
            "got '$authorName <$authorEmail>'."
        )
    }
    $commitSha = (Invoke-GitCapture @('rev-parse', 'HEAD')).Output.Trim()
    $results += [pscustomobject]@{
        Patch = (Split-Path -Leaf $patchPath)
        Status = 'APPLIED'
        Commit = $commitSha
    }
}

$results | Format-Table -AutoSize | Out-String | Write-Output
if ($failed) {
    Write-Output "Patch failed after the 3-way retry:"
    Write-Output $failureOutput
}
Write-Output "Rollback: git reset --hard $startSha"
if ($failed) {
    exit 1
}
