$ErrorActionPreference = 'Stop'
$TargetPath = Join-Path $env:LOCALAPPDATA 'Programs\AutoPPT'
$TargetExe = Join-Path $TargetPath 'AutoPPT.exe'

# 只停止当前用户安装目录中的程序，保留其他同名进程。
Get-Process -Name 'AutoPPT' -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        if ([System.IO.Path]::GetFullPath($_.Path) -eq $TargetExe) {
            Stop-Process -Id $_.Id -Force
        }
    } catch { }
}

$DesktopShortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'AutoPPT.lnk'
$StartMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'AutoPPT'
if (Test-Path -LiteralPath $DesktopShortcut) {
    Remove-Item -LiteralPath $DesktopShortcut -Force
}
if (Test-Path -LiteralPath $StartMenu) {
    Remove-Item -LiteralPath $StartMenu -Recurse -Force
}
Remove-Item -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoPPT' -Recurse -Force -ErrorAction SilentlyContinue

# 工作记录保存在本地数据目录，卸载程序时保留，方便重新安装后继续使用模板配置。
Set-Location -LiteralPath $env:TEMP
if (Test-Path -LiteralPath $TargetPath) {
    Remove-Item -LiteralPath $TargetPath -Recurse -Force
}
Add-Type -AssemblyName PresentationFramework
[System.Windows.MessageBox]::Show('AutoPPT已卸载。用户数据保留在本地AutoPPT目录。', 'AutoPPT') | Out-Null
