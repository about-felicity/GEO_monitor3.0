$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$TokenFile = Join-Path $ProjectRoot "config\worker_token.secret"
$DeepSeekFile = Join-Path (Split-Path -Parent $ProjectRoot) "ds_apikey.txt"

if (-not (Test-Path -LiteralPath $TokenFile)) {
    throw "Missing worker token file: $TokenFile"
}
if (-not (Test-Path -LiteralPath $DeepSeekFile)) {
    throw "Missing DeepSeek key file: $DeepSeekFile"
}

$env:GEO_SERVER_URL = if ($env:GEO_SERVER_URL) { $env:GEO_SERVER_URL } else { "https://www.ifbcy.com/geo" }
$env:GEO_WORKER_TOKEN = (Get-Content -LiteralPath $TokenFile -Raw -Encoding UTF8).Trim()
$env:GEO_DEEPSEEK_KEY_FILE = $DeepSeekFile
$env:GEO_COLLECTOR_FACTORY = "windows_enterprise_worker.collectors:create_collector"
$env:GEO_ANALYZER_FACTORY = "windows_enterprise_worker.analyzer:create_analyzer"
$env:GEO_WORKER_ID = if ($env:GEO_WORKER_ID) { $env:GEO_WORKER_ID } else { "windows-office-01" }
$env:GEO_MODEL_CONCURRENCY = if ($env:GEO_MODEL_CONCURRENCY) { $env:GEO_MODEL_CONCURRENCY } else { "6" }
$env:GEO_SUBPROCESS_CONCURRENCY = if ($env:GEO_SUBPROCESS_CONCURRENCY) { $env:GEO_SUBPROCESS_CONCURRENCY } else { "4" }
$env:GEO_QUARK_ROUND_ATTEMPTS = if ($env:GEO_QUARK_ROUND_ATTEMPTS) { $env:GEO_QUARK_ROUND_ATTEMPTS } else { "1" }
$env:GEO_RESOURCE_WAIT_SECONDS = if ($env:GEO_RESOURCE_WAIT_SECONDS) { $env:GEO_RESOURCE_WAIT_SECONDS } else { "240" }
$env:DOUBAO_APP_PSS_RESTART_MB = if ($env:DOUBAO_APP_PSS_RESTART_MB) { $env:DOUBAO_APP_PSS_RESTART_MB } else { "1250" }
$env:GEO_ANALYSIS_CONCURRENCY = if ($env:GEO_ANALYSIS_CONCURRENCY) { $env:GEO_ANALYSIS_CONCURRENCY } else { "3" }
$env:GEO_READINESS_TTL = if ($env:GEO_READINESS_TTL) { $env:GEO_READINESS_TTL } else { "45" }
$env:GEO_HEALTH_INTERVAL = if ($env:GEO_HEALTH_INTERVAL) { $env:GEO_HEALTH_INTERVAL } else { "15" }
$env:GEO_AUTO_RECLAIM_BROWSERS = if ($env:GEO_AUTO_RECLAIM_BROWSERS) { $env:GEO_AUTO_RECLAIM_BROWSERS } else { "1" }
# The worker is a production back-channel and must not inherit an optional
# desktop proxy. A stale Windows proxy otherwise leaves the process alive
# while every claim/heartbeat request is silently routed to localhost.
$env:NO_PROXY = "www.ifbcy.com,ifbcy.com,127.0.0.1,localhost"
$env:no_proxy = $env:NO_PROXY
Remove-Item Env:HTTP_PROXY, Env:HTTPS_PROXY, Env:ALL_PROXY, Env:http_proxy, Env:https_proxy, Env:all_proxy -ErrorAction SilentlyContinue
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$DataDir = Join-Path $ProjectRoot "runtime\worker_spool"
$LogDir = Join-Path $ProjectRoot "runtime\logs"
$env:GEO_CONNECTION_HEALTH_PATH = Join-Path $ProjectRoot "runtime\health\worker_connection.json"
New-Item -ItemType Directory -Force -Path $DataDir, $LogDir | Out-Null
# Keep operational logs bounded without touching result spools or customer data.
Get-ChildItem -LiteralPath $LogDir -Filter "worker-*.log" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
    Remove-Item -Force -ErrorAction SilentlyContinue
Set-Location -LiteralPath $ProjectRoot

while ($true) {
    $stamp = Get-Date -Format "yyyyMMdd"
    $log = Join-Path $LogDir "worker-$stamp.log"
    & python -X utf8 -m windows_worker_sdk --poll-seconds 3 --data-dir $DataDir *>> $log
    Start-Sleep -Seconds 10
}
