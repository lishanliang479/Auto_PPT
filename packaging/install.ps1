param(
    [string]$TargetPath = "",
    [switch]$NoLaunch,
    [switch]$SkipShortcuts,
    [switch]$SkipRegistry
)

# 本脚本由Windows PowerShell5.1执行（安装器内嵌调用），只使用5.1支持的语法。
$ErrorActionPreference = 'Stop'
if (-not $TargetPath) {
    $TargetPath = Join-Path $env:LOCALAPPDATA 'Programs\AutoPPT'
}
$TargetPath = [System.IO.Path]::GetFullPath($TargetPath)
$ArchivePath = Join-Path $PSScriptRoot 'AutoPPT.zip'
if (-not (Test-Path -LiteralPath $ArchivePath)) {
    throw '安装包缺少AutoPPT.zip。'
}

$TargetParent = Split-Path -Parent $TargetPath
New-Item -ItemType Directory -Path $TargetParent -Force | Out-Null

# Use a random suffix after the pid. A leftover directory from an aborted run is
# never reused, so stale files inside it cannot cause a denied write.

$RunTag = '{0}-{1}' -f $PID, ([guid]::NewGuid().ToString('N').Substring(0, 8))
$InstallTemp = Join-Path $TargetParent ('.AutoPPT-install-' + $RunTag)
$BackupPath = Join-Path $TargetParent ('.AutoPPT-backup-' + $RunTag)
$Separator = [System.IO.Path]::DirectorySeparatorChar
$ManagedRoots = @(
    $TargetPath + $Separator,
    $InstallTemp + $Separator,
    $BackupPath + $Separator
)

function Test-AutoPPTExtracted {
    param([string]$DestinationPath)
    return (Test-Path -LiteralPath (Join-Path $DestinationPath 'AutoPPT.exe'))
}

# Read-only, hidden and system attributes make an overwrite fail with "access denied".
function Clear-RestrictedAttribute {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $Blocked = [System.IO.FileAttributes]::ReadOnly -bor [System.IO.FileAttributes]::Hidden -bor [System.IO.FileAttributes]::System
            if ($_.Attributes -band $Blocked) {
                $_.Attributes = [System.IO.FileAttributes]::Normal
            }
        } catch { }
    }
}

# A failed delete must not abort the install, so swallow it and let the caller decide.
function Remove-LeftoverDirectory {
    param([string]$DirectoryPath)
    try {
        if (Test-Path -LiteralPath $DirectoryPath) {
            Clear-RestrictedAttribute $DirectoryPath
            Remove-Item -LiteralPath $DirectoryPath -Recurse -Force
        }
    } catch { }
}

# Only stop same-named processes living inside the install roots.
function Stop-InstalledApp {
    param([string[]]$Roots)
    Get-Process -Name 'AutoPPT' -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            if (-not $_.Path) { return }
            $ProcessPath = [System.IO.Path]::GetFullPath($_.Path)
            foreach ($Root in $Roots) {
                if ($ProcessPath.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase)) {
                    Stop-Process -Id $_.Id -Force
                    break
                }
            }
        } catch { }
    }
}

# Per-entry fallback: on failure it names the exact file that was denied, which
# tells the user whether a security product is intercepting the write.
function Expand-ArchiveEntryByEntry {
    param([string]$SourceArchive, [string]$DestinationPath, [int]$Attempts = 3)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = [System.IO.Compression.ZipFile]::OpenRead($SourceArchive)
    try {
        foreach ($Entry in $Archive.Entries) {
            $RelativePath = $Entry.FullName.Replace('/', $Separator)
            $DestinationFile = Join-Path $DestinationPath $RelativePath
            if ([string]::IsNullOrEmpty($Entry.Name)) {
                New-Item -ItemType Directory -Path $DestinationFile -Force | Out-Null
                continue
            }
            New-Item -ItemType Directory -Path (Split-Path -Parent $DestinationFile) -Force | Out-Null
            for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {
                try {
                    if (Test-Path -LiteralPath $DestinationFile) {
                        Clear-RestrictedAttribute $DestinationFile
                    }
                    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($Entry, $DestinationFile, $true)
                    break
                } catch {
                    if ($Attempt -ge $Attempts) {
                        throw ('写入 {0} 失败：{1}' -f $Entry.FullName, $_.Exception.Message)
                    }
                    Start-Sleep -Seconds 2
                }
            }
        }
    } finally {
        $Archive.Dispose()
    }
}

# Try a whole-archive extract, then clear attributes and retry, then fall back to
# per-entry extraction so one transient denial does not fail the whole install.
function Expand-AutoPPTArchive {
    param([string]$SourceArchive, [string]$DestinationPath, [int]$Attempts = 3)
    $LastMessage = ''
    for ($Attempt = 1; $Attempt -le $Attempts; $Attempt++) {
        try {
            New-Item -ItemType Directory -Path $DestinationPath -Force | Out-Null
            Expand-Archive -LiteralPath $SourceArchive -DestinationPath $DestinationPath -Force
            if (Test-AutoPPTExtracted $DestinationPath) { return }
            $LastMessage = '解压结果不完整，未找到AutoPPT.exe。'
        } catch {
            $LastMessage = $_.Exception.Message
        }
        Write-Warning ('第 {0} 次解压失败：{1}' -f $Attempt, $LastMessage)
        Clear-RestrictedAttribute $DestinationPath
        Start-Sleep -Seconds 2
    }
    Write-Warning '整包解压多次失败，改用逐文件解压重试。'
    Expand-ArchiveEntryByEntry -SourceArchive $SourceArchive -DestinationPath $DestinationPath
    if (-not (Test-AutoPPTExtracted $DestinationPath)) {
        throw ('解压安装文件失败：{0}' -f $LastMessage)
    }
}

