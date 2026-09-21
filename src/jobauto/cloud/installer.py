"""Serve an installer so a fresh PC needs nothing but one command.

The alternative -- clone the repo, install dependencies, set PYTHONPATH -- is
fine for the person who wrote it and a wall for anyone else. These endpoints
let the deployment bootstrap its own agent:

    GET /install.ps1   a PowerShell bootstrap with this site's URL baked in
    GET /agent.zip     the agent source, as an installable package

Neither needs authentication and neither carries a secret: the code is not
sensitive, and the agent token is typed in by the person running the installer.
Publishing to PyPI or making the repo public would work too, but this keeps the
whole thing inside the deployment you already control.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]      # src/jobauto
SRC_ROOT = PACKAGE_ROOT.parent                          # src
PROJECT_ROOT = SRC_ROOT.parent

# The cloud app's own modules are useless on an agent machine and pull in
# dependencies it does not need.
EXCLUDED_DIRS = {"cloud", "web", "__pycache__"}


def build_agent_zip() -> bytes:
    """Zip the agent half of the package, plus a minimal pyproject."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE_ROOT.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix not in (".py", ".yaml", ".yml"):
                continue
            relative = path.relative_to(PACKAGE_ROOT)
            if set(relative.parts) & EXCLUDED_DIRS:
                continue
            archive.write(path, f"jobauto-agent/src/jobauto/{relative.as_posix()}")

        archive.writestr("jobauto-agent/pyproject.toml", _AGENT_PYPROJECT)
        archive.writestr("jobauto-agent/README.md", _AGENT_README)
    return buffer.getvalue()


_AGENT_PYPROJECT = """\
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "jobauto"
version = "0.2.0"
description = "jobauto agent -- runs job searches and fills applications locally"
requires-python = ">=3.10"
dependencies = ["playwright>=1.44", "PyYAML>=6.0", "requests>=2.31"]

[project.scripts]
jobauto = "jobauto.cli:main"

[tool.setuptools]
package-dir = { "" = "src" }
include-package-data = true

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
"jobauto" = ["defaults/*.yaml", "defaults/portals/*.yaml"]
"""

_AGENT_README = """\
# jobauto agent

Runs on your PC: searches the job portals, scores the results against your
preferences, and fills in applications. It never submits one -- you do that.

Your portal logins stay on this machine. The agent connects outbound to your
jobauto site to collect work and report results; nothing is exposed on your
network.

    jobauto doctor     check the configuration
    jobauto login      sign in to each portal once, by hand
    jobauto agent      start syncing

Config and history live in ~/.jobauto.
"""


def installer_script(base_url: str) -> str:
    """PowerShell bootstrap, with this deployment's URL baked in."""
    return _INSTALLER.replace("__BASE_URL__", base_url.rstrip("/"))


