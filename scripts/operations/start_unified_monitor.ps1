param([switch]$NoOpen)

$ErrorActionPreference = "Stop"

# Some launchers can pass both Path and PATH. Windows PowerShell's
# Start-Process treats them as duplicate keys, so normalize them first.
$processPath = [Environment]::GetEnvironmentVariable("PATH", "Process")
[Environment]::SetEnvironmentVariable("Path", $null, "Process")
[Environment]::SetEnvironmentVariable("PATH", $processPath, "Process")

# Local dashboard traffic must never be sent through a user/system HTTP proxy.
$localNoProxy = "127.0.0.1,localhost,::1"
$existingNoProxy = [Environment]::GetEnvironmentVariable("NO_PROXY", "Process")
if ($existingNoProxy) { $localNoProxy = "$localNoProxy,$existingNoProxy" }
[Environment]::SetEnvironmentVariable("NO_PROXY", $localNoProxy, "Process")
[Environment]::SetEnvironmentVariable("no_proxy", $localNoProxy, "Process")

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dashboardRoot = Join-Path $projectRoot "yuanbao_monitor\dashboard"
$runtimeRoot = Join-Path $projectRoot "runtime\unified_control"
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

# Conservative defaults for a 16 GB workstation. These preserve full source
# bodies while preventing HTTP threads and hidden Chrome fallbacks from
# multiplying after a service restart. Users can still override them before
# launching the script.
if (-not [Environment]::GetEnvironmentVariable("DOUBAO_CONTENT_WORKERS", "Process")) {
    [Environment]::SetEnvironmentVariable("DOUBAO_CONTENT_WORKERS", "3", "Process")
}
if (-not [Environment]::GetEnvironmentVariable("DOUBAO_CONTENT_DYNAMIC_WORKERS", "Process")) {
    [Environment]::SetEnvironmentVariable("DOUBAO_CONTENT_DYNAMIC_WORKERS", "1", "Process")
}
if (-not [Environment]::GetEnvironmentVariable("DOUBAO_CONTENT_BATCH", "Process")) {
    [Environment]::SetEnvironmentVariable("DOUBAO_CONTENT_BATCH", "24", "Process")
}
if (-not [Environment]::GetEnvironmentVariable("DOUBAO_CONTENT_INDEX_PUBLISH_INTERVAL", "Process")) {
    [Environment]::SetEnvironmentVariable("DOUBAO_CONTENT_INDEX_PUBLISH_INTERVAL", "20", "Process")
}
if (-not [Environment]::GetEnvironmentVariable("MONITOR_ANALYTICS_MEMORY_CACHE_MAX", "Process")) {
    [Environment]::SetEnvironmentVariable("MONITOR_ANALYTICS_MEMORY_CACHE_MAX", "48", "Process")
}
[Environment]::SetEnvironmentVariable("MONITOR_RETAIN_GLOBAL_SNAPSHOT", "0", "Process")

function Test-ListeningPort([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $client.ConnectAsync("127.0.0.1", $Port)
        return $connection.Wait(700) -and $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Start-MonitorPostgres {
    if (Test-ListeningPort 5432) { return }
    try { Start-Service -Name "postgresql-x64-17" -ErrorAction Stop } catch { }
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline -and -not (Test-ListeningPort 5432)) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-ListeningPort 5432) { return }
    $pgCtl = "C:\Program Files\PostgreSQL\17\bin\pg_ctl.exe"
    $pgData = "C:\Program Files\PostgreSQL\17\data"
    if ((Test-Path $pgCtl) -and (Test-Path $pgData)) {
        $pgLog = Join-Path $runtimeRoot "postgres_manual.log"
        & $pgCtl start -D $pgData -l $pgLog -w | Out-Null
    }
    if (-not (Test-ListeningPort 5432)) {
        throw "PostgreSQL failed to start. Check runtime\postgres_manual.log."
    }
}

function Test-PythonWorkerRunning([string]$ScriptName) {
    return $null -ne (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -like "python*" -and $_.CommandLine -match [regex]::Escape($ScriptName)
    } | Select-Object -First 1)
}

function Start-AnalysisWorker([string]$ScriptName, [string]$LogPrefix) {
    if (-not (Test-PythonWorkerRunning $ScriptName)) {
        Start-Process -FilePath "python" -ArgumentList @("-u", (Join-Path $projectRoot $ScriptName)) `
            -WorkingDirectory $projectRoot `
            -RedirectStandardOutput (Join-Path $runtimeRoot ($LogPrefix + ".out.log")) `
            -RedirectStandardError (Join-Path $runtimeRoot ($LogPrefix + ".err.log")) `
            -WindowStyle Hidden
    }
}

