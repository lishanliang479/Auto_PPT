from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files


# 将网页资源和RapidOCR模型一并放入程序，目标电脑无需安装Python或下载模型。
sys.setrecursionlimit(sys.getrecursionlimit() * 5)
workspace = Path.cwd()
datas = [(str(workspace / "web"), "web")] + collect_data_files("rapidocr")
binaries = []
hiddenimports = []

a = Analysis(
    [str(workspace / "app.py")],
    pathex=[str(workspace)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "pandas", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoPPT",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(workspace / "packaging" / "autoppt.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AutoPPT",
)
