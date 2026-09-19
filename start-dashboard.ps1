<#
    Starts the jobauto dashboard and exposes it through a free Cloudflare
    Tunnel.

    The engine stays on this machine -- Cloudflare only forwards traffic to
    the local port. Your portal session cookies never leave this PC.

    Usage:
        .\start-dashboard.ps1              # local + public tunnel
        .\start-dashboard.ps1 -LocalOnly   # no tunnel, localhost only
        .\start-dashboard.ps1 -Port 5080

    Stop everything with Ctrl-C.
#>
param(
    [int]$Port = 5057,
    [switch]$LocalOnly,
    [switch]$NewToken
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# --------------------------------------------------------------- token
# Persisted so your URL keeps working across restarts. Lives under data/,
# which is gitignored.
$tokenFile = Join-Path $root 'data\web_token.txt'
New-Item -ItemType Directory -Force -Path (Split-Path $tokenFile) | Out-Null

if ($NewToken -or -not (Test-Path $tokenFile)) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = ([System.BitConverter]::ToString($bytes) -replace '-', '').ToLower()
    Set-Content -Path $tokenFile -Value $token -Encoding utf8 -NoNewline
    Write-Host "  generated a new access token" -ForegroundColor Yellow
}
$token = (Get-Content $tokenFile -Raw).Trim()

$env:JOBAUTO_WEB_TOKEN = $token
$env:PYTHONPATH = Join-Path $root 'src'

# ----------------------------------------------------------- dashboard
Write-Host ""
Write-Host "  starting dashboard on port $Port ..." -ForegroundColor Cyan

$bind = if ($LocalOnly) { '127.0.0.1' } else { '127.0.0.1' }
$web = Start-Process -FilePath 'python' `
    -ArgumentList '-m', 'jobauto', 'web', '--host', $bind, '--port', "$Port" `
    -PassThru -NoNewWindow

$bound = $false
foreach ($i in 1..10) {
    Start-Sleep -Seconds 1
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -TimeoutSec 3 `
                          -UseBasicParsing -ErrorAction Stop | Out-Null
        $bound = $true; break
    } catch {
        # A 401 means it IS listening and token auth is working.
        if ($_.Exception.Response.StatusCode.value__ -eq 401) { $bound = $true; break }
    }
}

if (-not $bound) {
    Write-Host ""
    Write-Host "  Dashboard did not come up on port $Port." -ForegroundColor Red
    Write-Host "  Windows reserves some ports (5000 is a common one -- check with:" -ForegroundColor DarkGray
    Write-Host "    netsh interface ipv4 show excludedportrange protocol=tcp" -ForegroundColor DarkGray
    Write-Host "  Try another:  .\start-dashboard.ps1 -Port 5080" -ForegroundColor DarkGray
    Stop-Process -Id $web.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

$localUrl = "http://127.0.0.1:$Port/?token=$token"

if ($LocalOnly) {
    Write-Host ""
    Write-Host "  READY (local only)" -ForegroundColor Green
    Write-Host "  $localUrl"
    Write-Host ""
    Write-Host "  Ctrl-C to stop."
    try { Wait-Process -Id $web.Id } finally { Stop-Process -Id $web.Id -Force -ErrorAction SilentlyContinue }
    exit 0
}

# -------------------------------------------------------------- tunnel
$cf = Join-Path $root 'tools\cloudflared.exe'
if (-not (Test-Path $cf)) {
    Write-Host "  tools\cloudflared.exe is missing. Download it with:" -ForegroundColor Red
    Write-Host "    curl -L -o tools\cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Stop-Process -Id $web.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "  opening Cloudflare tunnel ..." -ForegroundColor Cyan
$log = Join-Path $root 'data\tunnel.log'
if (Test-Path $log) { Remove-Item $log -Force }

$tunnel = Start-Process -FilePath $cf `
    -ArgumentList 'tunnel', '--no-autoupdate', '--url', "http://127.0.0.1:$Port" `
    -PassThru -NoNewWindow -RedirectStandardError $log -RedirectStandardOutput "$log.out"

# cloudflared prints the assigned hostname a second or two after startup.
$publicUrl = $null
foreach ($i in 1..30) {
    Start-Sleep -Seconds 1
    if (Test-Path $log) {
        $match = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' `
                               -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($match) {
            $publicUrl = $match.Matches[0].Value
            break
        }
    }
}

Write-Host ""
if ($publicUrl) {
    Write-Host "  READY" -ForegroundColor Green
    Write-Host ""
    Write-Host "  public:  $publicUrl/?token=$token"
    Write-Host "  local:   $localUrl"
    Write-Host ""
    Write-Host "  The public URL changes each restart; the token does not." -ForegroundColor DarkGray
    Write-Host "  Anyone with BOTH the URL and token can see your job data." -ForegroundColor DarkGray
} else {
    Write-Host "  Tunnel did not report a URL. Check data\tunnel.log" -ForegroundColor Yellow
    Write-Host "  local:   $localUrl"
}

Write-Host ""
Write-Host "  Ctrl-C to stop both."

try {
    Wait-Process -Id $web.Id
} finally {
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $web.Id -Force -ErrorAction SilentlyContinue
    Write-Host "  stopped."
}
