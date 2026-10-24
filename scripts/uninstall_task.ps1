# Remove the mercari-watch scheduled task.
# Usage: powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1
param(
    [string]$TaskName = "MercariWatch"
)

$ErrorActionPreference = "Stop"

$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Existing) {
    Write-Host "Task '$TaskName' is not installed. Nothing to do."
    return
}

if ($Existing.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Task '$TaskName' removed."
