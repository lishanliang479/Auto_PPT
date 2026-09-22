"""AutoPPT单文件安装器入口。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    # PyInstaller单文件程序会先把安装资源释放到临时目录。
    resource_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    install_script = resource_root / "install.ps1"
    archive = resource_root / "AutoPPT.zip"
    if not install_script.is_file() or not archive.is_file():
        print("安装资源不完整，请重新下载安装包。")
        input("按回车键退出。")
        return 1

    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(install_script),
    ]
    # 构建验证可把安装参数继续传给脚本，普通双击安装时参数为空。
    result = subprocess.run(command + sys.argv[1:])
    if result.returncode:
        print("安装未完成，请保留此窗口中的错误信息。")
        input("按回车键退出。")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
