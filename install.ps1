<#
.SYNOPSIS
    Installs the latest published GitHub Release of Repo360 via pipx.
.DESCRIPTION
    Queries the GitHub Releases API for Adithya-Meda/repo360, prefers the
    pre-built wheel (.whl) asset for fast installation, falls back to the
    exact release tag, and installs via pipx.
#>
$ErrorActionPreference = "Stop"

$Repo = "Adithya-Meda/repo360"
$ApiUrl = "https://api.github.com/repos/$Repo/releases/latest"

if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    Write-Error "pipx is required but was not found on PATH. Install it with: python -m pip install --user pipx && python -m pipx ensurepath"
    exit 1
}

Write-Host "Fetching latest Repo360 release metadata..." -ForegroundColor Cyan
$Headers = @{
    "Accept"     = "application/vnd.github+json"
    "User-Agent" = "repo360-installer"
}
$Release = Invoke-RestMethod -Uri $ApiUrl -Headers $Headers -TimeoutSec 20
$Tag = [string]$Release.tag_name

if ($Tag -notmatch '^v?\d+\.\d+\.\d+$') {
    Write-Error "Unexpected release tag returned by GitHub API: '$Tag'"
    exit 1
}

$WheelUrl = $null
if ($Release.assets) {
    foreach ($Asset in $Release.assets) {
        $Url = [string]$Asset.browser_download_url
        if ($Url -like "https://github.com/$Repo/releases/download/*" -and $Url.EndsWith(".whl")) {
            $WheelUrl = $Url
            break
        }
    }
}

$TargetSpec = if ($WheelUrl) { $WheelUrl } else { "git+https://github.com/$Repo.git@$Tag" }
Write-Host "Installing Repo360 $Tag from $TargetSpec..." -ForegroundColor Cyan

pipx install --force $TargetSpec
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host "Installed Repo360 $Tag successfully." -ForegroundColor Green
if (Get-Command repo360 -ErrorAction SilentlyContinue) {
    repo360 version
}
else {
    Write-Host "Run 'pipx ensurepath' and restart your terminal if 'repo360' is not yet on PATH." -ForegroundColor Yellow
}
