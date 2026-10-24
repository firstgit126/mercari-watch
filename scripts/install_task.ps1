# Register mercari-watch as a Windows Scheduled Task (runs at logon, no console window).
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -DryRun
param(
    [string]$TaskName = "MercariWatch",
    [string]$Python = "",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# Project root = parent of this script's folder
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root "mercari_watch"))) {
    throw "Cannot find mercari_watch package under $Root"
}

if (-not $Python) {
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($found) { $Python = $found.Source }
}
if (-not $Python -or -not (Test-Path $Python)) {
    throw "python.exe not found. Pass -Python C:\Path\To\python.exe"
}

# Prefer pythonw.exe so no console window flashes on logon
$PythonDir = Split-Path -Parent $Python
$PythonW = Join-Path $PythonDir "pythonw.exe"
$Exe = if (Test-Path $PythonW) { $PythonW } else { $Python }

$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

Write-Host "Task name   : $TaskName"
Write-Host "Executable  : $Exe"
Write-Host "Working dir : $Root"
Write-Host "Command     : -m mercari_watch run"

if ($DryRun) {
    Write-Host ""
    Write-Host "DryRun: nothing was changed."
    if ($Existing) { Write-Host "A task with this name already exists and would be replaced." }
    return
}

# Refuse to double-register while the old one is running
if ($Existing -and $Existing.State -eq "Running") {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

$Action = New-ScheduledTaskAction -Execute $Exe -Argument "-m mercari_watch run" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn

$settingsArgs = @{
    AllowStartIfOnBatteries     = $true
    DontStopIfGoingOnBatteries  = $true
    StartWhenAvailable          = $true
    RestartCount                = 3
    RestartInterval             = (New-TimeSpan -Minutes 1)
    ExecutionTimeLimit          = (New-TimeSpan -Seconds 0)
}
try {
    $Settings = New-ScheduledTaskSettingsSet @settingsArgs -MultipleInstances IgnoreNew
} catch {
    $Settings = New-ScheduledTaskSettingsSet @settingsArgs
}

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Mercari keyword watcher (mercari-watch)" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$Info = Get-ScheduledTask -TaskName $TaskName
Write-Host ""
Write-Host "Installed. State: $($Info.State)"
Write-Host "Log file: $Root\data\watch.log"
Write-Host ""
Write-Host "Inspect : Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "Remove  : powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1"
