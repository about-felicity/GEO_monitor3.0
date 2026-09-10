$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$WorkerTask = "GEO Enterprise Windows Worker"
$HealthPath = Join-Path $ProjectRoot "runtime\health\latest.json"
$ConnectionPath = Join-Path $ProjectRoot "runtime\health\worker_connection.json"
$WatchdogPath = Join-Path $ProjectRoot "runtime\health\watchdog.json"
$LogDir = Join-Path $ProjectRoot "runtime\logs"
$WorkerScript = Join-Path $ProjectRoot "scripts\run_windows_enterprise_worker.ps1"
New-Item -ItemType Directory -Force -Path (Split-Path $WatchdogPath), $LogDir | Out-Null

$now = Get-Date
$task = Get-ScheduledTask -TaskName $WorkerTask -ErrorAction SilentlyContinue
$worker = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq "python.exe" -and
    $_.CommandLine -like "*-m windows_worker_sdk*" -and
    $_.CommandLine -like "*$ProjectRoot*"
} | Select-Object -First 1

$healthAge = [double]::PositiveInfinity
if (Test-Path -LiteralPath $HealthPath) {
    $healthAge = ($now - (Get-Item -LiteralPath $HealthPath).LastWriteTime).TotalSeconds
}
$previousFailures = 0
if (Test-Path -LiteralPath $WatchdogPath) {
    try { $previousFailures = [int]((Get-Content -Raw -LiteralPath $WatchdogPath | ConvertFrom-Json).stale_checks) } catch { $previousFailures = 0 }
}
$connectionAge = [double]::PositiveInfinity
$connectionOk = $false
if (Test-Path -LiteralPath $ConnectionPath) {
    try {
        $connection = Get-Content -Raw -LiteralPath $ConnectionPath | ConvertFrom-Json
        $connectionAge = ($now - (Get-Item -LiteralPath $ConnectionPath).LastWriteTime).TotalSeconds
        $connectionOk = [bool]$connection.connected
    } catch {
        $connectionOk = $false
    }
}
$connectionStale = $connectionAge -gt 90 -or -not $connectionOk
$staleChecks = if ($healthAge -gt 90 -or $connectionStale) { $previousFailures + 1 } else { 0 }
# The scheduled worker action uses wscript.exe and intentionally returns after
# spawning a fully hidden process. Its task state is therefore normally Ready;
# process + health freshness are the authoritative liveness signals.
$mustRestart = -not $task -or -not $worker -or $staleChecks -ge 2
$action = "healthy"

if ($mustRestart -and $task) {
    $action = "worker_restarted"
    Stop-ScheduledTask -TaskName $WorkerTask -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -eq "python.exe" -and $_.CommandLine -like "*-m windows_worker_sdk*" -and $_.CommandLine -like "*$ProjectRoot*") -or
        ($_.Name -eq "powershell.exe" -and $_.CommandLine -like "*$WorkerScript*")
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-ScheduledTask -TaskName $WorkerTask
    $staleChecks = 0
}

# Keep the Quark-hosted Qwen collector on the actual /quarkchat page. Merely
# having quark.exe running is insufficient: the standalone Qianwen homepage
# cannot be used by this collector and must never receive enterprise leases.
$quarkRoot = Join-Path (Split-Path -Parent $ProjectRoot) "kuake"
$quarkExtension = Join-Path $quarkRoot "extension"
$quarkExe = "C:\Program Files\Quark\quark.exe"
$quarkUrl = "https://www.qianwen.com/quarkchat?entry=homepage&entry_l2=bar_switch_active"
$quarkProcess = Get-Process -Name "quark" -ErrorAction SilentlyContinue | Select-Object -First 1
$quarkHealth = $null
try { $quarkHealth = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 3 } catch {}
$expectedQuarkRevision = "20260905-quark-burst-v1"
$quarkRevisionMismatch = [bool](
    $quarkHealth -and $quarkHealth.extension_ready -and
    [string]$quarkHealth.extension_runtime_revision -ne $expectedQuarkRevision
)
$quarkReady = [bool](
    $quarkHealth -and $quarkHealth.extension_ready -and
    $quarkHealth.extension_collection_ready -and -not $quarkRevisionMismatch
)
$quarkAction = "healthy"
if (-not $quarkProcess -and (Test-Path -LiteralPath $quarkExe)) {
    # The collector is an interactive browser integration and must render its
    # page in the signed-in desktop session.
    Start-Process -FilePath $quarkExe -WindowStyle Normal -ArgumentList @(
        "--remote-debugging-port=9223",
        "--load-extension=$quarkExtension",
        $quarkUrl
    )
    $quarkAction = "browser_started"
} elseif ($quarkRevisionMismatch) {
    $reloadScript = Join-Path $ProjectRoot "scripts\reload_quark_extension.py"
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and (Test-Path -LiteralPath $reloadScript)) {
        & $python.Source -X utf8 $reloadScript | Out-Null
        $quarkAction = "extension_runtime_reloaded"
    } else {
        $quarkAction = "extension_runtime_stale"
    }
} elseif (-not $quarkReady -and (Test-Path -LiteralPath $quarkExe)) {
    Start-Process -FilePath $quarkExe -WindowStyle Normal -ArgumentList @($quarkUrl)
    $quarkAction = "collection_page_opened"
}

$os = Get-CimInstance Win32_OperatingSystem
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
$state = [ordered]@{
    checked_at = $now.ToString("o")
    action = $action
    task_state = if ($task) { [string]$task.State } else { "missing" }
    worker_process = [bool]$worker
    health_age_seconds = if ([double]::IsPositiveInfinity($healthAge)) { -1 } else { [math]::Round($healthAge, 1) }
    server_connection = $connectionOk
    connection_age_seconds = if ([double]::IsPositiveInfinity($connectionAge)) { -1 } else { [math]::Round($connectionAge, 1) }
    stale_checks = $staleChecks
    quark_process = [bool]$quarkProcess
    quark_ready = $quarkReady
    quark_extension_version = if ($quarkHealth) { [string]$quarkHealth.extension_version } else { "" }
    quark_extension_runtime_revision = if ($quarkHealth) { [string]$quarkHealth.extension_runtime_revision } else { "" }
    quark_action = $quarkAction
    memory_load = [math]::Round((1 - $os.FreePhysicalMemory / $os.TotalVisibleMemorySize) * 100, 1)
    available_memory_mb = [math]::Round($os.FreePhysicalMemory / 1024)
    system_disk_free_gb = [math]::Round($disk.FreeSpace / 1GB, 1)
}
$temporary = "$WatchdogPath.tmp"
$state | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
Move-Item -LiteralPath $temporary -Destination $WatchdogPath -Force
if ($action -ne "healthy" -or $quarkAction -ne "healthy") {
    ($state | ConvertTo-Json -Compress) | Add-Content -LiteralPath (Join-Path $LogDir ("watchdog-" + $now.ToString("yyyyMMdd") + ".jsonl")) -Encoding UTF8
}