$databaseUrl = [Environment]::GetEnvironmentVariable("MONITOR_DATABASE_URL", "User")
if ($databaseUrl) { Start-MonitorPostgres }

if (-not (Test-ListeningPort 8765)) {
    $backendOut = Join-Path $runtimeRoot "dashboard_server.out.log"
    $backendErr = Join-Path $runtimeRoot "dashboard_server.err.log"
    Start-Process -FilePath "python" -ArgumentList @("-u", (Join-Path $projectRoot "doubao_dashboard_server.py")) `
        -WorkingDirectory $projectRoot -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr -WindowStyle Hidden
}

if (-not (Test-ListeningPort 3000)) {
    $frontendCli = Join-Path $dashboardRoot "node_modules\.bin\vinext.cmd"
    if (-not (Test-Path $frontendCli)) {
        & npm.cmd install --prefix $dashboardRoot --registry https://registry.npmmirror.com --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "React dashboard dependency installation failed." }
    }
    # The React panel reads live analytics from port 8765. The legacy static
    # dashboard.json export is not consumed here, so rebuilding it on every
    # launch only adds CPU and forces an otherwise unnecessary frontend build.
    $buildMarker = Join-Path $dashboardRoot "dist\server\index.js"
    $sourcePaths = @(
        (Join-Path $dashboardRoot "app"),
        (Join-Path $dashboardRoot "public"),
        (Join-Path $dashboardRoot "vite.config.ts"),
        (Join-Path $dashboardRoot "next.config.ts"),
        (Join-Path $dashboardRoot "package.json")
    )
    $legacyDashboardData = Join-Path $dashboardRoot "public\data\dashboard.json"
    $latestSource = Get-ChildItem -LiteralPath $sourcePaths -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne $legacyDashboardData } |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if (
        -not (Test-Path -LiteralPath $buildMarker) -or
        ($latestSource -and $latestSource.LastWriteTimeUtc -gt (Get-Item -LiteralPath $buildMarker).LastWriteTimeUtc)
    ) {
        & npm.cmd run build --prefix $dashboardRoot
        if ($LASTEXITCODE -ne 0) { throw "React dashboard production build failed." }
    }
    $frontendOut = Join-Path $runtimeRoot "react_dashboard.out.log"
    $frontendErr = Join-Path $runtimeRoot "react_dashboard.err.log"
    $frontendProcess = Start-Process -FilePath "cmd.exe" `
        -ArgumentList @("/d", "/c", "npm.cmd run start -- -H 0.0.0.0 -p 3000") `
        -WorkingDirectory $dashboardRoot -RedirectStandardOutput $frontendOut `
        -RedirectStandardError $frontendErr -WindowStyle Hidden -PassThru
}

$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    if (
        (Test-ListeningPort 8765) -and
        (Test-ListeningPort 3000)
    ) { break }
    if ($frontendProcess -and $frontendProcess.HasExited) {
        throw "React dashboard exited during startup. Check runtime\unified_control\react_dashboard.err.log."
    }
    Start-Sleep -Milliseconds 500
}


$requiredPorts = @(8765, 3000)
$missingPorts = @($requiredPorts | Where-Object { -not (Test-ListeningPort $_) })
if ($missingPorts.Count -gt 0) {
    throw "Unified services startup timed out. Missing ports: $($missingPorts -join ', '). Check runtime\unified_control logs."
}

# A listening production process is not healthy when its hashed client bundle
# cannot be served. This catches the Windows path-separator regression that
# otherwise leaves the dashboard permanently showing `读取中` and zeroes.
$dashboardHtml = (Invoke-WebRequest -Uri "http://127.0.0.1:3000/" -TimeoutSec 10).Content
$assetMatch = [regex]::Match($dashboardHtml, 'href="(/assets/index-[^"]+\.css)"')
if (-not $assetMatch.Success) {
    throw "React dashboard did not expose a hashed client asset."
}
$assetResponse = Invoke-WebRequest -Uri ("http://127.0.0.1:3000" + $assetMatch.Groups[1].Value) -Method Head -TimeoutSec 10
if ($assetResponse.StatusCode -ne 200) {
    throw "React dashboard client assets are unavailable."
}
# PostgreSQL is optional. When configured, completed web rounds are mirrored
# into it and this worker enriches recommendation answers for every model.
if ($databaseUrl) { Start-AnalysisWorker "product_ai_worker.py" "product_ai_worker" }
if (-not $NoOpen) { Start-Process "http://127.0.0.1:3000" }
Write-Host "Five-model web collection dashboard is ready: http://127.0.0.1:3000"
