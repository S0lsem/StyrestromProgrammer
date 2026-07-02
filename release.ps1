<#
.SYNOPSIS
    One-command release for the Styrestrom PLC Programmer.

.DESCRIPTION
    Bumps the version, builds the .exe, pushes the commit, publishes a GitHub
    release, and uploads the freshly built .exe as the release asset -- so the
    in-app self-updater has something to find.

    Run it, then everyone on an older build gets the "Update and Restart" button.

.PREREQUISITES
    A GitHub token in a file named  .github_token  in this folder (gitignored).
    Create a fine-grained token at:
        https://github.com/settings/tokens?type=beta
      - Resource owner:         S0lsem
      - Repository access:      Only select repositories -> StyrestromProgrammer
      - Repository permissions: Contents = Read and write
    Copy the token (starts with github_pat_...) into .github_token and save.

.EXAMPLE
    .\release.ps1 -Version 1.0.8
    .\release.ps1 -Version 1.0.8 -Notes "Friendly CAN FD scan message + self-update"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [string]$Notes = ''
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$RepoOwner = 'S0lsem'
$RepoName  = 'StyrestromProgrammer'
$ExeName   = 'Styrestrom_Programmer.exe'
$DistExe   = Join-Path $PSScriptRoot "dist\$ExeName"
$Tag       = "v$Version"

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# --- Locate Python (the one with the build deps installed) ------------------
$Python = "C:\Users\erlen\AppData\Local\Programs\Python\Python313\python.exe"
if (-not (Test-Path $Python)) { $Python = 'py' }

# --- Read the GitHub token (never printed) ----------------------------------
$TokenFile = Join-Path $PSScriptRoot '.github_token'
if (-not (Test-Path $TokenFile)) {
    throw "Missing .github_token. Create a fine-grained PAT (Contents read/write) and save it to $TokenFile. See the header of this script."
}
$Token = (Get-Content $TokenFile -Raw).Trim()
if (-not $Token) { throw ".github_token is empty." }
$Headers = @{
    Authorization          = "Bearer $Token"
    Accept                 = 'application/vnd.github+json'
    'X-GitHub-Api-Version' = '2022-11-28'
    'User-Agent'           = 'StyrestromRelease'
}

# --- Guard: does this release already exist? --------------------------------
Step "Checking $Tag is not already published"
try {
    Invoke-RestMethod -Uri "https://api.github.com/repos/$RepoOwner/$RepoName/releases/tags/$Tag" -Headers $Headers | Out-Null
    throw "Release $Tag already exists on GitHub. Pick a new version number."
} catch {
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode.value__ -ne 404) { throw }
    # 404 = good, it does not exist yet.
}

# --- 1. Bump version.py -----------------------------------------------------
Step "Bumping version.py to $Version"
$VersionPy = Join-Path $PSScriptRoot 'mrs_protocol\version.py'
[System.IO.File]::WriteAllText($VersionPy, "APP_VERSION = '$Version'`n", [System.Text.Encoding]::ASCII)

# --- 2. Commit (skip if version.py is unchanged) ----------------------------
Step "Committing the version bump"
git add mrs_protocol/version.py
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "version.py already at $Version - nothing to commit."
} else {
    git commit -m "Bump version to $Version"
    if ($LASTEXITCODE -ne 0) { throw "git commit failed." }
}

# --- 3. Build the exe -------------------------------------------------------
Step "Building $ExeName (PyInstaller - a few minutes)"
& $Python -m PyInstaller programmer_app.spec --clean --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
if (-not (Test-Path $DistExe)) { throw "Build reported success but $DistExe is missing." }
$Built = Get-Item $DistExe
Write-Host ("Built {0}  ({1:N1} MB, {2})" -f $ExeName, ($Built.Length / 1MB), $Built.LastWriteTime)

# --- 4. Push the commit -----------------------------------------------------
Step "Pushing to main"
git push origin main
if ($LASTEXITCODE -ne 0) { throw "git push failed." }
$Sha = (git rev-parse HEAD).Trim()

# --- 5. Create the release --------------------------------------------------
Step "Creating GitHub release $Tag"
$Body = @{
    tag_name         = $Tag
    target_commitish = $Sha
    name             = $Tag
    body             = $Notes
    draft            = $false
    prerelease       = $false
} | ConvertTo-Json
$Release = Invoke-RestMethod -Method Post `
    -Uri "https://api.github.com/repos/$RepoOwner/$RepoName/releases" `
    -Headers $Headers -Body $Body -ContentType 'application/json'

# --- 6. Upload the exe asset ------------------------------------------------
Step "Uploading $ExeName"
$UploadUrl = ($Release.upload_url -replace '\{\?.*\}', '') + "?name=$ExeName"
Invoke-RestMethod -Method Post -Uri $UploadUrl -Headers $Headers `
    -ContentType 'application/octet-stream' -InFile $DistExe -TimeoutSec 600 | Out-Null

# --- 7. Verify --------------------------------------------------------------
Step "Verifying"
$Check = Invoke-RestMethod -Uri "https://api.github.com/repos/$RepoOwner/$RepoName/releases/latest" -Headers $Headers
$Assets = ($Check.assets | ForEach-Object { $_.name }) -join ', '
Write-Host ""
if ($Check.tag_name -eq $Tag -and $Assets -match [regex]::Escape($ExeName)) {
    Write-Host "SUCCESS: published $($Check.tag_name) with asset(s): $Assets" -ForegroundColor Green
    Write-Host "Older builds will now show the 'Update and Restart' button." -ForegroundColor Green
} else {
    throw "Verification failed. latest=$($Check.tag_name) assets=$Assets"
}
