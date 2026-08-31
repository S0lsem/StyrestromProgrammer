<#
.SYNOPSIS
    Promote a prerelease so distributors are offered it.

.DESCRIPTION
    Clears the prerelease flag on an existing release. Distributors' clients
    ask GitHub for /releases/latest, which skips prereleases -- so until this
    runs, only HQ accounts are offered the build. Afterwards everyone on an
    older version sees "Update and Restart" on their next launch.

    Nothing is rebuilt or re-uploaded: the asset published by release.ps1 is
    exactly what gets promoted, so what you tested is what they receive.

.PREREQUISITES
    The same .github_token release.ps1 uses (Contents = Read and write on
    StyrestromProgrammer).

.EXAMPLE
    .\promote.ps1 -Version 1.0.18
    .\promote.ps1 -Version 1.0.18 -WhatIf     # show what would change
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version
)

$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot

$RepoOwner = 'S0lsem'
$RepoName  = 'StyrestromProgrammer'
$ExeName   = 'Styrestrom_Programmer.exe'
$Tag       = "v$Version"

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

$TokenFile = Join-Path $PSScriptRoot '.github_token'
if (-not (Test-Path $TokenFile)) { throw "Missing .github_token." }
$Token = (Get-Content $TokenFile -Raw).Trim()
if (-not $Token) { throw ".github_token is empty." }

$Headers = @{
    Authorization          = "Bearer $Token"
    Accept                 = 'application/vnd.github+json'
    'X-GitHub-Api-Version' = '2022-11-28'
    'User-Agent'           = 'StyrestromPromote'
}

# --- Find it ----------------------------------------------------------------
Step "Looking up $Tag"
try {
    $Release = Invoke-RestMethod -Headers $Headers `
        -Uri "https://api.github.com/repos/$RepoOwner/$RepoName/releases/tags/$Tag"
} catch {
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode.value__ -eq 404) {
        throw "No release tagged $Tag. Publish it first with release.ps1."
    }
    throw
}

$Assets = ($Release.assets | ForEach-Object { $_.name }) -join ', '
Write-Host "  tag        : $($Release.tag_name)"
Write-Host "  published  : $($Release.published_at)"
Write-Host "  prerelease : $($Release.prerelease)"
Write-Host "  assets     : $Assets"

# --- Guards -----------------------------------------------------------------
# Promoting a release with no .exe would offer every distributor an update
# they cannot download, so refuse rather than half-do it.
if ($Assets -notmatch [regex]::Escape($ExeName)) {
    throw "Release $Tag has no $ExeName asset. Refusing to promote - distributors would see an update they cannot download."
}

# Deliberately not short-circuiting when prerelease is already false: the flag
# and the "latest" pointer are separate pieces of state, and a half-finished
# promotion leaves the first cleared while the second still points elsewhere.
# Re-running must be able to finish the job.
if (-not $Release.prerelease) {
    Write-Host "  already flagged as a full release - will re-assert it as latest." -ForegroundColor Yellow
}

# --- Promote ----------------------------------------------------------------
if (-not $PSCmdlet.ShouldProcess("$Tag", "clear the prerelease flag (offer it to all distributors)")) {
    return
}

Step "Promoting $Tag to all distributors"
# make_latest matters as much as prerelease. GitHub stores which release is
# "latest" rather than deriving it on every request, and clearing the
# prerelease flag alone does not reliably recompute it -- v1.0.19 sat as the
# newest non-draft, non-prerelease release while /releases/latest still served
# v1.0.17, for minutes, on authenticated requests too. Setting make_latest
# says it outright instead of hoping GitHub works it out.
$Body = @{ prerelease = $false; make_latest = 'true' } | ConvertTo-Json
Invoke-RestMethod -Method Patch -Headers $Headers -ContentType 'application/json' `
    -Uri "https://api.github.com/repos/$RepoOwner/$RepoName/releases/$($Release.id)" `
    -Body $Body | Out-Null

# --- Verify -----------------------------------------------------------------
# Check the way a distributor's client does: /releases/latest, unauthenticated.
# That is the call update_checker.py makes, so it is the only one that proves
# the promotion actually landed for them.
Step "Verifying as a distributor would see it"
$Latest = Invoke-RestMethod -Uri "https://api.github.com/repos/$RepoOwner/$RepoName/releases/latest" `
    -Headers @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'StyrestromPromote' }

Write-Host ""
if ($Latest.tag_name -eq $Tag) {
    Write-Host "SUCCESS: $Tag is now the latest release." -ForegroundColor Green
    Write-Host "Distributors on an older build will see 'Update and Restart' on next launch." -ForegroundColor Green
} else {
    throw "Promotion did not take: /releases/latest still reports $($Latest.tag_name)."
}
