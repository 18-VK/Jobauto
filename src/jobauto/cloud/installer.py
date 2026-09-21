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
    exit 1
}

# ------------------------------------------------------------ environment
New-Item -ItemType Directory -Force -Path $Home_ | Out-Null

if (-not (Test-Path $Py)) {
    Write-Host '  creating a private environment ...'
    & $python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Write-Host '  could not create it.' -ForegroundColor Red; exit 1 }
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
if ($LASTEXITCODE -ne 0) { Write-Host '  install failed.' -ForegroundColor Red; exit 1 }

Write-Host '  downloading a browser for the automation ...'
& $Py -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Host '  browser download failed -- run this later:' -ForegroundColor Yellow
    Write-Host "    $Py -m playwright install chromium"
}

# ------------------------------------------------------------------ link
Write-Host ''
Write-Host '  Paste the agent token from your dashboard (Devices tab).' -ForegroundColor Cyan
$token = Read-Host '  token'
if (-not $token) { Write-Host '  no token given; run the installer again when you have it.'; exit 1 }

& $Exe link --url $Base --token $token.Trim()
if ($LASTEXITCODE -ne 0) { Write-Host '  linking failed.' -ForegroundColor Red; exit 1 }

# -------------------------------------------------------------- autostart
Write-Host ''
$auto = Read-Host '  Start the agent automatically when you log in? (y/N)'
if ($auto -match '^[Yy]') {
    try {
        $action  = New-ScheduledTaskAction -Execute $Exe -Argument 'agent'
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        Register-ScheduledTask -TaskName 'jobauto-agent' -Action $action `
            -Trigger $trigger -Force -Description 'jobauto cloud sync agent' | Out-Null
        Write-Host '  registered. It will start at every login.' -ForegroundColor Green
    } catch {
        Write-Host "  could not register the task: $_" -ForegroundColor Yellow
    }
}

Write-Host ''
Write-Host '  DONE' -ForegroundColor Green
Write-Host ''
Write-Host '  Next, sign in to each job portal once (a browser opens; do it by hand):'
Write-Host "    $Exe login" -ForegroundColor Cyan
Write-Host ''
Write-Host '  Then start syncing:'
Write-Host "    $Exe agent" -ForegroundColor Cyan
Write-Host ''
Write-Host '  Everything lives in ~/.jobauto -- delete that folder to remove it all.' -ForegroundColor DarkGray
Write-Host ''
"""
