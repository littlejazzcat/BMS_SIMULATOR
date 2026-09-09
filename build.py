"""打包脚本：PyInstaller onedir 模式 + 复制 ZLG 驱动 DLL

用法：python build.py
产物：dist/BMS_SIMULATOR/BMS_SIMULATOR.exe（双击启动，浏览器打开 http://localhost:8000）
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "BMS_SIMULATOR"


def main():
    # 1. 清理旧构建
    for d in ("build", "dist"):
        shutil.rmtree(ROOT / d, ignore_errors=True)
    spec = ROOT / "BMS_SIMULATOR.spec"
    if spec.exists():
        spec.unlink()

    # 2. PyInstaller 打包（onedir，非单文件）
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onedir",
        "--name", "BMS_SIMULATOR",
        "--add-data", "web;web",                      # 前端静态文件
        "--add-data", "protocol/data;protocol/data",  # 协议 JSON
        "--hidden-import", "server.app",              # uvicorn 字符串加载
        "--noconfirm",
        "main.py",
    ]
    print("执行:", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)

    # 3. 复制 ZLG 驱动（zlgcan.py 用相对路径 ./zlgcan.dll 加载，需放在 exe 旁边）
    shutil.copy2(ROOT / "zlgcan.dll", DIST / "zlgcan.dll")
    shutil.copytree(ROOT / "kerneldlls", DIST / "kerneldlls", dirs_exist_ok=True)

    print()
    print("打包完成:", DIST / "BMS_SIMULATOR.exe")
    print("启动后访问: http://localhost:8000")


if __name__ == "__main__":
    main()
