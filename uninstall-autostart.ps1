<#
    uninstall-autostart.ps1 -- remove unstream-proxy Windows scheduled task.
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'UnstreamProxy'
)

$ErrorActionPreference = 'Stop'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "[ ok  ] no task named '$TaskName' exists." -ForegroundColor Green
    exit 0
}

try {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[ ok  ] stopped and removed scheduled task '$TaskName'" -ForegroundColor Green
} catch {
    Write-Host "[fail ] failed to remove task: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