_INSTALLER = r"""
# jobauto agent installer
#
#   irm __BASE_URL__/install.ps1 | iex
#
# Installs into ~/.jobauto: its own virtual environment, so nothing else on the
# machine is touched and removing that folder removes everything.

$ErrorActionPreference = 'Stop'
$Base = '__BASE_URL__'

# Run via `irm ... | iex` the script shares the window's session, so `exit`
# closes the window -- taking the error message with it, which is exactly when
# you need to read it. Everything ends through here instead.
function Finish([int]$code = 0) {
    Write-Host ''
    if ($code -ne 0) {
        Write-Host '  Nothing was linked. The messages above explain why.' -ForegroundColor Yellow
    }
    Write-Host '  Press Enter to close.' -ForegroundColor DarkGray
    try { [void](Read-Host) } catch { }
    if ($MyInvocation.MyCommand.CommandType -eq 'ExternalScript') { exit $code }
    return
}
$Home_ = Join-Path $env:USERPROFILE '.jobauto'
$Venv  = Join-Path $Home_ 'venv'
$Py    = Join-Path $Venv 'Scripts\python.exe'
$Exe   = Join-Path $Venv 'Scripts\jobauto.exe'

Write-Host ''
Write-Host '  jobauto agent installer' -ForegroundColor Cyan
Write-Host "  site: $Base" -ForegroundColor DarkGray
Write-Host ''

# ---------------------------------------------------------------- python
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    try {
        $version = & $candidate --version 2>&1
        if ($version -match 'Python (\d+)\.(\d+)') {
            if ([int]$Matches[1] -ge 3 -and [int]$Matches[2] -ge 10) {
                $python = $candidate
                Write-Host "  found $version" -ForegroundColor Green
                break
            }
        }
    } catch { }
}

if (-not $python) {
    Write-Host '  Python 3.10 or newer is required and was not found.' -ForegroundColor Red
    Write-Host '  Install it from https://www.python.org/downloads/ and tick'
    Write-Host '  "Add python.exe to PATH", then run this installer again.'
    Write-Host ''
    Write-Host '  Or:  winget install Python.Python.3.12'
    Finish 1
}

# ------------------------------------------------------------ environment
New-Item -ItemType Directory -Force -Path $Home_ | Out-Null

if (-not (Test-Path $Py)) {
    Write-Host '  creating a private environment ...'
    & $python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Write-Host '  could not create it.' -ForegroundColor Red; Finish 1 }
}

Write-Host '  downloading the agent ...'
$zip = Join-Path $env:TEMP 'jobauto-agent.zip'
$dir = Join-Path $env:TEMP 'jobauto-agent-src'
Invoke-WebRequest -Uri "$Base/agent.zip" -OutFile $zip -UseBasicParsing
if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
Expand-Archive -Path $zip -DestinationPath $dir -Force

Write-Host '  installing (this pulls in Playwright, ~1-2 minutes) ...'
& $Py -m pip install --quiet --upgrade pip
& $Py -m pip install --quiet (Join-Path $dir 'jobauto-agent')
if ($LASTEXITCODE -ne 0) { Write-Host '  install failed.' -ForegroundColor Red; Finish 1 }

Write-Host '  downloading a browser for the automation ...'
& $Py -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Host '  browser download failed -- run this later:' -ForegroundColor Yellow
    Write-Host "    $Py -m playwright install chromium"
}

# ------------------------------------------------------------------ link
Write-Host ''
Write-Host '  Paste the agent token from your dashboard (Devices tab).' -ForegroundColor Cyan
Write-Host '  Sign in, open Devices, and use the Copy token button.' -ForegroundColor DarkGray
$token = (Read-Host '  token').Trim()

if (-not $token) {
    Write-Host ''
    Write-Host '  No token given. Get one from Devices and run the installer again.' -ForegroundColor Yellow
    Finish 1
}

# Check the token against the API before handing it to the CLI, so a bad token
# produces a plain sentence instead of a stack trace.
Write-Host '  checking the token ...'
$ok = $false
try {
    $probe = Invoke-WebRequest -Uri "$Base/api/agent/hello" -Method POST `
        -Headers @{ 'X-Agent-Token' = $token } `
        -ContentType 'application/json' -Body '{"status":"installing"}' `
        -UseBasicParsing -TimeoutSec 30
    $ok = ($probe.StatusCode -eq 200)
} catch {
    $code = $_.Exception.Response.StatusCode.value__
    if ($code -eq 401) {
        Write-Host ''
        Write-Host '  That token was rejected by the server.' -ForegroundColor Red
        Write-Host ''
        Write-Host '  Usually one of:' -ForegroundColor DarkGray
        Write-Host '    - it was copied partially; use the Copy token button rather than' -ForegroundColor DarkGray
        Write-Host '      selecting it by hand' -ForegroundColor DarkGray
        Write-Host '    - it was rotated since you copied it; open Devices and copy again' -ForegroundColor DarkGray
        Write-Host '    - you have not created an account on the site yet, so no token' -ForegroundColor DarkGray
        Write-Host "      exists -- open $Base and sign up first" -ForegroundColor DarkGray
        Write-Host ''
        Write-Host "  token received: $($token.Length) characters, starts '$($token.Substring(0, [Math]::Min(6, $token.Length)))'" -ForegroundColor DarkGray
        Finish 1
    }
    Write-Host ''
    Write-Host "  Could not reach $Base : $($_.Exception.Message)" -ForegroundColor Red
    Finish 1
}

if (-not $ok) { Write-Host '  the server did not accept the token.' -ForegroundColor Red; Finish 1 }
Write-Host '  token accepted.' -ForegroundColor Green

# --token=VALUE, not --token VALUE: a token beginning with '-' would otherwise
# be read as an option name rather than a value.
& $Exe link --url="$Base" --token="$token"
if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host '  Linking failed even though the token was accepted.' -ForegroundColor Red
    Write-Host '  The error above is from the agent. Try it again by hand:' -ForegroundColor DarkGray
    Write-Host "    $Exe link --url=`"$Base`" --token=`"<your token>`"" -ForegroundColor DarkGray
    Finish 1
}

# -------------------------------------------------------------- autostart
Write-Host ''
$auto = Read-Host '  Start the agent automatically when you log in? (y/N)'
if ($auto -match '^[Yy]') {
    $registered = $false

    # Preferred, but Register-ScheduledTask needs elevation on many machines
    # and fails with "Access is denied" without it.
    try {
        $action  = New-ScheduledTaskAction -Execute $Exe -Argument 'agent'
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName 'jobauto-agent' -Action $action `
            -Trigger $trigger -Force -Description 'jobauto cloud sync agent' | Out-Null
        Write-Host '  registered as a scheduled task -- starts at every login.' -ForegroundColor Green
        $registered = $true
    } catch { }

    # A shortcut in the Startup folder is per-user and needs no privileges, so
    # it works where the scheduler does not.
    if (-not $registered) {
        try {
            $startup = [Environment]::GetFolderPath('Startup')
            $link = Join-Path $startup 'jobauto-agent.lnk'
            $shell = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($link)
            $shortcut.TargetPath = $Exe
            $shortcut.Arguments = 'agent'
            $shortcut.WorkingDirectory = $Home_
            $shortcut.Description = 'jobauto cloud sync agent'
            $shortcut.WindowStyle = 7          # start minimised
            $shortcut.Save()
            Write-Host '  added to your Startup folder -- starts at every login.' -ForegroundColor Green
            Write-Host "  remove it any time: $link" -ForegroundColor DarkGray
            $registered = $true
        } catch {
            Write-Host "  could not set up autostart: $_" -ForegroundColor Yellow
        }
    }

    if (-not $registered) {
        Write-Host '  Start it by hand instead:' -ForegroundColor DarkGray
        Write-Host "    $Exe agent" -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------- portal logins
# Two essential steps remain, and ending on "DONE" with a list of commands
# reads as finished -- so people stop here, and then nothing ever happens.
# Offer to do them instead.
Write-Host ''
Write-Host '  ------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host '  Installed and linked. Two things left, and nothing works' -ForegroundColor Cyan
Write-Host '  until both are done.' -ForegroundColor Cyan
Write-Host '  ------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  1. Sign in to the job portals.' -ForegroundColor White
Write-Host '     A real browser opens, one portal at a time. Sign in by hand,'
Write-Host '     OTP included. No password is stored -- only the session, the'
Write-Host '     same way your normal browser keeps you signed in.'
Write-Host ''
$doLogin = Read-Host '     Do this now? (Y/n)'
if ($doLogin -notmatch '^[Nn]') {
    Write-Host ''
    & $Exe login
    Write-Host ''
    if ($LASTEXITCODE -ne 0) {
        Write-Host '     Sign-in did not complete. You can retry any time:' -ForegroundColor Yellow
        Write-Host "       $Exe login" -ForegroundColor DarkGray
    }
} else {
    Write-Host ''
    Write-Host '     Skipped. Searches will find nothing until you run:' -ForegroundColor Yellow
    Write-Host "       $Exe login" -ForegroundColor DarkGray
}

# ----------------------------------------------------------------- agent
Write-Host ''
Write-Host '  2. Start the agent.' -ForegroundColor White
Write-Host '     It polls your site for work and does the searching and'
Write-Host '     form filling. Leave it running; close it whenever you like.'
Write-Host ''
$doAgent = Read-Host '     Start it now? (Y/n)'

Write-Host ''
Write-Host '  Everything lives in ~/.jobauto -- delete that folder to remove it all.' -ForegroundColor DarkGray
Write-Host "  Dashboard: $Base" -ForegroundColor DarkGray
Write-Host ''

if ($doAgent -notmatch '^[Nn]') {
    Write-Host '  Starting the agent. Ctrl-C to stop it.' -ForegroundColor Green
    Write-Host '  Watch progress at ' -NoNewline -ForegroundColor DarkGray
    Write-Host "$Base" -ForegroundColor Cyan
    Write-Host ''
    # Runs in this window so its output is visible. This is the last step, so
    # blocking here is what the user wants.
    & $Exe agent
    Finish 0
}

Write-Host '  Start it when you are ready:' -ForegroundColor DarkGray
Write-Host "    $Exe agent" -ForegroundColor Cyan
Finish 0
"""
