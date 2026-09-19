<#
    Creates a PRIVATE GitHub repo under your account and pushes this project.

    Needs a one-time browser sign-in to GitHub; everything else is automatic.

    Usage:
        .\push-to-github.ps1
        .\push-to-github.ps1 -RepoName jobauto -Public    # if you ever want it public
#>
param(
    [string]$RepoName = "jobauto",
    [switch]$Public
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$gh = Join-Path $root 'tools\gh.exe'
if (-not (Test-Path $gh)) {
    Write-Host "  tools\gh.exe is missing. Fetch it with:" -ForegroundColor Red
    Write-Host "    curl -L -o tools\gh.zip https://github.com/cli/cli/releases/latest/download/gh_2.83.2_windows_amd64.zip"
    Write-Host "    tar -xf tools\gh.zip -C tools"
    exit 1
}

# ------------------------------------------------------------- sign in
& $gh auth status 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "  Signing in to GitHub." -ForegroundColor Cyan
    Write-Host "  A code appears below -- your browser opens, paste it there." -ForegroundColor DarkGray
    Write-Host ""
    & $gh auth login --hostname github.com --git-protocol https --web --scopes repo
    if ($LASTEXITCODE -ne 0) { Write-Host "  sign-in failed." -ForegroundColor Red; exit 1 }
}

$account = (& $gh api user --jq .login)
Write-Host ""
Write-Host "  signed in as $account" -ForegroundColor Green

# Commits are attributed by the email in git config, not by who is signed in.
# If that email is not on your GitHub account, commits will not link to your
# profile -- harmless, but worth knowing.
$email = (git config user.email)
Write-Host "  commits authored as: $email" -ForegroundColor DarkGray

# ------------------------------------------------------------ the repo
$visibility = if ($Public) { '--public' } else { '--private' }
Write-Host ""
Write-Host "  creating $account/$RepoName ($(if ($Public) {'public'} else {'private'})) ..." -ForegroundColor Cyan

& $gh repo create $RepoName $visibility --source=. --remote=origin --push
if ($LASTEXITCODE -ne 0) {
    # Most likely the repo already exists; fall back to adding the remote.
    Write-Host "  create failed -- trying to push to an existing repo" -ForegroundColor Yellow
    $url = "https://github.com/$account/$RepoName.git"
    if (-not (git remote 2>$null | Select-String -Quiet '^origin$')) {
        git remote add origin $url
    }
    git push -u origin (git branch --show-current)
    if ($LASTEXITCODE -ne 0) { Write-Host "  push failed." -ForegroundColor Red; exit 1 }
}

Write-Host ""
Write-Host "  DONE" -ForegroundColor Green
Write-Host "  https://github.com/$account/$RepoName"
Write-Host ""
Write-Host "  Next: deploy it so you can use it from anywhere --" -ForegroundColor DarkGray
Write-Host "    render.com -> New -> Blueprint -> pick this repo -> Apply" -ForegroundColor DarkGray
Write-Host "    full walkthrough in docs\DEPLOY.md" -ForegroundColor DarkGray
Write-Host ""