# ==== main flow ====

# Clean up temp directories left by an aborted run. Directories touched within the
# last 6 hours may belong to another running installer window, so leave those alone.

$Leftovers = @(
    Get-ChildItem -LiteralPath $TargetParent -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like '.AutoPPT-install-*' -or $_.Name -like '.AutoPPT-backup-*' } |
        Where-Object { $_.FullName -ne $InstallTemp -and $_.FullName -ne $BackupPath } |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddHours(-6) }
)
foreach ($Leftover in $Leftovers) {
    Remove-LeftoverDirectory $Leftover.FullName
}

# Before updating, only stop the AutoPPT process running from the install directory.
Stop-InstalledApp -Roots $ManagedRoots
Remove-LeftoverDirectory $InstallTemp
Remove-LeftoverDirectory $BackupPath

try {
    Expand-AutoPPTArchive -SourceArchive $ArchivePath -DestinationPath $InstallTemp
} catch {
    $Reason = $_.Exception.Message
    Remove-LeftoverDirectory $InstallTemp
    throw ('解压安装文件失败：{0}
请关闭正在运行的AutoPPT，并在安全软件（360、火绒、Windows Defender等）中放行 {1} 后重试；仍失败时重启电脑再安装。' -f $Reason, $TargetParent)
}
if (-not (Test-AutoPPTExtracted $InstallTemp)) {
    Remove-LeftoverDirectory $InstallTemp
    throw '安装文件校验失败，未找到AutoPPT.exe。'
}

try {
    if (Test-Path -LiteralPath $TargetPath) {
        Move-Item -LiteralPath $TargetPath -Destination $BackupPath
    }
    Move-Item -LiteralPath $InstallTemp -Destination $TargetPath
} catch {
    # The target directory may still be held by the app; stop it, clear attributes, retry once.
    Stop-InstalledApp -Roots $ManagedRoots
    Start-Sleep -Seconds 2
    Clear-RestrictedAttribute $TargetPath
    try {
        if (Test-Path -LiteralPath $TargetPath) {
            Move-Item -LiteralPath $TargetPath -Destination $BackupPath
        }
        Move-Item -LiteralPath $InstallTemp -Destination $TargetPath
    } catch {
        if (-not (Test-Path -LiteralPath $TargetPath) -and (Test-Path -LiteralPath $BackupPath)) {
            Move-Item -LiteralPath $BackupPath -Destination $TargetPath
        }
        throw ('无法替换安装目录 {0}：{1}
请结束所有AutoPPT进程后重试；旧版本文件保留在 {2}。' -f $TargetPath, $_.Exception.Message, $BackupPath)
    }
}
Remove-LeftoverDirectory $BackupPath

# Shortcut and registry failures do not affect the app itself, so warn instead of aborting.
if (-not $SkipShortcuts) {
    try {
        $Shell = New-Object -ComObject WScript.Shell
        $Desktop = [Environment]::GetFolderPath('Desktop')
        $StartMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'AutoPPT'
        New-Item -ItemType Directory -Path $StartMenu -Force | Out-Null
        foreach ($ShortcutPath in @((Join-Path $Desktop 'AutoPPT.lnk'), (Join-Path $StartMenu 'AutoPPT.lnk'))) {
            $Shortcut = $Shell.CreateShortcut($ShortcutPath)
            $Shortcut.TargetPath = Join-Path $TargetPath 'AutoPPT.exe'
            $Shortcut.WorkingDirectory = $TargetPath
            $Shortcut.IconLocation = (Join-Path $TargetPath 'AutoPPT.exe') + ',0'
            $Shortcut.Description = 'AutoPPT会议内容工作台'
            $Shortcut.Save()
        }
    } catch {
        Write-Warning ('创建快捷方式失败，可手动运行 {0}：{1}' -f (Join-Path $TargetPath 'AutoPPT.exe'), $_.Exception.Message)
    }
}

if (-not $SkipRegistry) {
    try {
        $UninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\AutoPPT'
        New-Item -Path $UninstallKey -Force | Out-Null
        Set-ItemProperty -Path $UninstallKey -Name DisplayName -Value 'AutoPPT会议内容工作台'
        Set-ItemProperty -Path $UninstallKey -Name DisplayVersion -Value '1.0.0'
        Set-ItemProperty -Path $UninstallKey -Name Publisher -Value 'Hongma'
        Set-ItemProperty -Path $UninstallKey -Name InstallLocation -Value $TargetPath
        Set-ItemProperty -Path $UninstallKey -Name DisplayIcon -Value (Join-Path $TargetPath 'AutoPPT.exe')
        $UninstallCommand = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $TargetPath 'uninstall.ps1') + '"'
        Set-ItemProperty -Path $UninstallKey -Name UninstallString -Value $UninstallCommand
        Set-ItemProperty -Path $UninstallKey -Name NoModify -Value 1 -Type DWord
        Set-ItemProperty -Path $UninstallKey -Name NoRepair -Value 1 -Type DWord
    } catch {
        Write-Warning ('写入卸载注册表项失败，不影响程序使用：{0}' -f $_.Exception.Message)
    }
}

Write-Host ('AutoPPT安装完成：{0}' -f $TargetPath)
if (-not $NoLaunch) {
    Start-Process -FilePath (Join-Path $TargetPath 'AutoPPT.exe') -WorkingDirectory $TargetPath
}
