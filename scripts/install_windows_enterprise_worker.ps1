$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Runner = Join-Path $ProjectRoot "scripts\run_windows_enterprise_worker.ps1"
$TokenFile = Join-Path $ProjectRoot "config\worker_token.secret"
if (-not (Test-Path -LiteralPath $TokenFile)) { throw "Create config\worker_token.secret before installation." }

$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "GEO Enterprise Windows Worker" -Action $Action -Trigger $Trigger -Settings $Settings -Description "Local four-model GEO diagnostic worker" -Force | Out-Null
Start-ScheduledTask -TaskName "GEO Enterprise Windows Worker"
Write-Output "GEO Enterprise Windows Worker installed and started."
