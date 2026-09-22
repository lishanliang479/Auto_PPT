$ErrorActionPreference = 'Stop'
$Workspace = Split-Path -Parent $PSScriptRoot
$BuildRoot = Join-Path $Workspace '.build\installer'
$PyInstallerWork = Join-Path $BuildRoot 'pyinstaller'
$DistRoot = Join-Path $BuildRoot 'dist'
$ReleaseRoot = Join-Path $Workspace 'release'
$StageRoot = Join-Path $BuildRoot 'stage'
$TestInstall = Join-Path $BuildRoot 'install-test'
$SetupWork = Join-Path $BuildRoot 'setup-work'
$SetupSpec = Join-Path $BuildRoot 'setup-spec'

# 构建目录固定在项目内部，清理前核对绝对路径，避免误删其他目录。
$ResolvedWorkspace = [System.IO.Path]::GetFullPath($Workspace)
$ResolvedBuild = [System.IO.Path]::GetFullPath($BuildRoot)
if (-not $ResolvedBuild.StartsWith($ResolvedWorkspace + [System.IO.Path]::DirectorySeparatorChar)) {
    throw '构建目录超出项目范围。'
}
if (Test-Path -LiteralPath $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $BuildRoot, $DistRoot, $ReleaseRoot, $StageRoot -Force | Out-Null

Set-Location -LiteralPath $Workspace
python packaging\make_icon.py
if ($LASTEXITCODE -ne 0) { throw '生成程序图标失败。' }

$PyInstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $PyInstaller) { throw '未找到PyInstaller，请先安装打包工具。' }
& $PyInstaller.Source --noconfirm --clean --distpath $DistRoot --workpath $PyInstallerWork packaging\AutoPPT.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller构建失败。' }

$AppDirectory = Join-Path $DistRoot 'AutoPPT'
if (-not (Test-Path -LiteralPath (Join-Path $AppDirectory 'AutoPPT.exe'))) {
    throw '程序构建结果不完整。'
}

# Windows PowerShell5读取带BOM的UTF8脚本更稳定，安装脚本统一写成该编码。
# 换行也统一为CRLF：BOM+CRLF是Windows PowerShell解析含中文注释脚本的最稳妥组合。
$Utf8Bom = New-Object System.Text.UTF8Encoding($true)
$CrLf = [string][char]13 + [string][char]10
function Write-ScriptFile {
    param([string]$SourcePath, [string]$DestinationPath)
    $Text = [System.IO.File]::ReadAllText($SourcePath, [System.Text.Encoding]::UTF8)
    $Normalized = ($Text -replace "`r`n", "`n") -replace "`n", $CrLf
    [System.IO.File]::WriteAllText($DestinationPath, $Normalized, $Utf8Bom)
}
Write-ScriptFile (Join-Path $PSScriptRoot 'uninstall.ps1') (Join-Path $AppDirectory 'uninstall.ps1')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.txt') -Destination (Join-Path $AppDirectory '使用说明.txt')

$ArchivePath = Join-Path $StageRoot 'AutoPPT.zip'
Compress-Archive -Path (Join-Path $AppDirectory '*') -DestinationPath $ArchivePath -CompressionLevel Optimal
Write-ScriptFile (Join-Path $PSScriptRoot 'install.ps1') (Join-Path $StageRoot 'install.ps1')

# 语法自检：内嵌脚本要运行在Windows PowerShell5.1下，解析失败必须在打包阶段就暴露。
$ParseErrors = $null
$ParseTokens = $null
[void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $StageRoot 'install.ps1'), [ref]$ParseTokens, [ref]$ParseErrors)
if ($ParseErrors.Count -gt 0) {
    throw ('install.ps1语法检查失败：{0}' -f ($ParseErrors[0].Message))
}

# 在项目内模拟一次安装并启动可执行文件，确认安装脚本与独立程序都能正常使用。
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $StageRoot 'install.ps1') -TargetPath $TestInstall -NoLaunch -SkipShortcuts -SkipRegistry
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $TestInstall 'AutoPPT.exe'))) {
    throw '安装脚本验证失败。'
}

$InstallerPath = Join-Path $ReleaseRoot 'AutoPPT-Setup-v1.0.0.exe'
if (Test-Path -LiteralPath $InstallerPath) {
    Remove-Item -LiteralPath $InstallerPath -Force
}

# 安装器本身也使用PyInstaller构建，内置程序压缩包和安装脚本，用户只需运行一个文件。
& $PyInstaller.Source --noconfirm --clean --onefile --console `
    --name 'AutoPPT-Setup-v1.0.0' `
    --icon (Join-Path $PSScriptRoot 'autoppt.ico') `
    --distpath $ReleaseRoot `
    --workpath $SetupWork `
    --specpath $SetupSpec `
    --add-data "$ArchivePath;." `
    --add-data "$(Join-Path $StageRoot 'install.ps1');." `
    (Join-Path $PSScriptRoot 'setup_launcher.py')
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $InstallerPath)) {
    throw '单文件安装包构建失败。'
}

$Hash = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash
$Size = (Get-Item -LiteralPath $InstallerPath).Length
Write-Host "安装包：$InstallerPath"
Write-Host "大小：$Size 字节"
Write-Host "SHA256：$Hash"
