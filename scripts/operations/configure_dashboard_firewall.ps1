﻿﻿﻿$ErrorActionPreference = "Stop"

$logPath = Join-Path $env:TEMP "configure_dashboard_firewall.log"
Start-Transcript -Path $logPath -Force | Out-Null

# 统一监控面板局域网访问防火墙规则
# 开放端口:
#   3000 - React 前端面板
#   8765 - 本地数据 API (doubao_dashboard_server.py)

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
Write-Host "IsAdmin=$isAdmin User=$($identity.Name)"
if (-not $isAdmin) {
    throw "需要管理员权限运行此脚本。请右键 -> 以管理员身份运行。"
}

$rules = @()
$rules += @{ Name = "Doubao Dashboard Frontend 3000"; Port = 3000; Protocol = "TCP" }
$rules += @{ Name = "Doubao Dashboard API 8765"; Port = 8765; Protocol = "TCP" }

# 允许哪个网络配置文件:Private 仅家庭/工作网络,Any 包含公用网络
$profile = "Any"

foreach ($rule in $rules) {
    $existing = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if (-not $existing) {
        New-NetFirewallRule `
            -DisplayName $rule.Name `
            -Direction Inbound `
            -Action Allow `
            -Protocol $rule.Protocol `
            -LocalPort $rule.Port `
            -Profile $profile `
            -RemoteAddress LocalSubnet | Out-Null
    } else {
        Set-NetFirewallRule `
            -DisplayName $rule.Name `
            -Enabled True `
            -Direction Inbound `
            -Action Allow `
            -Profile $profile | Out-Null
        $existing | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter `
            -RemoteAddress LocalSubnet | Out-Null
        $existing | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter `
            -Protocol $rule.Protocol `
            -LocalPort $rule.Port | Out-Null
    }
    Write-Host ("已开放入站 {0} {1} ({2}) - Profile:{3}" -f $rule.Protocol, $rule.Port, $rule.Name, $profile) -ForegroundColor Green
}

Write-Host "防火墙规则创建完成,验证:"
Get-NetFirewallRule -DisplayName "Doubao*" -ErrorAction SilentlyContinue | ForEach-Object {
    $pf = $_ | Get-NetFirewallPortFilter
    Write-Host ("  - {0} | Port={1} | Enabled={2}" -f $_.DisplayName, $pf.LocalPort, $_.Enabled)
}

Stop-Transcript | Out-Null

Write-Host ""
Write-Host "本机局域网 IP 地址:" -ForegroundColor Cyan
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    ForEach-Object {
        Write-Host ("  {0,-20} {1}" -f $_.IPAddress, $_.InterfaceAlias) -ForegroundColor Yellow
    }

Write-Host ""
Write-Host "局域网其他设备访问方式:" -ForegroundColor Cyan
Write-Host "  面板:  http://<本机IP>:3000" -ForegroundColor White
Write-Host "  API:   http://<本机IP>:8765" -ForegroundColor White
Write-Host ""
Write-Host "提示: 若仍无法访问,检查:" -ForegroundColor Cyan
Write-Host "  1. 当前网络配置文件是否为 '专用 (Private)'"
Write-Host "  2. 路由器是否开启了 AP 隔离 / 客户端隔离"
Write-Host "  3. 杀毒软件是否拦截了 Python / Node 入站连接"
