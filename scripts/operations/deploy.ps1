$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dashboardRoot = Join-Path $projectRoot "yuanbao_monitor\dashboard"
Set-Location -LiteralPath $projectRoot

function Resolve-CommandPath([string]$Name, [string[]]$Fallbacks = @()) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    foreach ($candidate in $Fallbacks) {
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    return $null
}

function Install-WithWinget([string]$Id, [string]$Label) {
    $winget = Resolve-CommandPath "winget.exe"
    if (-not $winget) { throw "$Label is missing and winget is unavailable." }
    & $winget install --id $Id --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "$Label installation failed." }
}

Write-Host "[1/5] Checking Python..."
$python = Resolve-CommandPath "python.exe" @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
)
if (-not $python) {
    Install-WithWinget "Python.Python.3.12" "Python 3.12"
    $python = Resolve-CommandPath "python.exe" @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    )
}
if (-not $python) { throw "Python is unavailable. Reopen this installer." }

Write-Host "[2/5] Installing browser collector dependencies..."
& $python -m pip install -r (Join-Path $projectRoot "requirements\web.txt")
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }

Write-Host "[3/5] Checking Chrome..."
$chromeCandidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
if (-not ($chromeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1)) {
    Install-WithWinget "Google.Chrome" "Google Chrome"
}

Write-Host "[4/5] Checking dashboard dependencies..."
$node = Resolve-CommandPath "node.exe"
$npm = Resolve-CommandPath "npm.cmd"
if (-not $node -or -not $npm) {
    Install-WithWinget "OpenJS.NodeJS.LTS" "Node.js LTS"
    $env:Path = (Join-Path $env:ProgramFiles "nodejs") + ";" + $env:Path
    $npm = Resolve-CommandPath "npm.cmd"
}
if (-not $npm) { throw "npm is unavailable. Reopen this installer." }
if (-not (Test-Path -LiteralPath (Join-Path $dashboardRoot "node_modules"))) {
    & $npm install --prefix $dashboardRoot --registry https://registry.npmmirror.com --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "Dashboard dependency installation failed." }
}

Write-Host "[5/5] Opening model login pages and dashboard..."
& $python -m web_collectors.login --model all
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_unified_monitor.ps1")
exit $LASTEXITCODE
