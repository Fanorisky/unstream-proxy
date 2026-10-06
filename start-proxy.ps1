<#
    start-proxy.ps1 -- launch unstream-proxy.

    Usage:
      .\start-proxy.ps1                  # interactive, Ctrl+C to stop
      .\start-proxy.ps1 -Port 8181       # different port
      .\start-proxy.ps1 -Service         # supervised, rotating log file
#>

[CmdletBinding()]
param(
    [int]$Port = 8181,
    [switch]$Service,
    [int]$MaxLogKB = 1024
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$script:LogPath   = $null
$script:MaxBytes  = $MaxLogKB * 1KB
$script:LineCount = 0

if ($Service) {
    $logDir = Join-Path $root 'logs'
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
    $script:LogPath = Join-Path $logDir 'proxy.log'
}

function Invoke-LogRotation {
    if (-not $script:LogPath) { return }
    if (-not (Test-Path $script:LogPath)) { return }
    if ((Get-Item $script:LogPath).Length -le $script:MaxBytes) { return }
    $old = "$($script:LogPath).1"
    if (Test-Path $old) { Remove-Item $old -Force -ErrorAction SilentlyContinue }
    Move-Item $script:LogPath $old -Force -ErrorAction SilentlyContinue
}

function Write-Line {
    param([string]$Text, [string]$Color = 'Gray')
    if (-not $script:LogPath) { Write-Host $Text -ForegroundColor $Color; return }
    if (($script:LineCount % 50) -eq 0) { Invoke-LogRotation }
    $script:LineCount++
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    try { Add-Content -LiteralPath $script:LogPath -Value "$stamp $Text" -Encoding utf8 -ErrorAction Stop } catch { }
}

function Write-Step { param([string]$m) Write-Line "[start] $m" 'Cyan' }
function Write-Ok   { param([string]$m) Write-Line "[ ok  ] $m" 'Green' }
function Write-Warn { param([string]$m) Write-Line "[warn ] $m" 'Yellow' }
function Write-Err  { param([string]$m) Write-Line "[fail ] $m" 'Red' }

# 0. Single instance guard
function Test-OurProxy {
    param([int]$OnPort)
    try {
        $probe = Invoke-RestMethod "http://127.0.0.1:$OnPort/health" -TimeoutSec 5 -ErrorAction Stop
        return ($probe.status -eq 'ok')
    } catch { return $false }
}

function Get-PortOwners {
    param([int]$OnPort)
    $conns = $null
    try { $conns = Get-NetTCPConnection -LocalPort $OnPort -State Listen -ErrorAction SilentlyContinue } catch { }
    if (-not $conns) { return @() }
    return @($conns | Select-Object -ExpandProperty OwningProcess -Unique)
}

$owners = Get-PortOwners -OnPort $Port
if ($owners.Count -gt 0) {
    if (Test-OurProxy -OnPort $Port) {
        Write-Ok "proxy already healthy on http://127.0.0.1:$Port (PID $($owners -join ', ')) -- nothing to do"
        exit 0
    }
    Write-Err "Port $Port is occupied by PID $($owners -join ', ')."
    exit 2
}

# 1. Locate Python
Write-Step 'Locating Python interpreter'
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $python) {
    Write-Err 'No Python interpreter found on PATH. Install Python 3.8+ and re-run.'
    exit 1
}
$pyVersion = & $python --version 2>&1
Write-Ok "interpreter: $pyVersion"

# 2. Start Proxy
$proxyScript = Join-Path $root 'Proxy.py'
$env:PORT = "$Port"
$env:HOST = "127.0.0.1"

Write-Line ''
Write-Line '  unstream-proxy (Anthropic Relay Proxy)' 'White'
Write-Line "  listen   : http://127.0.0.1:$Port (loopback only)" 'DarkGray'
Write-Line "  health   : http://127.0.0.1:$Port/health" 'DarkGray'
if ($Service) { Write-Line "  log      : $($script:LogPath) (rotates at $MaxLogKB KB)" 'DarkGray' }
else          { Write-Line '  stop     : Ctrl+C' 'DarkGray' }
Write-Line ''

if ($Service) {
    $backoff       = 5
    $maxBackoff    = 60
    $rapidFailures = 0

    while ($true) {
        $startedAt = Get-Date
        & $python -u $proxyScript 2>&1 | ForEach-Object { Write-Line ([string]$_) }
        $code    = $LASTEXITCODE
        $ranFor  = ((Get-Date) - $startedAt).TotalSeconds
        Write-Warn "Proxy exited with code $code after $([math]::Round($ranFor))s"

        $taken = Get-PortOwners -OnPort $Port
        if ($taken.Count -gt 0) {
            Write-Err "Port $Port was claimed by another process (PID $($taken -join ', ')) while restarting. Stopping."
            exit 2
        }

        if ($ranFor -ge 60) {
            $backoff = 5
            $rapidFailures = 0
        } else {
            $rapidFailures++
        }

        if ($rapidFailures -ge 10) {
            Write-Err "Proxy failed $rapidFailures times in rapid succession -- giving up."
            exit 1
        }

        Write-Warn "restarting in ${backoff}s (consecutive rapid failures: $rapidFailures)"
        Start-Sleep -Seconds $backoff
        $backoff = [Math]::Min($backoff * 2, $maxBackoff)
        Write-Step "restarting proxy on 127.0.0.1:$Port"
    }
}

& $python -u $proxyScript
