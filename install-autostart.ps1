<#
    install-autostart.ps1 -- register a Windows scheduled task that starts unstream-proxy at logon.
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'UnstreamProxy',
    [int]$Port = 8181,
    [int]$DelaySeconds = 5,
    [int]$MaxLogKB = 1024,
    [switch]$WhatIf_
)

$ErrorActionPreference = 'Stop'

$root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root 'start-proxy.ps1'

if (-not (Test-Path $script)) {
    Write-Host "[fail ] start-proxy.ps1 not found in $root" -ForegroundColor Red
    exit 1
}

$account = "$env:USERDOMAIN\$env:USERNAME"
$powershell = (Get-Command powershell.exe -ErrorAction SilentlyContinue).Source
if (-not $powershell) { $powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" }

$arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Service -Port {1} -MaxLogKB {2}' -f $script, $Port, $MaxLogKB

Write-Host ''
Write-Host '  Scheduled task to be registered' -ForegroundColor White
Write-Host "  name      : $TaskName"        -ForegroundColor DarkGray
Write-Host "  account   : $account"          -ForegroundColor DarkGray
Write-Host "  trigger   : at logon, +${DelaySeconds}s delay" -ForegroundColor DarkGray
Write-Host "  action    : $powershell"       -ForegroundColor DarkGray
Write-Host "  arguments : $arguments"        -ForegroundColor DarkGray
Write-Host "  workdir   : $root"             -ForegroundColor DarkGray
Write-Host "  listen    : 127.0.0.1:$Port (loopback only)" -ForegroundColor DarkGray
Write-Host ''

if ($WhatIf_) {
    Write-Host '[ ok  ] -WhatIf_ given: nothing was changed.' -ForegroundColor Yellow
    exit 0
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[warn ] a task named '$TaskName' already exists -- replacing it." -ForegroundColor Yellow
}

$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$trigger.Delay = "PT${DelaySeconds}S"
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$settings.Hidden = $true

try {
    Register-ScheduledTask -TaskName $TaskName `
        -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
        -Description 'Local Anthropic SSE compatibility proxy for Claude Code (loopback only).' `
        -Force | Out-Null
} catch {
    Write-Host "[fail ] Register-ScheduledTask failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-Host "[ ok  ] registered scheduled task '$TaskName'" -ForegroundColor Green

try {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host '[ ok  ] task started' -ForegroundColor Green
} catch {
    Write-Host "[warn ] could not start task immediately: $($_.Exception.Message)" -ForegroundColor Yellow
}

$up = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3 -ErrorAction Stop
        if ($h.status -eq 'ok') { $up = $true; break }
    } catch { }
}

Write-Host ''
if ($up) {
    Write-Host "[ ok  ] proxy is answering on http://127.0.0.1:$Port/health" -ForegroundColor Green
} else {
    Write-Host "[warn ] proxy is not answering /health yet. Check logs\proxy.log." -ForegroundColor Yellow
}

Write-Host ''
Write-Host '  Manage it with:' -ForegroundColor White
Write-Host "    Get-ScheduledTask   -TaskName $TaskName"  -ForegroundColor DarkGray
Write-Host "    Stop-ScheduledTask  -TaskName $TaskName   # stop this run"  -ForegroundColor DarkGray
Write-Host "    Disable-ScheduledTask -TaskName $TaskName # keep it down"  -ForegroundColor DarkGray
Write-Host "    .\uninstall-autostart.ps1                 # remove entirely" -ForegroundColor DarkGray
Write-Host ''
