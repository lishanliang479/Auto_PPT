# 只停止监听AutoPPT默认端口且由app.py启动的Python进程。
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$taskPort = 8765
$taskPattern = '^\s*TCP\s+127\.0\.0\.1:' + [regex]::Escape([string]$taskPort) + '\s+\S+\s+LISTENING\s+(\d+)\s*$'
$taskProcessIds = @(
    foreach ($taskLine in (& netstat -ano -p TCP)) {
        $taskMatch = [regex]::Match($taskLine, $taskPattern)
        if ($taskMatch.Success) { [int]$taskMatch.Groups[1].Value }
    }
) | Sort-Object -Unique
if ($taskProcessIds.Count -eq 0) {
    Write-Host 'AutoPPT当前没有运行。'
    Start-Sleep -Milliseconds 1200
    exit 0
}

# 先核对本地接口特征，再停止对应端口进程，避免关闭其他程序。
try {
    $taskConfig = Invoke-RestMethod -Uri "http://127.0.0.1:$taskPort/api/config" -TimeoutSec 2
} catch {
    Write-Host '8765端口由其他程序占用，未执行停止操作。'
    exit 1
}
if (-not $taskConfig.token -or -not $taskConfig.roles.cover -or -not $taskConfig.roles.guest) {
    Write-Host '8765端口由其他程序占用，未执行停止操作。'
    exit 1
}

$taskStopped = 0
foreach ($taskProcessId in $taskProcessIds) {
    $taskProcess = Get-Process -Id $taskProcessId -ErrorAction SilentlyContinue
    if (-not $taskProcess) { continue }
    if ($taskProcess.ProcessName -notmatch '^pythonw?$') {
        continue
    }
    Stop-Process -Id $taskProcessId -Force
    $taskStopped++
}

if ($taskStopped -eq 0) {
    Write-Host '8765端口由其他程序占用，未执行停止操作。'
    exit 1
}

Write-Host 'AutoPPT已停止。'
Start-Sleep -Milliseconds 1200
