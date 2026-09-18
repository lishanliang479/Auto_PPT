# 优先使用项目虚拟环境，其次使用已安装的本地依赖运行时。
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPythonPaths = @(
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
)
$taskPythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($taskPythonCommand) { $taskPythonPaths += $taskPythonCommand.Source }
foreach ($taskPythonPath in $taskPythonPaths) {
    if (Test-Path -LiteralPath $taskPythonPath) {
        & $taskPythonPath -c 'import lxml, PIL' 2>$null
        if ($LASTEXITCODE -eq 0) {
            & $taskPythonPath app.py
            exit $LASTEXITCODE
        }
    }
}
Write-Host '缺少Python依赖，请按README中的安装步骤运行后重试。'
Read-Host '按回车退出'
exit 1
